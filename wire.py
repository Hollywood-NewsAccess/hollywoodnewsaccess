#!/usr/bin/env python3
"""
wire.py - the publishing engine for Hollywood News Access.

WHAT THIS DOES, PLAIN ENGLISH
-----------------------------
Every time it runs it pulls the public RSS feeds of the trades, reads the
headline, summary, publish time and photo out of each feed item, sorts the
items into sections by keyword, and rewrites docs/index.html plus the seven
section pages from scratch. After this, nobody hand-types an index page again.

Three things it merges together on every page:

  1. ORIGINATED - our own reporting, read from data/originated.json. These sit
     above everything else and never expire on the wire timer. This manifest is
     what stops a regeneration from wiping our own journalism off the site.
  2. WIRE - headline cards built from the trades' own RSS feeds. Each card
     shows the outlet's name, credits the photo to that outlet, and links out
     to the original article on the outlet's own site. We do not reproduce
     their article text - just the headline, a short trailing summary, and the
     thumbnail the feed itself publishes. That is the aggregator model (Google
     News, Flipboard, SmartNews), not syndication, and it needs no licence
     because the feed is published for exactly this use.
  3. TAKEOVERS - date-gated event skins, read from data/event-takeovers.json.
     While today is inside a takeover's start..end window the banner and nav
     item appear on every page. The day after it ends they vanish on the next
     hourly run with nobody touching a file.

PHOTOS
------
Pulled in this order out of each feed item, exactly as the feed publishes them:
  media:content url=  ->  media:thumbnail url=  ->  enclosure url=  ->
  the first <img> inside content:encoded or description
If an item ships no image, the card gets a CSS gradient well. No card is ever
bare, and no image is ever invented, scraped from a stock library, or presented
as ours. Wire photos are always credited to the outlet that published them.

WHAT IT NEVER DOES
------------------
Never invents a headline, a date, a byline or a photo. An item with no
parseable publish date is dropped rather than guessed at. BREAKING and
EXCLUSIVE flags are only ever set when those words are genuinely in the
outlet's own headline.

HOW TO RUN IT
-------------
  python3 wire.py              - full run, writes the pages
  python3 wire.py --dry-run    - fetch and report, write nothing

Runs hourly in CI via .github/workflows/wire.yml, which also has a
"Run workflow" button for forcing a run. Cost is zero: stdlib Python only, no
dependencies to install, no AI model calls, free Actions minutes on a public
repo. Every run appends its feed results to runbook/wire-feed-status.md so
there is always a record of which feeds answered and which were dropped.
"""

import html
import json
import urllib.parse
import urllib.request
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(ROOT, "data")
STATUS_LOG = os.path.join(ROOT, "runbook", "wire-feed-status.md")

# Los Angeles is UTC-7 in September (PDT). The site's dateline is LA time.
LA = timezone(timedelta(hours=-7))

WIRE_WINDOW_DAYS = 7          # house rule: wire items older than this drop off
FETCH_TIMEOUT = 20            # seconds per feed
MAX_PER_SECTION = 12
HOME_WIRE_COUNT = 12

USER_AGENT = (
    "HollywoodNewsAccessBot/1.0 (+https://hollywoodnewsaccess.com; "
    "RSS aggregation with attribution and outbound links)"
)

# Sections wire.py owns. Order matters: an item lands in the FIRST section
# whose keywords it matches, so nothing is double-posted.
SECTIONS = [
    ("love", "Love & Family", "Who is dating, engaged, married, expecting, or calling it quits.", [
        "dating", "engaged", "engagement", "wedding", "married", "marries", "divorce",
        "split", "breakup", "broke up", "baby", "pregnant", "pregnancy", "boyfriend",
        "girlfriend", "romance", "couple", "husband", "wife", "custody", "proposal",
    ]),
    ("red-carpet", "Red Carpet & Style", "The looks, the gowns, the glam and the best dressed.", [
        "red carpet", "gown", "best dressed", "worst dressed", "met gala", "fashion week",
        "outfit", "wore", "dress", "style", "beauty", "makeup", "hairstyle", "runway",
        "designer", "stylist", "look",
    ]),
    ("music", "Music", "Albums, tours, beefs, charts and the stars behind them.", [
        "album", "single", "tour", "concert", "rapper", "singer", "grammy", "vmas",
        "bet awards", "billboard", "song", "music video", "setlist", "festival",
        "coachella", "residency", "drops new",
    ]),
    ("tv", "TV & Streaming", "Reality, series, finales, talk shows and what everyone is watching.", [
        "reality", "housewives", "bachelor", "bachelorette", "love island", "survivor",
        "season", "episode", "finale", "series", "netflix", "hulu", "hbo", "peacock",
        "talk show", "late night", "emmy", "sitcom", "streaming",
    ]),
    ("movies", "Movies", "Premieres, trailers, casting and the box office.", [
        "box office", "trailer", "premiere", "sequel", "casting", "cast as", "biopic",
        "film", "movie", "oscar", "academy award", "marvel", "director",
    ]),
    ("celebrity", "Celebrity", "The stars, the moments and the stories everyone is talking about.", [
        "celebrity", "star", "actor", "actress", "spotted", "reveals", "opens up",
        "slams", "responds", "instagram", "tiktok", "viral", "feud", "birthday",
        "net worth", "arrested", "lawsuit", "sues", "tribute", "dies", "death",
        "health", "interview", "exclusive", "throwback", "fans",
    ]),
]

SECTION_LOOKUP = {slug: (label, blurb, kws) for slug, label, blurb, kws in SECTIONS}

# Gradient wells for items that ship no photo. Built from the house palette,
# not from stock imagery - an honest graphic, not a fake picture.
GRADIENTS = [
    "linear-gradient(135deg,#2A0B33 0%,#0C0710 55%,#FF2E88 150%)",
    "linear-gradient(135deg,#140B3A 0%,#07051A 55%,#6B3BFF 150%)",
    "linear-gradient(160deg,#1A0E24 0%,#0A0610 50%,#FFC233 160%)",
    "linear-gradient(200deg,#250A20 0%,#0B0509 55%,#FF5A3C 150%)",
]


# --------------------------------------------------------------------------
# fetching and parsing
# --------------------------------------------------------------------------

