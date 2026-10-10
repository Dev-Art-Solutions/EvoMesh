---
name: news-impact-analysis-disk-cache
description: When news_fetch's tool view truncates its output (~5000 chars), read the on-disk cache file _cache/news_cache.json directly as the canonical current headline inventory for the recurring news-impact-analysis goal.
---

# news-impact-analysis-disk-cache

## The problem
`news_fetch` (the built-in tool, no config.json) truncates its live output in the tool's view at ~5000 chars. You cannot read the full current headline list to dedup/assess every headline from the tool view alone.

## Source of truth
The current snapshot lives on disk at `_cache/news_cache.json`. Read it directly with the `read` tool. It contains every configured feed's latest headlines. Each entry has: `title`, `url`, `published` (ISO8601, local tz, e.g. "2026-07-09T11:16:00+01:00"), `summary`, `topics`, and sometimes `body` (the truncated live-fetch article text) and `keywords` (from body scraping).

## Sequence
1. **Read the cache**: `read` on `_cache/news_cache.json`. This gives you the FULL current inventory (unlike the tool view).
2. **Dedup**: Read `_assessed_headlines.json` (keys = exact headline titles). Skip any title already there. (Cache keys are deduped to the 10 most recent per feed, so a headline appearing here but not assessed is genuinely new; a headline in both is "seen and assessed".)
3. **Assess new headlines**: For each headline not in the assessment log, read its `summary` (and `body` if present, which holds live data). Use the news-impact-analysis skill to judge instrument/direction/confidence.
4. **Report only those clearing min_confidence** (3 = high, 2 = medium, 1 = low). Report the ones above the bar.

## Notes
- Headline `published` times tell you recency — the 3 most recent (~6h ago) are usually the new ones vs. prior assessments.
- The `body` field is the truncated live tool-view text of that specific article — often contains the freshest numbers.
- The built-in news_fetch tool has NO config.json; the tool defaults feeds (gold, AUDUSD, oil, GBPUSD).
- Update `_assessed_headlines.json` after assessing new headlines.
