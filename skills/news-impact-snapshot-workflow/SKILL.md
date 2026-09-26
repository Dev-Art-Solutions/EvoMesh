---
name: news-impact-snapshot-workflow
description: How to run the news-impact-analysis goal when news_fetch truncates its output and you need the full headline inventory plus a proper disambiguation against already-assessed items.
---

## Goal
Get the complete list of fresh financial headlines and assess them for instrument/direction/confidence, even though the news_fetch tool wrapper truncates its output.

## The truncation problem
news_fetch (the wrapper, which just calls news_fetch_v2) returns output truncated at ~32 items. There is no `offset`/`limit`/`count` parameter on news_fetch. The output header says something like `# Total N | Showing 1-32 of N`.

## Getting the full inventory
Two probes, in order:
1. `{"from_cache": true, "limit": 100}` — the cached snapshot has the SAME truncation point, but the header uses **offsets** (`# Total N | Showing offset-N-offset(M-N)` instead of `1-N`). This gives you N (the full count) cheaply and reliably, with no network.
2. The live feed (`{"limit": 100}` or `{"since_hours": 48, "limit": 100}`) then gives the actual fresh headlines — but still only the first ~32.

## Decoding the offsets
The cached header shows two numbers: `start` and `start+M-1` (M = items shown, ~32). From two different cached fetches, `N - start2` = the number of items in the **tail** that the 32-item head cut off. Read that many lines from the *live* feed to capture everything without overlap.

## After you have headlines
1. Read `config.json` for watchlist + `min_confidence` (default `medium`).
2. Check `scripts/.news_reports.log` (oldest first) — that's the dedupe list. If two news_fetch runs share the same header string, they're the same snapshot (no new items).
3. Cross-reference each new headline against existing log entries to avoid re-assessing stale ones.
4. Assess per news-impact-analysis skill: instrument/direction/confidence. Apply Rule 2 (macro/regional news = neutral = don't report), Rule 4 (flows/dispositions like ETF inflows count as real catalysts), Rule 3 (price quotes = no alpha).
5. Only report headlines at/above min_confidence. Silence is correct when nothing clears the bar.
6. Append to `.news_reports.log`.

## When silence is correct
If after this all remaining new headlines are either already-assessed stale items, or news that is macro/regional/price-quote (Rule 2), you report nothing — just append the log entry.
