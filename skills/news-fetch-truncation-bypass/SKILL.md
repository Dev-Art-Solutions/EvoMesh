---
name: news-fetch-truncation-bypass
description: When news_fetch truncates live output and you need the full current headline inventory to assess headlines' impact — the cached JSON is your source, and from_cache:true forces a fresh snapshot.
---

## news_fetch live output truncation bypass

When news_fetch truncates its live tool view (wraps at ~3000 chars), you can't see
all current headlines. The full current inventory lives in `_cache/news_cache.json`
under `headlines[]` (each has `title`, `url`, `source`).

**Bypass procedure:**

1. Run `python3 -c "import json; d=json.load(open('_cache/news_cache.json')); [print(i,'|',h['title']) for i,h in enumerate(d['headlines'],1)]"`
   to get the current headline inventory. The cache is authoritative even when the
   live fetcher view is truncated.

2. If the cache appears stale (headlines empty or stale), fetch fresh with
   `{"from_cache": true, "limit": 20}`. The fetcher returns a fresh snapshot even
   though the live view truncates the printed output.

3. Dedup against `_cache/news_cache.json` `assessed[]` (prior log, dated run).
   Ignore anything already assessed.

4. Assess the genuinely new ones per the report bar (confidence >= 0.3).

5. Save the new assessments into `assessed[]` (append entries with text, instrument,
   direction, confidence, rationale, source, and score; set `reported: false` unless the
   report was actually sent).

The tool wrapper truncates output at ~3000 chars but does NOT alter the cache — the
cache is always the real state.