def fetch(url):
    """Return the raw feed text, or raise with a readable reason."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":          # gzip, some hosts send it regardless
        import gzip
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _tag(block, name):
    """Pull the text of the first <name>...</name> out of an item block."""
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (name, name), block, re.S | re.I)
    if not m:
        return ""
    text = m.group(1).strip()
    cdata = re.match(r"^<!\[CDATA\[(.*?)\]\]>$", text, re.S)
    if cdata:
        text = cdata.group(1)
    return text.strip()


# Things that appear in feeds looking like images but are not photographs:
# analytics pixels, share buttons, avatars, spacers. A card showing one of
# these looks broken, so they never count as the item's picture.
NOT_A_PHOTO = re.compile(
    r"(pixel|/stats?[/.]|track|beacon|feedburner|doubleclick|gravatar|"
    r"emoji|spacer|blank\.|1x1|avatar|badge|logo|button|icon)", re.I)


def looks_like_photo(url):
    if not url.startswith("http"):
        return False
    if NOT_A_PHOTO.search(url):
        return False
    if re.search(r"\.gif(\?|$)", url, re.I):
        return False
    return True


def _attr_url(block, pattern):
    """First url="..." on a tag matching pattern, if it looks like an image."""
    for m in re.finditer(pattern, block, re.I):
        url = html.unescape(m.group(1)).strip()
        if looks_like_photo(url):
            return url
    return ""


def extract_image(block):
    """Feed image, in the documented priority order. Empty string if none."""
    url = _attr_url(block, r"<media:content[^>]*\burl=[\"']([^\"']+)[\"']")
    if url:
        return url
    url = _attr_url(block, r"<media:thumbnail[^>]*\burl=[\"']([^\"']+)[\"']")
    if url:
        return url
    for m in re.finditer(r"<enclosure[^>]*>", block, re.I):
        tag = m.group(0)
        if re.search(r'type=["\']image/', tag, re.I) or not re.search(r'type=', tag, re.I):
            u = _attr_url(tag, r'\burl=["\']([^"\']+)["\']')
            if u and re.search(r"\.(jpe?g|png|webp)", u, re.I):
                return u
    for tag_name in ("content:encoded", "description", "content", "summary"):
        body = _tag(block, tag_name)
        if not body:
            continue
        for m in re.finditer(r'<img[^>]*\bsrc=["\']([^"\']+)["\']', html.unescape(body), re.I):
            if looks_like_photo(m.group(1)):
                return m.group(1)
    # Last resort: the first image URL published anywhere in the item. Feeds
    # carry thumbnails under tags we have not anticipated (media:group,
    # post-thumbnail, image href, og:image mirrors), and this catches those
    # without inventing anything - it is still only ever a URL the outlet
    # itself published in its own feed.
    for m in re.finditer(r'["\'(>\s](https?://[^"\'<>)\s]+\.(?:jpe?g|png|webp))', block, re.I):
        url = html.unescape(m.group(1))
        if looks_like_photo(url):
            return url
    return ""


def strip_tags(s, limit=210):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        cut = s[:limit].rsplit(" ", 1)[0]
        s = cut.rstrip(",.;:-") + "…"
    return s


def parse_date(block):
    raw = _tag(block, "pubDate") or _tag(block, "published") or _tag(block, "updated")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(text, source_name):
    """Turn feed XML into item dicts. Regex-based on purpose: real trade feeds
    are frequently not strictly well-formed, and a parser that throws on one
    stray ampersand would cost us the whole outlet for that hour."""
    items = []
    blocks = re.findall(r"<item\b.*?</item>", text, re.S | re.I)
    if not blocks:
        blocks = re.findall(r"<entry\b.*?</entry>", text, re.S | re.I)
    for block in blocks:
        title = strip_tags(_tag(block, "title"), 160)
        link = _tag(block, "link")
        if not link:
            m = re.search(r'<link[^>]*\bhref=["\']([^"\']+)["\']', block, re.I)
            link = m.group(1) if m else ""
        link = html.unescape(link).strip()
        published = parse_date(block)
        if not title or not link.startswith("http") or published is None:
            continue        # no guessing: an item we cannot date, we do not run
        items.append({
            "title": title,
            "link": link,
            "summary": strip_tags(_tag(block, "description") or _tag(block, "content:encoded")),
            "published": published,
            "image": extract_image(block),
            "source": source_name,
        })
    return items


def diagnose_no_photos(text):
    """A feed that returns items but no photos is either publishing images in a
    shape we do not read, or not publishing them at all. Those need different
    fixes, so say which it is in the status log instead of leaving it a
    mystery."""
    first = re.search(r"<(item|entry)\b.*?</\1>", text, re.S | re.I)
    block = first.group(0) if first else text[:4000]
    found = [t for t in ("media:content", "media:thumbnail", "media:group",
                         "enclosure", "content:encoded", "<img")
             if t in block.lower()]
    if not found:
        return "feed publishes no image fields at all"
    urls = re.findall(r"https?://[^\"'<>)\s]+\.(?:jpe?g|png|webp|gif)", block, re.I)
    if urls and all(not looks_like_photo(u) for u in urls):
        return "only non-photo assets (%s)" % ", ".join(sorted(found))
    return "carries %s but no usable photo URL" % ", ".join(sorted(found))


def collect(feeds, dry_run=False):
    """Fetch every feed. Returns (items, per-feed status rows)."""
    items, status = [], []
    for feed in feeds:
        name, url = feed["name"], feed["url"]
        try:
            text = fetch(url)
        except urllib.error.HTTPError as e:
            status.append((name, url, "dropped", "HTTP %s" % e.code, 0, 0))
            continue
        except Exception as e:                      # timeouts, DNS, TLS, resets
            status.append((name, url, "dropped", type(e).__name__, 0, 0))
            continue
        parsed = parse_feed(text, name)
        if not parsed:
            status.append((name, url, "dropped", "no parseable items", 0, 0))
            continue
        with_photo = sum(1 for i in parsed if i["image"])
        note = "" if with_photo else diagnose_no_photos(text)
        status.append((name, url, "ok", note, len(parsed), with_photo))
        items.extend(parsed)
    return items, status


# --------------------------------------------------------------------------
# routing, flags, takeovers
# --------------------------------------------------------------------------

def classify(item):
    hay = (" " + item["title"] + " " + item["summary"] + " ").lower()
    for slug, _label, _blurb, keywords in SECTIONS:
        for kw in keywords:
            if kw in hay:
                return slug
    return None


def flags_for(item):
    """Only ever true when the outlet's own headline says so."""
    t = item["title"].lower()
    return {
        "breaking": bool(re.search(r"\bbreaking\b", t)),
        "exclusive": bool(re.search(r"\bexclusive\b", t)),
    }


def active_takeover(takeovers, today):
    for t in takeovers:
        start = datetime.strptime(t["start"], "%Y-%m-%d").date()
        end = datetime.strptime(t["end"], "%Y-%m-%d").date()
        if start <= today <= end:
            return t
    return None


# --------------------------------------------------------------------------
# HTML building blocks
# --------------------------------------------------------------------------

def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


