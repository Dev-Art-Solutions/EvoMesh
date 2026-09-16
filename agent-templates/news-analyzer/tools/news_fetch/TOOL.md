---
name: news_fetch
description: Fetch the most recent financial news headlines -- RSS/Atom where a source has a feed, and a small site-specific HTML scrape for finance.yahoo.com and forexfactory.com, which do not. Optionally pass a JSON object to override the configured feeds, filter by keyword, change how many headlines come back, or read back cached history instead of fetching live.
command: python "{tool_dir}/scripts/news_fetch.py"
parameters:
  - name: request
    description: >
      Optional JSON object: {"feeds": ["https://..."], "keywords": ["gold"],
      "limit": 10, "from_cache": false, "since_hours": 24}. Every field is
      optional; feeds/keywords/limit default to config.json beside this
      template if omitted. Set "from_cache": true to skip the network
      entirely and read back headlines already seen over the retention
      window (config.json's "cache_days", default 3) instead of a live
      snapshot; add "since_hours" to narrow that further, e.g. the last day.
    required: false
---

Read-only. Returns a JSON list of {"title", "link", "published"} objects,
most recent first, already filtered by keyword if any were given. Every
live fetch (from_cache omitted or false) also feeds a durable cache beside
this template, so nothing seen is lost the moment a feed's own "latest"
window moves past it -- ask with "from_cache": true for anything gathered
over the last "cache_days" days (or "since_hours" hours) instead of only
what a source is showing right now.
