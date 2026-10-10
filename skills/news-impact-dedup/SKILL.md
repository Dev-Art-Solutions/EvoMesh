---
name: news-impact-dedup
description: When running news-impact-analysis, deduping the fetched batch against the prior-assessment log to decide whether anything is genuinely new before assessing anything.
---

## When news_fetch truncates its view, use the logs to dedup

The `news_fetch` tool wrapper truncates output (~5000 chars), so you never see the
full headline inventory in one tool view. But two side-effect files capture what
was already assessed and what the last batch was:

- `.news_reasoning.log` — one line per assessed headline (the skill's audit trail).
- `.news_reasoning_cache.json` — JSON array of the last fetched batch (each entry has
  `headline`, `verdict` optionally `"silent"`, `instrument`, `direction`, `confidence`).

Both are written by prior runs of this workflow; they're the source of truth, not
the `silent`-marker on a previous fetch.

## Procedure

1. Read `.news_reasoning_cache.json`. Its `headline` field is the current batch
   (the fetch returned it).
2. For each headline, grep `.news_reasoning.log` (search via the tool or a short
   python one-liner).
3. If **all** batch headlines are already in the log → the batch is a repeat.
   Nothing new. Report silence. Stop.
4. If **some** headline is missing from the log → it's new. Assess it normally:
   - pick the configured instrument it plausibly affects,
   - judge direction (bullish/bearish/neutral),
   - judge confidence (low/medium/high),
   - only report items at/above config.json's `min_confidence`,
   - append one line to `.news_reasoning.log` recording headline + verdict +
     instrument + direction + confidence.

## Notes

- Don't rely on a previous cycle's `silent` marker — the same batch is often
  returned repeatedly. Rely on the log + cache instead.
- The two files must be consistent with each other; if a headline appears in the
  cache but not the log, treat it as new and record it.
