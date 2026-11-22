#!/usr/bin/env python3
"""
tools/adsense.py - the one switch that turns Google ads on or off site-wide.

WHAT THIS DOES, PLAIN ENGLISH
-----------------------------
Google AdSense needs one small line of code (the "Auto ads tag") inside the
<head> of every page, plus a file called ads.txt at the root of the site that
tells advertisers "yes, this site really sells its space through Google".

The generator already adds the tag to the pages it rebuilds every hour. This
script does the same for every page, including the hand-written article
pages the generator never touches, so nothing is missed.

It reads ONE setting: ADSENSE_PUB in wire.py (top of the file).
  - ADSENSE_PUB set   -> the tag goes between <!-- ADSENSE:START --> and
                         <!-- ADSENSE:END --> in the <head> of every page,
                         and ads.txt is written.
  - ADSENSE_PUB empty -> the tag is removed from every page and the ads.txt
                         this script wrote is deleted. Ads are off.

Safe to run as often as you like: running it twice changes nothing the
second time.

Pages it leaves ad-free on purpose: press releases and sponsored pages (a
client paid for that page; we do not sell ads inside it), and any page that
carries <meta name="ads" content="off">.

HOW TO RUN IT
-------------
  python3 tools/adsense.py            switch on/off to match ADSENSE_PUB
  python3 tools/adsense.py --check    report what would change, write nothing
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "wire.py")
PAGES = os.path.join(ROOT, "docs")
ADS_TXT = os.path.join(PAGES, "ads.txt")
MANIFEST = os.path.join(ROOT, os.path.join("data", "originated.json"))
SKIP_DIRS = set(["img", "video", "assets"])

BLOCK_RE = re.compile(r"<!-- ADSENSE:START -->.*?<!-- ADSENSE:END -->\n?", re.S)
NO_ADS_RE = re.compile(r'<meta\s+name="ads"\s+content="off"', re.I)


def read_pub():
    """Read ADSENSE_PUB straight out of the generator's source, so there is
    exactly one place to set it. Returns pub-1234... or ""."""
    with open(SOURCE, encoding="utf-8") as f:
        m = re.search(r"""^ADSENSE_PUB\s*=\s*["']([^"']*)["']""", f.read(), re.M)
    raw = m.group(1) if m else ""
    digits = re.sub(r"^(ca-)?(pub-)?", "", raw.strip())
    if raw.strip() and not re.fullmatch(r"\d{10,20}", digits):
        sys.exit("ADSENSE_PUB looks wrong (%r). It should look like pub-1234567890123456." % raw)
    return ("pub-" + digits) if digits else ""


def block_for(pub):
    return ('<!-- ADSENSE:START -->\n<script async src="https://pagead2.googlesyndication.com/'
            'pagead/js/adsbygoogle.js?client=ca-%s" crossorigin="anonymous"></script>\n'
            '<!-- ADSENSE:END -->\n' % pub)


def paid_pages():
    """Pages a client paid for, from the story manifest: press releases and
    anything marked sponsored. These stay ad-free."""
    out = set()
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    rows = data.get("articles") or data.get("stories") or data.get("releases") or []
    for a in rows if isinstance(rows, list) else []:
        label = " ".join(str(a.get(k, "")) for k in ("kind", "tag", "type", "label")).lower()
        if a.get("sponsored") or "release" in label or "sponsored" in label:
            url = str(a.get("url", "")).lstrip("/")
            if url:
                out.add(url)
    return out


def is_paid(rel, text, paid):
    name = os.path.basename(rel)
    if rel in paid or name in paid:
        return True
    if re.search(r"^pr-|press-release|official-release", name) or rel.startswith("press/"):
        return True
    return bool(NO_ADS_RE.search(text))


def pages():
    for folder, dirs, files in os.walk(PAGES):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".html"):
                path = os.path.join(folder, name)
                yield path, os.path.relpath(path, PAGES).replace(os.sep, "/")


def main():
    check = "--check" in sys.argv
    pub = read_pub()
    paid = paid_pages()
    changed, skipped = [], []
    for path, rel in pages():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if "</head>" not in text:
            continue
        new = BLOCK_RE.sub("", text)
        if pub and not is_paid(rel, text, paid):
            new = new.replace("</head>", block_for(pub) + "</head>", 1)
        elif pub:
            skipped.append(rel)
        if new != text:
            changed.append(rel)
            if not check:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)

    line = "google.com, %s, DIRECT, f08c47fec0942fa0\n" % pub if pub else ""
    old = open(ADS_TXT, encoding="utf-8").read() if os.path.exists(ADS_TXT) else None
    if pub and old != line:
        changed.append("ads.txt")
        if not check:
            with open(ADS_TXT, "w", encoding="utf-8") as f:
                f.write(line)
    elif not pub and old is not None and all(
            l.startswith("google.com,") for l in old.splitlines() if l.strip()):
        changed.append("ads.txt (removed)")
        if not check:
            os.remove(ADS_TXT)

    print("AdSense is %s." % ("ON for " + pub if pub else "OFF (ADSENSE_PUB is empty)"))
    print("%s %d file(s)%s" % ("Would change" if check else "Changed", len(changed),
                                (": " + ", ".join(changed[:12]) + (" ..." if len(changed) > 12 else ""))
                                if changed else ""))
    if skipped:
        print("Kept ad-free (paid pages): %s" % ", ".join(skipped))


if __name__ == "__main__":
    main()