CSS = """:root{--ink:#0C0811;--body:#26212D;--paper:#FFFFFF;--soft:#F6F2F8;--rule:#E3DCE8;--rule-soft:#EFEAF2;--signal:#FF2E88;--date:#6B3BFF;--flag:#FFC233;--hot:#FF2E88;--violet:#6B3BFF;--hed:'Anton',Impact,'Arial Narrow',sans-serif;--sub:'Bricolage Grotesque',system-ui,sans-serif;--txt:'Instrument Sans',system-ui,sans-serif;--wrap:1240px}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--paper);color:var(--body);font-family:var(--txt);font-size:16.5px;line-height:1.55}
a{color:inherit;text-decoration:none}
a:focus-visible{outline:3px solid var(--hot);outline-offset:2px}
img{display:block;max-width:100%}
.wrap{max-width:var(--wrap);margin:0 auto;padding:0 18px}
.masthead{background:var(--ink);color:#fff;padding:16px 0 12px;position:relative;overflow:hidden}
.masthead::before{content:"";position:absolute;inset:-40% -10% auto auto;width:520px;height:520px;background:radial-gradient(closest-side,rgba(255,46,136,.38),transparent 70%);pointer-events:none}
.masthead::after{content:"";position:absolute;inset:auto auto -60% -8%;width:460px;height:460px;background:radial-gradient(closest-side,rgba(107,59,255,.34),transparent 70%);pointer-events:none}
.masthead .wrap{position:relative;display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{font-family:var(--hed);font-weight:400;font-size:clamp(34px,6vw,58px);line-height:.9;letter-spacing:.5px;text-transform:uppercase;display:block}
.brand em{font-style:normal;background:linear-gradient(90deg,var(--hot),#FF7AB8 45%,var(--violet));-webkit-background-clip:text;background-clip:text;color:transparent}
.standfirst{font-family:var(--sub);font-weight:700;font-size:12px;letter-spacing:.22em;color:#C9BFD3;margin-top:8px;text-transform:uppercase}
.dateline{font-family:var(--sub);font-weight:500;font-size:12px;letter-spacing:.06em;color:#9A8FA6;text-align:right}
.dateline b{display:block;color:var(--flag);letter-spacing:.2em;font-size:11px;text-transform:uppercase}
.nav{background:var(--ink);border-top:1px solid #2A2232;border-bottom:4px solid var(--hot);overflow-x:auto;position:sticky;top:env(safe-area-inset-top,0px);z-index:20}
.nav ul{display:flex;list-style:none;white-space:nowrap;gap:24px;padding:12px 18px;max-width:var(--wrap);margin:0 auto}
.nav a{font-family:var(--sub);font-weight:800;font-size:13.5px;letter-spacing:.08em;color:#F1ECF5;text-transform:uppercase}
.nav a:hover,.nav a.current{color:var(--hot)}
.ticker{background:var(--flag);color:var(--ink);overflow:hidden;display:flex;align-items:stretch;border-bottom:1px solid #E6A800}
.ticker b{flex:none;position:relative;z-index:2;box-shadow:8px 0 12px -4px rgba(0,0,0,.35);background:var(--ink);color:var(--flag);font-family:var(--hed);font-weight:400;font-size:15px;letter-spacing:.12em;text-transform:uppercase;padding:8px 14px;display:flex;align-items:center}
.ticker-track{display:flex;white-space:nowrap;animation:tick 70s linear infinite;padding:8px 0}
.ticker-track a{font-family:var(--sub);font-weight:700;font-size:14px;padding:0 22px;flex:none}
.ticker-track a::after{content:'\\2605';margin-left:22px;color:var(--hot)}
.ticker:hover .ticker-track{animation-play-state:paused}
@keyframes tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.ticker-track{animation:none;overflow-x:auto}}
.takeover-banner{background:var(--hot);color:#fff;text-align:center;padding:9px 18px;font-family:var(--sub);font-weight:800;font-size:13px;letter-spacing:.1em;text-transform:uppercase}
.takeover-banner a{color:#fff;border-bottom:1px solid rgba(255,255,255,.6)}
.carpet{background:var(--ink);border-top:3px solid var(--flag);border-bottom:3px solid var(--flag);overflow:hidden;padding:10px 0}
.carpet-track{display:flex;white-space:nowrap;font-family:var(--hed);font-size:14px;letter-spacing:.32em;text-transform:uppercase;color:var(--flag)}
.carpet-track span{padding:0 26px;flex:none}
.carpet-track span:not(:last-child)::after{content:'\\2605';margin-left:26px;color:var(--hot)}
.lede{position:relative;min-height:64vh;display:flex;align-items:flex-end;background:var(--ink);overflow:hidden}
.lede>.wrap{width:100%}
.lede-img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.72}
.lede-fill{position:absolute;inset:0}
@media(max-width:700px){.lede--face{display:block;min-height:0}.lede--face .lede-img{position:relative;inset:auto;display:block;height:auto;aspect-ratio:16/9;opacity:1}.lede--face .lede-shade{display:none}.lede--face .lede-copy{padding:22px 0 30px}}
.lede-shade{position:absolute;inset:0;background:linear-gradient(0deg,rgba(12,8,17,.96) 0%,rgba(12,8,17,.6) 42%,rgba(12,8,17,.05) 100%),linear-gradient(90deg,rgba(255,46,136,.28),transparent 55%)}
.lede-copy{position:relative;padding:70px 0 40px;color:#fff;max-width:44rem}
.lede h1{font-family:var(--hed);font-weight:400;font-size:clamp(2.4rem,5.4vw,4.6rem);line-height:.92;letter-spacing:.5px;text-transform:uppercase;color:#fff;margin:14px 0 14px;text-wrap:balance}
.lede h1 a:hover{color:var(--flag)}
.lede p{font-size:19px;color:#EDE7F2;line-height:1.45;max-width:34em}
.lede .credit{font-family:var(--sub);font-size:11.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:#C2B7CC;margin-top:16px}
.tagrow{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
.tag{font-family:var(--sub);font-weight:800;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;padding:5px 10px;background:var(--hot);color:#fff;border-radius:2px}
.tag.flag{background:var(--flag);color:var(--ink)}
.tag.ours{background:var(--violet);color:#fff}
h2.sect{font-family:var(--hed);font-weight:400;font-size:clamp(28px,5vw,44px);letter-spacing:.5px;text-transform:uppercase;color:var(--ink);margin:52px 0 6px;display:flex;align-items:center;gap:14px}
h2.sect::before{content:"";width:14px;height:14px;background:var(--hot);transform:rotate(45deg);flex:none}
h2.sect::after{content:"";flex:1;height:3px;background:linear-gradient(90deg,var(--ink),transparent)}
.sect-note{font-size:15px;color:#6E6577;margin-bottom:24px;max-width:52em}
.grid{display:grid;grid-template-columns:1fr;gap:28px 24px}
@media(min-width:620px){.grid{grid-template-columns:1fr 1fr}}
@media(min-width:1000px){.grid{grid-template-columns:repeat(4,1fr)}.grid>.card:first-child{grid-column:span 2;grid-row:span 2}.grid>.card:first-child h3{font-size:30px}.grid>.card:first-child .shot{padding-top:68%}}
.card{display:flex;flex-direction:column;min-width:0}
.card .shot{position:relative;width:100%;padding-top:62%;margin-bottom:12px;overflow:hidden;background:var(--ink);border-radius:6px}
.card .shot img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transition:transform .5s ease}
.card:hover .shot img{transform:scale(1.04)}
@media (prefers-reduced-motion:reduce){.card .shot img{transition:none}.card:hover .shot img{transform:none}}
.card .shot .well{position:absolute;inset:0}
.card .shot .well.plate{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:14px}
.card .shot .well.plate::after{content:none}
.plate-desk{font-family:var(--hed);font-size:20px;letter-spacing:.08em;text-transform:uppercase;color:#fff;line-height:1.1}
.plate-rule{display:block;width:34px;height:3px;background:var(--hot);margin:10px 0}
.plate-mark{font-family:var(--sub);font-weight:700;font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;color:rgba(255,255,255,.66)}
.card .shot .well::after{content:attr(data-outlet);position:absolute;left:14px;bottom:12px;right:14px;font-family:var(--hed);font-size:16px;letter-spacing:.12em;text-transform:uppercase;color:rgba(255,255,255,.7)}
.card h3{font-family:var(--sub);font-weight:800;font-size:19px;line-height:1.14;letter-spacing:-.2px;color:var(--ink);margin:8px 0 7px;text-wrap:balance}
.card:hover h3{color:var(--hot)}
.card p{font-size:14.5px;line-height:1.48;color:#5E5568}
.card .credit{font-family:var(--sub);font-size:11.5px;font-weight:600;letter-spacing:.04em;color:#8D8496;margin-top:10px}
.card .credit b{color:var(--hot);font-weight:800;text-transform:uppercase;letter-spacing:.08em}
.empty{border-top:3px solid var(--hot);padding:26px 0;margin-top:26px;color:#6E6577;font-size:16px;max-width:44em}
.band{background:var(--ink);color:#fff;padding:40px 0;margin-top:60px;position:relative;overflow:hidden}
.band::before{content:"";position:absolute;inset:auto -10% -80% auto;width:600px;height:600px;background:radial-gradient(closest-side,rgba(107,59,255,.35),transparent 70%)}
.band .wrap{position:relative}
.band h2{font-family:var(--hed);font-weight:400;font-size:clamp(26px,5vw,40px);text-transform:uppercase;letter-spacing:.5px;margin-bottom:9px}
.band p{color:#C6BCD0;max-width:48em}
.band a{color:var(--flag);border-bottom:1px solid var(--flag)}
footer{background:var(--soft);border-top:4px solid var(--hot);padding:30px 0 50px;font-family:var(--sub);font-size:13px;color:#6E6577}
footer nav{display:flex;flex-wrap:wrap;gap:18px;margin-bottom:14px}
footer nav a{color:var(--ink);font-weight:800;letter-spacing:.06em;text-transform:uppercase;font-size:12.5px}
footer nav a:hover{color:var(--hot)}
.machine{font-size:12.5px;color:#8D8496;margin-top:10px}
@media(max-width:700px){.dateline{text-align:left}.lede{min-height:46vh}.lede-copy{padding:40px 0 30px}}"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
         '<link href="https://fonts.googleapis.com/css2?family=Anton'
         '&family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800'
         '&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">')


NAV_ORDER = ["celebrity", "love", "red-carpet", "music", "tv", "movies"]


def nav_html(current, takeover):
    items = [("index.html", "Home")]
    items += [(slug + ".html", SECTION_LOOKUP[slug][0]) for slug in NAV_ORDER]
    rows = []
    for href, label in items:
        cls = ' class="current"' if href == current else ""
        rows.append("<li><a href=\"%s\"%s>%s</a></li>" % (href, cls, esc(label)))
    if takeover:
        rows.append('<li><a href="%s" style="color:var(--signal)">%s</a></li>'
                    % (esc(takeover["link"]), esc(takeover["nav_label"])))
    rows.append('<li><a href="wire.html">The Wire</a></li>')
    rows.append('<li><a href="pressroom.html">Press Room</a></li>')
    return '<nav class="nav"><ul>\n%s\n</ul></nav>' % "\n".join(rows)


def head_html(title, description, canonical):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<meta name="description" content="%s">
<meta property="og:title" content="%s">
<meta property="og:description" content="%s">
<meta property="og:type" content="website">
<link rel="canonical" href="%s">
<link rel="alternate" type="application/rss+xml" title="Hollywood News Access" href="https://hollywoodnewsaccess.com/feed.xml">
<link rel="sitemap" type="application/xml" href="sitemap.xml">
%s
<style>
%s
</style>
</head>""" % (esc(title), esc(description), esc(title), esc(description),
               esc(canonical), FONTS, CSS)


