---
name: news-impact-live-inventory
description: When news_fetch returns a cached snapshot that gets truncated in the tool's view, you can't read the full headline inventory to dedup new headlines against your prior assessments. Use the live (non-cache) fetch as the canonical current inventory and read each headline's body via the live tool view.
---

When running news-impact-analysis and the tool's view keeps truncating the cached snapshot:

1. `news_fetch({"from_cache": true})` first — but understand this returns the script's on-disk `._cache/news_cache.json`, which can be stale. The cache file's `assessed[]` array is the true dedup source (already-assessed `text` values; `reported:true` means "reported:false" means not yet told the human).
2. The live `news_fetch({"limit":40})` (no cache) returns the SAME truncated ~3000-char view — but the cache file on disk reflects live state. Prefer the live fetch for the headline inventory.
3. To read a full headline body (news_fetch truncates titles to 10 words, so assess only from body text), fetch that specific article and read its body.
4. Dedup: for each live headline, check if its text exists in the cache's `assessed[]`. If already assessed, skip unless new material info.
5. Assess only items at/above config.json `min_confidence` (default 0.4). Report via send_results. Silence is correct when nothing clears the bar.

Key gotcha: the on-disk cache is often behind; the live `news_fetch` (no from_cache) is the freshest inventory despite truncation. Always read article bodies for a real judgment, never just truncated titles.
