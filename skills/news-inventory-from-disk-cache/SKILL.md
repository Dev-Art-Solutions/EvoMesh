---
name: news-inventory-from-disk-cache
description: When news_fetch's live tool view truncates (~5000 chars) and you need the complete current headline inventory to assess every headline — read the on-disk cache file directly as the source of truth.
---

# news-inventory-from-disk-cache

When `news_fetch` live output is truncated by the tool wrapper (wraps at ~5000 chars) and you need the full current headline inventory to assess every headline's market impact.

## When to use
- Running news-impact-analysis but the live news_fetch output is cut off mid-list.
- `from_cache:true` still shows the truncated view inside the tool wrapper.
- No `news_fetch` API tool is directly reachable in the workspace (config feeds may be empty; config.json may lack tool bindings).

## The path
The tool's JSON cache is written to disk so it can be read as a plain file.
- On this workspace the cache is at `cache/news_cache.json` (relative to job root).
- On the sandboxed python tool environment it's elsewhere; locate it with:
  `subprocess.run(['find', os.getcwd(), '-name', 'news_cache.json', '-maxdepth', '4'])`
- Read it with `json_read` (canonical JSON) or `read` (raw, cheaper).

## What's in the cache
Each headline entry carries the fields you need for analysis:
- `text` (headline)
- `instrument` (e.g. "EURUSD")
- `direction` (bullish/bearish/neutral)
- `confidence` (low/medium/high)
- `timestamp` (headline's own publish time)
- `score` (0-1 numeric)
- `reported`, `source`, `rationale`
- `cache_timestamp` / `min_confidence` at top level — the report bar.

## Workflow
1. **Get inventory from the file, not the tool.** The on-disk file is the canonical current snapshot — no truncation.
2. **Note the cache timestamp** — this is the moment the inventory was frozen. It's your dedup baseline.
3. **Dedup against prior assessments.** Load `.assessed_headlines.json` (prior-assessment log). Any headline already in that log = already assessed = skip. If the cache timestamp == the last log entry's timestamp, you're a consistent round — nothing new since last round.
4. **Check for post-assessment arrivals.** Headlines whose own `timestamp` is *after* the last assessment log entry are genuinely new. Only those are candidates for a fresh assessment.
5. **Assess only the new/clearing ones.** Apply min_confidence (cache's top-level value) — silence is correct when nothing clears it.

## Notes
- The tool wrapper may still show truncated output; trust the file.
- Headlines without `instrument` or `direction` fields need a manual read (via news body / web research) to assess — but most have them pre-populated.
- `from_cache:true` doesn't bypass the wrapper's truncation; the file does.