def masthead_html(now_la):
    return """<header class="masthead"><div class="wrap">
<div><a class="brand" href="index.html">Hollywood <em>News Access</em></a>
<div class="standfirst">The center of the entertainment universe</div></div>
<div class="dateline"><b>Live from Hollywood</b>%s</div>
</div></header>""" % now_la.strftime("%A, %B %-d, %Y")


def ticker_html(items):
    """The running headline strip under the nav. Wire items link out to the
    outlet that reported them, the same as the cards."""
    items = [i for i in items if i.get("title")][:10]
    if not items:
        return ""
    links = "".join('<a href="%s"%s>%s</a>' % (
        esc(i["link"]), "" if i.get("source") == "Hollywood News Access" else ' target="_blank" rel="noopener"',
        esc(i["title"])) for i in items)
    return ('<div class="ticker" aria-label="Latest headlines"><b>Just in</b>'
            '<div class="ticker-track">%s%s</div></div>' % (links, links))


def banner_html(takeover):
    if not takeover:
        return ""
    return ('<div class="takeover-banner"><a href="%s">%s &rarr;</a></div>'
            % (esc(takeover["link"]), esc(takeover["banner"])))


def carpet_html(takeover):
    if not takeover or not takeover.get("carpet"):
        return ""
    words = [esc(w) for w in takeover["carpet"]] * 2
    return ('<div class="carpet"><div class="carpet-track">%s</div></div>'
            % "".join("<span>%s</span>" % w for w in words))


_FOCUS_RE = re.compile(r"^(\d{1,3}%|left|center|right) (\d{1,3}%|top|center|bottom)$")


def focus_attr(item):
    """A story can name where its photo should stay anchored when the page
    crops it, e.g. "50% 20%" to keep a face near the top. Anything that does
    not match the strict pattern is ignored, so a manifest can never inject
    markup through it."""
    f = (item.get("image_focus") or "").strip()
    return ' style="object-position:%s"' % f if _FOCUS_RE.match(f) else ""



def lede_html(item, is_ours):
    """Full-bleed hero. Real feed photo when the item has one, house gradient
    well when it does not - never a stock photo standing in for either."""
    if item.get("image"):
        backdrop = '<img class="lede-img" src="%s"%s alt="" loading="eager">' % (esc(item["image"]), focus_attr(item))
        credit = "Photo: %s" % esc(item["source"]) if not is_ours else ""
    else:
        backdrop = '<div class="lede-fill" style="background:%s"></div>' % GRADIENTS[0]
        credit = "" if is_ours else "No photo published in %s's feed" % esc(item["source"])
    if is_ours:
        tags = '<span class="tag ours">%s</span>' % esc(item.get("tag", "Hollywood News Access"))
        link_open, link_close = '<a href="%s">' % esc(item["link"]), "</a>"
        credit_line = esc(item.get("byline", ""))
    else:
        f = flags_for(item)
        tags = ""
        if f["breaking"]:
            tags += '<span class="tag flag">Breaking</span>'
        if f["exclusive"]:
            tags += '<span class="tag flag">Exclusive</span>'
        tags += '<span class="tag">%s</span>' % esc(item["source"])
        link_open = '<a href="%s" target="_blank" rel="noopener">' % esc(item["link"])
        link_close = "</a>"
        bits = ["Reported by %s" % esc(item["source"])]
        if credit:
            bits.append(credit)
        credit_line = " &middot; ".join(bits)
    return """<section class="lede%s">
%s
<div class="lede-shade"></div>
<div class="wrap"><div class="lede-copy">
<div class="tagrow">%s</div>
<h1>%s%s%s</h1>
<p>%s</p>
<div class="credit">%s</div>
</div></div>
</section>""" % (" lede--face" if focus_attr(item) else "", backdrop, tags, link_open, esc(item["title"]), link_close,
                 esc(item.get("dek") or item.get("summary", "")), credit_line)


def card_html(item, index, is_ours=False):
    if item.get("image"):
        shot = '<div class="shot"><img src="%s"%s alt="" loading="lazy"></div>' % (esc(item["image"]), focus_attr(item))
    elif is_ours:
        # Our own reporting with no licensed photograph. A bare gradient reads
        # as a hole where a picture should be; a typographic plate reads as a
        # newspaper running a story without art, which is what this is.
        well = GRADIENTS[index % len(GRADIENTS)]
        shot = ('<div class="shot"><div class="well plate" style="background:%s">'
                '<span class="plate-desk">%s</span>'
                '<span class="plate-rule"></span>'
                '<span class="plate-mark">Hollywood News Access</span>'
                '</div></div>'
                % (well, esc(item.get("tag", "Reporting"))))
    else:
        well = GRADIENTS[index % len(GRADIENTS)]
        shot = ('<div class="shot"><div class="well" style="background:%s" data-outlet="%s"></div></div>'
                % (well, esc(item["source"])))
    if is_ours:
        tags = '<span class="tag ours">%s</span>' % esc(item.get("tag", "Ours"))
        head = '<h3><a href="%s">%s</a></h3>' % (esc(item["link"]), esc(item["title"]))
        photo_line = ""
        if item.get("image") and item.get("photo_author"):
            photo_line = (" &middot; Photo: %s (%s)"
                          % (esc(item["photo_author"][:42]),
                             esc(item.get("photo_licence", "") or "free licence")))
        credit = ('<div class="credit">%s%s</div>'
                  % (esc(item.get("byline", "")), photo_line))
        body = esc(item.get("dek", ""))
    else:
        f = flags_for(item)
        tags = ""
        if f["breaking"]:
            tags += '<span class="tag flag">Breaking</span>'
        if f["exclusive"]:
            tags += '<span class="tag flag">Exclusive</span>'
        tags += '<span class="tag">%s</span>' % esc(item["section_label"])
        head = ('<h3><a href="%s" target="_blank" rel="noopener">%s</a></h3>'
                % (esc(item["link"]), esc(item["title"])))
        photo_credit = (" &middot; Photo: %s" % esc(item["source"])) if item.get("image") else ""
        credit = ('<div class="credit"><b>%s</b>%s &middot; <a href="%s" target="_blank" '
                  'rel="noopener">Read it there &rarr;</a></div>'
                  % (esc(item["source"]), photo_credit, esc(item["link"])))
        body = esc(item.get("summary", ""))
    return ('<article class="card">%s<div class="tagrow">%s</div>%s<p>%s</p>%s</article>'
            % (shot, tags, head, body, credit))


def footer_html(now_utc, feed_ok, feed_total):
    return """<footer><div class="wrap">
<nav>
<a href="index.html">Home</a>
<a href="wire.html">The Wire</a>
<a href="pressroom.html">Press Room</a>
<a href="pressroom.html#advertise">Advertise</a>
<a href="pressroom.html#standards">Standards &amp; Corrections</a>
</nav>
<div>&copy; 2026 Hollywood News Access. All rights reserved. Los Angeles, California.</div>
<div class="machine">Headline cards credited to another outlet are that outlet's reporting, shown
from their own public feed with their own photo and a link to the original. Our reporting carries a
Hollywood News Access desk byline. Page rebuilt automatically %s UTC from %d of %d feeds.</div>
</div></footer>""" % (now_utc.strftime("%Y-%m-%d %H:%M"), feed_ok, feed_total)


