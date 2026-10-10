---
name: news-truncation-bypass
description: When news_fetch truncates its live tool view (wraps at ~5000 chars) and you need the full current headline inventory to assess every headline's market impact.
---

## news-truncation-bypass

### Situation
`news_fetch` returns headlines but the tool wrapper truncates the visible output at ~5000 characters, so the last ~26 headlines are hidden and you cannot see the full list of current headlines.

### Fix
Don't rely on the tool's live output. The cache is the source of truth:

1. `news_fetch` stores results at `_cache/news_cache.json` under the tool's directory (path relative to the job root).
2. Read it directly with a different tool — either `read` or `python3` to `json.load` and iterate — instead of `json_read` (which also truncates at ~5000 chars).
3. The cache holds a list of `{text, instrument, direction, confidence, assessed_at}` items — the full current headline inventory plus prior assessments.

### Notes
- This works because the cache is written by `news_fetch` before the tool returns; the truncation is only in the *tool's own view* of its output, not in the file on disk.
- Compare cache items against `recent_reports` / your own assessed history to find what's genuinely new (items whose `text`/topic you haven't already judged).
- This is specific to the news_fetch tool's caching convention; other tools may not persist the same way.
