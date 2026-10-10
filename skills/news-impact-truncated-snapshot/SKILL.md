---
name: news-impact-truncated-snapshot
description: When news_fetch keeps returning an identical cached snapshot that gets truncated in the tool's view, so you can't re-read the full headline list.
---

## Situation
`news_fetch` returns the same cached snapshot every call, and the tool wrapper truncates the output so you never see the full headline list. Repeated `news_fetch` calls only re-truncate identically — it's not a live feed in a sandboxed tool environment.

## Do NOT do
- Keep calling `news_fetch` expecting different results.
- Keep re-reading files that get truncated.
- Fabricate headlines or "new" signals to justify a report.

## What actually works
1. Read the news reasoning log (`scripts/.news_reasoning.log`) — it stores the full fetched headlines (e.g. Yahoo + Forexfactory) once, in readable form, so you can review the complete list without re-fetching.
2. Check `scripts/.news_reports.log` for already-assessed items (dedup source of truth).
3. Dedup against recent reports and the reasoning log before assessing.
4. Assess only against configured instruments from config.json; apply min_confidence; report only NEW headlines that clear the bar.

## Judgment
The gold-at-record-high cluster appeared as multiple headlines but is the same already-assessed event — not new. If everything instrument-relevant is already assessed, silence is correct.