# --------------------------------------------------------------------------
# page generation
# --------------------------------------------------------------------------

def build_index(ours, wire_items, takeover, now_la, now_utc, feed_ok, feed_total):
    lede_item, rest = None, list(wire_items)
    if takeover:
        for o in ours:
            if o["link"] == takeover["link"]:
                lede_item = dict(o)
                break
    if lede_item is None and ours:
        lede_item = dict(ours[0])
    lede_is_ours = lede_item is not None
    if lede_item is None and rest:
        lede_item, rest = rest[0], rest[1:]

    parts = [head_html(
        "Hollywood News Access — Celebrity News, Red Carpets, Music, TV & Movies",
        "Celebrity news, red carpets, music, TV and movies from Hollywood, "
        "updated around the clock. Our own reporting plus the wire.",
        "https://hollywoodnewsaccess.com/")]
    parts.append("<body>")
    parts.append(masthead_html(now_la))
    parts.append(nav_html("index.html", takeover))
    parts.append(ticker_html((ours or []) + list(wire_items)))
    parts.append(banner_html(takeover))
    parts.append(carpet_html(takeover))
    if lede_item:
        parts.append(lede_html(lede_item, lede_is_ours))
    parts.append('<main><div class="wrap">')

    others = [o for o in ours if not (lede_is_ours and o["link"] == lede_item["link"])]
    if others:
        parts.append('<h2 class="sect">HNA exclusives</h2>')
        parts.append('<p class="sect-note">Originated by Hollywood News Access desks. '
                     'Every one of these is ours, reported and bylined.</p>')
        parts.append('<div class="grid">%s</div>'
                     % "".join(card_html(o, i, is_ours=True) for i, o in enumerate(others)))

    if rest:
        parts.append('<h2 class="sect">The wire</h2>')
        parts.append('<p class="sect-note">Headlines and photos from the outlets\' own feeds, '
                     'credited to the outlet that reported them, linking straight to their story. '
                     'Refreshed hourly; anything older than seven days drops off on its own.</p>')
        parts.append('<div class="grid">%s</div>'
                     % "".join(card_html(it, i) for i, it in enumerate(rest[:HOME_WIRE_COUNT])))
    else:
        parts.append('<div class="empty">No wire items came back on the last run. '
                     'That means every feed was unreachable or returned nothing inside the '
                     'seven-day window &mdash; it does not mean the industry went quiet. '
                     'See runbook/wire-feed-status.md for which feeds answered.</div>')

    parts.append("</div></main>")
    parts.append("""<section class="band"><div class="wrap">
<h2>Standards &amp; corrections</h2>
<p>Sourcing, embargoes, image licensing and corrections are covered in our
<a href="pressroom.html#standards">editorial standards</a>. Wire cards credit and link to the outlet
that did the reporting; we do not republish their article text. Spotted an error?
<a href="pressroom.html#standards">Send it through the Press Room</a>.</p>
</div></section>""")
    parts.append(footer_html(now_utc, feed_ok, feed_total))
    parts.append("""<script type="application/ld+json">
{"@context":"https://schema.org","@type":"NewsMediaOrganization","name":"Hollywood News Access","url":"https://hollywoodnewsaccess.com/","description":"Celebrity news, red carpets, music, TV and movies from Hollywood.","address":{"@type":"PostalAddress","addressLocality":"Los Angeles","addressRegion":"CA","addressCountry":"US"},"publishingPrinciples":"https://hollywoodnewsaccess.com/pressroom.html#standards","correctionsPolicy":"https://hollywoodnewsaccess.com/pressroom.html#standards"}
</script>""")
    parts.append("</body>\n</html>")
    return "\n".join(p for p in parts if p) + "\n"


def build_section(slug, ours, wire_items, takeover, now_la, now_utc, feed_ok, feed_total):
    label, blurb, _kw = SECTION_LOOKUP[slug]
    title = "%s | Hollywood News Access" % label
    lede_item = dict(ours[0]) if ours else (dict(wire_items[0]) if wire_items else None)
    lede_is_ours = bool(ours)
    rest = wire_items[1:] if (wire_items and not lede_is_ours) else wire_items

    parts = [head_html(title, "Hollywood News Access's %s desk: %s" % (label, blurb),
                       "https://hollywoodnewsaccess.com/%s.html" % slug)]
    parts.append("<body>")
    parts.append(masthead_html(now_la))
    parts.append(nav_html(slug + ".html", takeover))
    parts.append(banner_html(takeover))
    if lede_item:
        parts.append(lede_html(lede_item, lede_is_ours))
    parts.append('<main><div class="wrap">')

    tail_ours = ours[1:]
    if tail_ours:
        parts.append('<h2 class="sect">Our %s reporting</h2>' % esc(label.lower()))
        parts.append('<div class="grid">%s</div>'
                     % "".join(card_html(o, i, is_ours=True) for i, o in enumerate(tail_ours)))

    if rest:
        parts.append('<h2 class="sect">%s on the wire</h2>' % esc(label))
        parts.append('<p class="sect-note">From the outlets\' own feeds, credited and linked to '
                     'the outlet that reported it. Rebuilt hourly, seven-day window.</p>')
        parts.append('<div class="grid">%s</div>'
                     % "".join(card_html(it, i) for i, it in enumerate(rest[:MAX_PER_SECTION])))
    elif not ours:
        parts.append('<div class="empty">Nothing matched %s on the wire in the last seven days, '
                     'and our own %s coverage has not published yet. This page fills itself the '
                     'moment either one lands &mdash; nobody types into it by hand.</div>'
                     % (esc(label), esc(label.lower())))

    parts.append("</div></main>")
    parts.append(footer_html(now_utc, feed_ok, feed_total))
    parts.append("""<script type="application/ld+json">
{"@context":"https://schema.org","@type":"CollectionPage","name":"%s","description":"Hollywood News Access's %s desk: %s","url":"https://hollywoodnewsaccess.com/%s.html","publisher":{"@type":"NewsMediaOrganization","name":"Hollywood News Access"}}
</script>""" % (esc(title), esc(label), esc(blurb), slug))
    parts.append("</body>\n</html>")
    return "\n".join(p for p in parts if p) + "\n"


LABELS = {
    "community":  ("COMMUNITY",  "Local Los Angeles. Ours, or submitted by the organization named."),
    "syndicated": ("SYNDICATED", "Written elsewhere, carried here with attribution and a link out."),
    "sponsored":  ("SPONSORED",  "Paid placement. Labeled, and never selected or edited by the Newsroom."),
}
LABEL_ORDER = ["community", "syndicated", "sponsored"]


def wire_item_html(item):
    """One item on The Wire. The label is not decoration - a reader has to be able
    to tell in one glance who wrote this and whether anyone paid for it."""
    kind = item.get("type", "community")
    if kind not in LABELS:
        kind = "community"
    label = LABELS[kind][0]

    parts = ['<article class="wireitem">']
    parts.append('<span class="lbl %s">%s</span>' % (kind, label))
    parts.append("<h3>%s</h3>" % esc(item.get("headline", "")))

    if item.get("when_text") or item.get("where"):
        bits = [b for b in (item.get("when_text"), item.get("where")) if b]
        parts.append('<div class="whenwhere">%s</div>'
                     % " &middot; ".join(esc(b) for b in bits))

    if item.get("dek"):
        parts.append('<p class="dek">%s</p>' % esc(item["dek"]))

    for para in [p for p in item.get("body", "").split("\n") if p.strip()]:
        parts.append('<p class="para">%s</p>' % esc(para.strip()))

    meta = []
    if kind == "sponsored":
        payer = item.get("paid_by", "").strip()
        meta.append("<b>Paid content.</b> %s" % (
            ("Paid for by %s." % esc(payer)) if payer
            else "Paid placement, distributed on behalf of a client."))
    if item.get("source"):
        if item.get("source_url"):
            meta.append('Source: <a href="%s" target="_blank" rel="noopener"><b>%s</b></a>'
                        % (esc(item["source_url"]), esc(item["source"])))
        else:
            meta.append("Source: <b>%s</b>" % esc(item["source"]))
    if item.get("contact"):
        meta.append("Contact: %s" % item["contact"])
    if item.get("date"):
        meta.append("Filed %s" % esc(item["date"]))
    if meta:
        parts.append('<div class="wiremeta">%s</div>' % " &middot; ".join(meta))

    parts.append("</article>")
    return "".join(parts)


