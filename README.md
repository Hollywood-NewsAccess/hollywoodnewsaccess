# Hollywood News Access

The center of the entertainment universe. Celebrity news, red carpets, music, TV and movies.

- `wire.py` rebuilds the site every hour from the outlets in `data/feeds.json`.
- Our own stories go in `docs/` and get listed in `data/originated.json`.
- `.github/workflows/` holds the hourly rebuild and the sitemap and search-engine pings.
- Standing rules for anyone working here, human or agent: `CLAUDE.md`.
