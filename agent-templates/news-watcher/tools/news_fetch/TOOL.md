---
name: news_fetch
description: Fetch the most recent financial news headlines from RSS feeds. Optionally pass a JSON object to override the configured feeds, filter by keyword, or change how many headlines come back.
command: python "{tool_dir}/scripts/news_fetch.py"
parameters:
  - name: request
    description: >
      Optional JSON object: {"feeds": ["https://..."], "keywords": ["gold"],
      "limit": 10}. Every field is optional; feeds/keywords/limit default to
      config.json beside this template if omitted.
    required: false
---

Read-only. Returns a JSON list of {"title", "link", "published"} objects,
most recent feed order, already filtered by keyword if any were given.