def build_wire(wire_data, takeover, now_la, now_utc, feed_ok, feed_total):
    """docs/wire.html, generated. It used to be hand-typed and carried nothing at
    all, so it sat empty with a frozen dateline. Now it is built from
    data/wire-items.json on every run like every other index page."""
    items = wire_data.get("items", [])
    email = wire_data.get("submissions_email", "").strip()
    title = "The Wire | Hollywood News Access"
    desc = ("Local Los Angeles items, syndicated coverage carried with attribution, "
            "and clearly labeled sponsored releases.")

    parts = [head_html(title, desc, "https://hollywoodnewsaccess.com/wire.html")]
    parts.append("<body>")
    parts.append(masthead_html(now_la))
    parts.append(nav_html("wire.html", takeover))
    parts.append(banner_html(takeover))
    parts.append('<div class="wrap"><div class="hero">')
    parts.append('<span class="kicker">The Wire</span>')
    parts.append("<h1>The Wire</h1>")
    parts.append("<p>Local Los Angeles, and everything that reaches this site by a path "
                 "other than our own newsroom. Three kinds of item run here, each labeled "
                 "so you can tell them apart at a glance.</p>")
    parts.append("</div></div>")
    parts.append('<main><div class="wrap">')

    parts.append('<p class="sect-note">%s</p>' % " ".join(
        '<span class="lbl %s">%s</span> %s' % (k, LABELS[k][0], esc(LABELS[k][1]))
        for k in LABEL_ORDER))

    if items:
        ordered = sorted(
            items,
            key=lambda i: (LABEL_ORDER.index(i.get("type", "community"))
                           if i.get("type") in LABELS else 0,
                           "" if not i.get("date") else i["date"]),
            reverse=False)
        ordered = sorted(ordered, key=lambda i: i.get("date", ""), reverse=True)
        parts.append("".join(wire_item_html(i) for i in ordered))
    else:
        parts.append('<div class="empty">Nothing on the wire yet. Items added to '
                     'data/wire-items.json appear here on the next hourly run &mdash; '
                     'nobody types into this page by hand.</div>')

    if email:
        parts.append('<div class="submit">')
        parts.append("<h3>Submit to The Wire</h3>")
        parts.append("<p>Local organizations, venues and publicists: send releases, event "
                     "listings and community notices to "
                     '<a href="mailto:%s"><b>%s</b></a>.</p>' % (esc(email), esc(email)))
        parts.append("<p>Include a date, a location and a named contact we can reach. "
                     "We run local items free. Paid distribution is a separate service and "
                     "anything paid for is labeled as such on this page.</p>")
        parts.append("</div>")

    parts.append("</div></main>")
    parts.append(footer_html(now_utc, feed_ok, feed_total))
    parts.append("""<script type="application/ld+json">
{"@context":"https://schema.org","@type":"CollectionPage","name":"The Wire","description":"%s","url":"https://hollywoodnewsaccess.com/wire.html","publisher":{"@type":"NewsMediaOrganization","name":"Hollywood News Access"}}
</script>""" % esc(desc))
    parts.append("</body>\n</html>")
    return "\n".join(p for p in parts if p) + "\n"


# --------------------------------------------------------------------------
# Wikimedia Commons photographs for our own reporting
# --------------------------------------------------------------------------
# Our stories had no pictures because we hold no wire subscription, and the
# standing rule forbids lifting images off the open web. Wikimedia Commons is
# the legitimate middle: real photographs of real people, freely licensed,
# with the author and licence published alongside each file.
#
# The bargain is attribution. Every photo sourced here renders its
# photographer and licence on the card - that is not decoration, it is the
# licence term, and dropping it would make the use infringing.
#
# What this will NOT do: generate an image of a real person. A synthetic
# photograph of Madonna on a news page is a fabricated document, and one of
# those ends a newsroom. Stories with no licensed photo keep the typographic
# plate instead.

class CommonsUnavailable(Exception):
    """Could not reach Wikimedia. Distinct from 'asked, nothing free existed' -
    one is a temporary fault to retry, the other is a permanent answer to cache."""


WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PHOTO_CACHE = os.path.join(DATA, "photo-cache.json")


def _api_get(params, endpoint=None):
    url = (endpoint or WIKIPEDIA_API) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        # Wikimedia requires a descriptive agent with contact. An anonymous
        # scraper gets blocked, and rightly.
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def commons_photo(subject):
    """Lead photograph for a subject, with the attribution its licence requires.

    Returns {url, author, licence, page} or None. Never raises - a photo is a
    nice-to-have and must never take the site build down."""
    try:
        meta = _api_get({
            "action": "query", "format": "json", "formatversion": "2",
            "titles": subject, "prop": "pageimages",
            "piprop": "original", "pilicense": "free", "redirects": "1",
        }, WIKIPEDIA_API)
        pages = meta.get("query", {}).get("pages", [])
        if not pages or "original" not in pages[0]:
            return None
        src = pages[0]["original"]["source"]

        bare = src.split("?", 1)[0].split("#", 1)[0]
        filename = "File:" + urllib.parse.unquote(bare.rsplit("/", 1)[-1])

        # Story art means a photograph. A logo or a video still is not one, and
        # an SVG wordmark on a news card looks like a placeholder.
        if not re.search(r"\.(jpe?g|png)$", bare, re.I):
            print("commons: %r lead image is %s, not a photograph - skipped"
                  % (subject, bare.rsplit(".", 1)[-1].lower()))
            return None
        info = _api_get({
            "action": "query", "format": "json", "formatversion": "2",
            "titles": filename, "prop": "imageinfo",
            "iiprop": "extmetadata|url",
        }, COMMONS_API)
        ipages = info.get("query", {}).get("pages", [])
        ex = {}
        if ipages and ipages[0].get("imageinfo"):
            ex = ipages[0]["imageinfo"][0].get("extmetadata", {}) or {}
        else:
            # Diagnostic: cannot reach this API from the build sandbox, so CI
            # has to report what it actually received.
            p0 = ipages[0] if ipages else {}
            print("  commons-debug %s -> missing=%s keys=%s"
                  % (filename, p0.get("missing"), sorted(p0.keys())[:6]))

        licence = (_strip_html(ex.get("LicenseShortName", {}).get("value", ""))
                   or _strip_html(ex.get("License", {}).get("value", "")))
        # Anything not clearly free is not ours to run.
        if licence and re.search(r"fair use|non-?free|copyright", licence, re.I):
            return None

        author = (_strip_html(ex.get("Artist", {}).get("value", ""))
                  or _strip_html(ex.get("Credit", {}).get("value", "")))
        if not author or not licence:
            # No identifiable photographer or no stated licence means we cannot
            # credit it properly, so we do not run it.
            print("commons: %r has no usable attribution - skipped" % subject)
            return None

        return {
            "url": bare,
            "author": author,
            "licence": licence,
            "page": _strip_html(ex.get("DescriptionUrl", {}).get("value", ""))
                    or "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(filename),
        }
    except Exception as exc:
        # Raised, not answered. The caller must retry next run rather than
        # record this as a verdict.
        print("commons lookup failed for %r: %s" % (subject, exc))
        raise CommonsUnavailable(str(exc))


def load_photo_cache():
    try:
        with open(PHOTO_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_photo_cache(cache):
    try:
        with open(PHOTO_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)
    except OSError as exc:
        print("could not write photo cache: %s" % exc)


def attach_photos(originated, dry_run=False):
    """Fill in a photo for any of our stories that names a photo_subject and
    does not already carry an image. Cached, so we ask Wikimedia once per
    subject rather than on every hourly run."""
    cache = load_photo_cache()
    changed = False
    for a in originated:
        if a.get("image"):
            continue
        subject = a.get("photo_subject")
        if not subject:
            continue
        if subject not in cache:
            if dry_run:
                continue
            try:
                found = commons_photo(subject)
            except CommonsUnavailable:
                # Leave it uncached so the next run tries again.
                print("commons: %-28s unreachable, will retry" % subject)
                continue
            cache[subject] = found or {}
            changed = True
            print("commons: %-28s %s" % (subject, "found" if found else "nothing free"))
        hit = cache.get(subject) or {}
        if hit.get("url"):
            a["image"] = hit["url"]
            a["photo_author"] = hit.get("author", "")
            a["photo_licence"] = hit.get("licence", "")
    if changed and not dry_run:
        save_photo_cache(cache)
    return originated


# --------------------------------------------------------------------------
# Our own RSS feed
# --------------------------------------------------------------------------
# We pull nine feeds and published none of our own, which meant every
# aggregator that moves stories around the internet - Google News, Apple News,
# Flipboard, SmartNews, MSN, Feedly, and every newsroom that watches rivals the
# way we watch ours - had no way to see us. A wire service that cannot be
# subscribed to is not on the wire.

def rfc822(datestr):
    """RSS wants RFC-822 dates. Our stories carry YYYY-MM-DD."""
    try:
        d = datetime.strptime(datestr, "%Y-%m-%d").replace(tzinfo=LA)
    except (ValueError, TypeError):
        d = datetime.now(LA)
    return d.strftime("%a, %d %b %Y %H:%M:%S %z")


def build_rss(ours, now_la):
    site = "https://hollywoodnewsaccess.com"
    items = []
    for a in ours[:40]:
        link = "%s/%s" % (site, a.get("link", "").lstrip("/"))
        enclosure = ""
        img = a.get("image", "")
        if img:
            if not img.startswith("http"):
                img = "%s/%s" % (site, img.lstrip("/"))
            # media:content is what aggregators read for the card image.
            enclosure = ('<media:content url="%s" medium="image"/>'
                         '<media:thumbnail url="%s"/>' % (esc(img), esc(img)))
        items.append(
            "<item>"
            "<title>%s</title>"
            "<link>%s</link>"
            "<guid isPermaLink=\"true\">%s</guid>"
            "<pubDate>%s</pubDate>"
            "<dc:creator>%s</dc:creator>"
            "<description>%s</description>"
            "%s"
            "</item>"
            % (esc(a.get("title", "")), esc(link), esc(link),
               rfc822(a.get("date", "")),
               esc(a.get("byline", "Hollywood News Access")),
               esc(a.get("dek", "")), enclosure))

    return ("""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<title>Hollywood News Access</title>
<link>%s</link>
<atom:link href="%s/feed.xml" rel="self" type="application/rss+xml"/>
<description>Celebrity news, red carpets, music, TV and movies from Hollywood.</description>
<language>en-us</language>
<copyright>Copyright %d Hollywood News Access</copyright>
<lastBuildDate>%s</lastBuildDate>
<ttl>15</ttl>
<image><url>%s/img/hna-share.png</url><title>Hollywood News Access</title><link>%s</link></image>
%s
</channel>
</rss>
""" % (site, site, now_la.year,
       now_la.strftime("%a, %d %b %Y %H:%M:%S %z"), site, site,
       "\n".join(items)))


def sync_social_tags(originated, now_la, dry_run=False):
    """Give every article page a complete share card.

    Pages carried og:title and og:type and nothing else, so a link shared
    anywhere rendered as plain text. This fills in description, url, image,
    site_name and the Twitter card from the same manifest that drives the
    index pages, so the card can never drift from the story."""
    by_file = {}
    for a in originated:
        by_file[a.get("url", "")] = a

    touched = []
    for name in sorted(os.listdir(DOCS)):
        if not name.endswith(".html") or name.startswith("_"):
            continue
        meta = by_file.get(name)
        if not meta:
            continue
        path = os.path.join(DOCS, name)
        with open(path, encoding="utf-8") as f:
            original = f.read()

        url = "https://hollywoodnewsaccess.com/" + name
        img = meta.get("image", "")
        if img and not img.startswith("http"):
            img = "https://hollywoodnewsaccess.com/" + img.lstrip("/")
        if not img:
            img = "https://hollywoodnewsaccess.com/img/hna-share.png"

        tags = [
            ('og:description', meta.get("dek", "")),
            ('og:url', url),
            ('og:image', img),
            ('og:site_name', "Hollywood News Access"),
            ('article:published_time', meta.get("date", "")),
        ]
        block = "".join(
            '<meta property="%s" content="%s">\n' % (k, esc(v)) for k, v in tags if v)
        block += ('<meta name="twitter:card" content="summary_large_image">\n'
                  '<meta name="twitter:title" content="%s">\n'
                  '<meta name="twitter:description" content="%s">\n'
                  '<meta name="twitter:image" content="%s">\n'
                  % (esc(meta.get("title", "")), esc(meta.get("dek", "")), esc(img)))

        # Replace any previous block so repeated runs do not stack duplicates.
        # Consume the surrounding newlines too. Stripping only the markers left
        # a blank line behind on every run, so the file grew a little whitespace
        # every hour forever.
        updated = re.sub(r"\n*<!-- SOCIAL -->.*?<!-- /SOCIAL -->\n*", "\n",
                         original, flags=re.S)
        marked = "<!-- SOCIAL -->\n" + block + "<!-- /SOCIAL -->"
        if "</head>" not in updated:
            continue
        updated = updated.replace("</head>", marked + "\n</head>", 1)

        if updated != original:
            touched.append(name)
            if not dry_run:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(updated)
    return touched


# --------------------------------------------------------------------------
# takeover sync on the hand-written pages
# --------------------------------------------------------------------------

def sync_galleries(dry_run=False):
    """Fill red-carpet galleries on article pages from Wikimedia Commons.

    A page asks for one by carrying a marker naming the people it wants:

        <!-- GALLERY: Madonna | Snoop Dogg | Taylor Swift -->
        <!-- /GALLERY -->

    Everything between the markers is replaced with a credited figure per
    subject. We cannot license tonight's wire photographs, so the gallery
    runs freely licensed portraits and says so plainly. A subject Commons
    has nothing free for is dropped rather than faked, and a name that
    cannot be reached is left for the next run instead of being cached as
    a miss.
    """
    cache = load_photo_cache()
    changed = False
    touched = []

    for name in sorted(os.listdir(DOCS)):
        if not name.endswith(".html") or name.startswith("_"):
            continue
        path = os.path.join(DOCS, name)
        with open(path, encoding="utf-8") as f:
            original = f.read()

        m = re.search(r"<!-- GALLERY:(.*?)-->(.*?)<!-- /GALLERY -->",
                      original, re.S)
        if not m:
            continue

        subjects = [s.strip() for s in m.group(1).split("|") if s.strip()]
        figures = []
        for subject in subjects:
            if subject not in cache:
                if dry_run:
                    continue
                try:
                    found = commons_photo(subject)
                except CommonsUnavailable:
                    print("gallery: %-24s unreachable, will retry" % subject)
                    continue
                cache[subject] = found or {}
                changed = True
                print("gallery: %-24s %s"
                      % (subject, "found" if found else "nothing free"))
            hit = cache.get(subject) or {}
            if not hit.get("url"):
                continue
            credit = hit.get("author", "")
            licence = hit.get("licence", "")
            figures.append(
                '  <figure class="shot">\n'
                '    <img src="%s" alt="%s" loading="lazy">\n'
                '    <figcaption><b>%s</b>'
                '<span>%s &middot; Wikimedia Commons &middot; %s</span>'
                '</figcaption>\n'
                '  </figure>'
                % (esc(hit["url"]), esc(subject), esc(subject),
                   esc(credit), esc(licence))
            )

        if not figures:
            continue

        block = ('<!-- GALLERY:%s-->\n<div class="shots">\n%s\n</div>\n'
                 '<!-- /GALLERY -->'
                 % (m.group(1), "\n".join(figures)))
        updated = original[:m.start()] + block + original[m.end():]

        if updated != original and not dry_run:
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated)
            touched.append(name)

    if changed and not dry_run:
        save_photo_cache(cache)
    return touched


def sync_takeover_markers(active_ids, now_la, dry_run=False):
    """The article pages, calendar, press room and wire page are hand-written and
    wire.py does not regenerate them. Their nav still has to gain and lose the
    takeover item on the same schedule, so each carries a marked block:

        <!-- TAKEOVER:id --> ...nav item... <!-- /TAKEOVER:id -->

    While the takeover is live the block holds the nav item. Once it expires the
    block is emptied - markers stay put as the anchor for the next takeover. This
    is why no takeover ever needs removing by hand.

    Same pass also restamps the dateline. Those pages carried a hand-typed date
    that nobody remembers to change, so the calendar and press room were reading
    eight days old on a live news site. Now every page says today."""
    touched = []
    dateline_re = re.compile(
        r'(<div class="dateline">Los Angeles, California &middot; )[^<]*(</div>)')
    today = now_la.strftime("%A, %B %-d, %Y")
    pattern = re.compile(r"<!-- TAKEOVER:([a-z0-9-]+) -->(.*?)<!-- /TAKEOVER:\1 -->", re.S)
    for name in sorted(os.listdir(DOCS)):
        if not name.endswith(".html"):
            continue
        path = os.path.join(DOCS, name)
        with open(path, encoding="utf-8") as f:
            original = f.read()

        def replace(m):
            tid, inner = m.group(1), m.group(2)
            # A marker used to only fill for its own id, which meant every new
            # takeover needed fresh markers hand-placed into every hand-written
            # page before its nav item could appear. Any marker now carries
            # whichever takeover is live, so a new event works on day one.
            want = active_ids.get(tid) or (next(iter(active_ids.values())) if active_ids else "")
            return "<!-- TAKEOVER:%s -->%s<!-- /TAKEOVER:%s -->" % (tid, want, tid)

        updated = pattern.sub(replace, original)
        updated = dateline_re.sub(r"\g<1>" + today + r"\g<2>", updated)
        if updated != original:
            touched.append(name)
            if not dry_run:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(updated)
    return touched


def write_status_log(status, now_utc, kept, dropped_old):
    ok = [s for s in status if s[2] == "ok"]
    lines = ["", "## Run %s UTC" % now_utc.strftime("%Y-%m-%d %H:%M"), ""]
    lines.append("Feeds answered: %d of %d. Items kept inside the %d-day window: %d "
                 "(%d dropped as older)." % (len(ok), len(status), WIRE_WINDOW_DAYS,
                                             kept, dropped_old))
    lines.append("")
    lines.append("| Feed | Status | Items | With photo | Note |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, _url, state, note, count, photos in status:
        lines.append("| %s | %s | %d | %d | %s |" % (name, state, count, photos, note or "-"))
    with open(STATUS_LOG, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    now_utc = datetime.now(timezone.utc)
    now_la = now_utc.astimezone(LA)

    with open(os.path.join(DATA, "feeds.json"), encoding="utf-8") as f:
        feeds = json.load(f)["feeds"]
    with open(os.path.join(DATA, "originated.json"), encoding="utf-8") as f:
        originated = json.load(f)["articles"]
    with open(os.path.join(DATA, "event-takeovers.json"), encoding="utf-8") as f:
        takeovers = json.load(f)["takeovers"]

    live = active_takeover(takeovers, now_la.date())

    items, status = collect(feeds, dry_run)
    feed_ok = sum(1 for s in status if s[2] == "ok")

    cutoff = now_utc - timedelta(days=WIRE_WINDOW_DAYS)
    fresh, dropped_old = [], 0
    seen_links = set()
    for it in items:
        if it["published"] < cutoff:
            dropped_old += 1
            continue
        if it["link"] in seen_links:
            continue
        seen_links.add(it["link"])
        fresh.append(it)
    fresh.sort(key=lambda i: i["published"], reverse=True)

    by_section = {slug: [] for slug, _l, _b, _k in SECTIONS}
    unrouted = []
    for it in fresh:
        slug = classify(it)
        if slug:
            it["section_label"] = SECTION_LOOKUP[slug][0]
            by_section[slug].append(it)
        else:
            it["section_label"] = "The Wire"
            unrouted.append(it)

    ours_by_section = {slug: [] for slug, _l, _b, _k in SECTIONS}
    ours_all = []
    for a in originated:
        entry = {
            "title": a["title"], "link": a["url"], "dek": a.get("dek", ""),
            "summary": a.get("dek", ""), "byline": a.get("byline", ""),
            "tag": a.get("tag", "Hollywood News Access"), "source": "Hollywood News Access",
            "image": a.get("image", ""), "image_focus": a.get("image_focus", ""), "date": a.get("date", ""),
            "photo_subject": a.get("photo_subject", ""),
            "photo_author": a.get("photo_author", ""),
            "photo_licence": a.get("photo_licence", ""),
        }
        ours_all.append(entry)
        if a.get("section") in ours_by_section:
            ours_by_section[a["section"]].append(entry)
    ours_all.sort(key=lambda a: a.get("date", ""), reverse=True)

    # Give our own reporting a photograph where a free, properly licensed one
    # exists. Cached, so this costs one Wikimedia call per new subject.
    attach_photos(ours_all, dry_run)
    for slug in ours_by_section:
        attach_photos(ours_by_section[slug], dry_run)

    wire_data = {}
    wire_items_path = os.path.join(DATA, "wire-items.json")
    if os.path.exists(wire_items_path):
        try:
            with open(wire_items_path, encoding="utf-8") as f:
                wire_data = json.load(f)
        except (ValueError, OSError) as exc:
            # A typo in the JSON must not take the whole site build down with it.
            print("wire-items.json unreadable, skipping The Wire: %s" % exc)
            wire_data = {}

    home_wire = fresh[:HOME_WIRE_COUNT] if fresh else []
    pages = {"index.html": build_index(ours_all, home_wire, live, now_la, now_utc,
                                       feed_ok, len(status))}
    for slug, _label, _blurb, _kw in SECTIONS:
        pages[slug + ".html"] = build_section(slug, ours_by_section[slug], by_section[slug],
                                              live, now_la, now_utc, feed_ok, len(status))
    pages["wire.html"] = build_wire(wire_data, live, now_la, now_utc, feed_ok, len(status))
    pages["feed.xml"] = build_rss(ours_all, now_la)

    written = []
    for name, content in pages.items():
        path = os.path.join(DOCS, name)
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        if existing == content:
            continue
        written.append(name)
        if not dry_run:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    active_ids = {}
    if live:
        active_ids[live["id"]] = ('<li><a href="%s" style="color:var(--signal)">%s</a></li>'
                                  % (live["link"], live["nav_label"]))
    touched = sync_takeover_markers(active_ids, now_la, dry_run)
    social = sync_social_tags(originated, now_la, dry_run)
    shots = sync_galleries(dry_run)
    if social:
        print("share cards refreshed on: %s" % ", ".join(social))

    if not dry_run:
        write_status_log(status, now_utc, len(fresh), dropped_old)

    print("feeds ok: %d/%d" % (feed_ok, len(status)))
    for name, _u, state, note, count, photos in status:
        print("  %-28s %-8s items=%-3d photos=%-3d %s" % (name, state, count, photos, note))
    print("wire items kept: %d (dropped as older than %d days: %d, unrouted: %d)"
          % (len(fresh), WIRE_WINDOW_DAYS, dropped_old, len(unrouted)))
    print("takeover active: %s" % (live["id"] if live else "none"))
    print("pages rewritten: %s" % (", ".join(written) if written else "none (no change)"))
    print("takeover markers updated on: %s" % (", ".join(touched) if touched else "none"))
    if dry_run:
        print("DRY RUN - nothing written")


if __name__ == "__main__":
    main()
