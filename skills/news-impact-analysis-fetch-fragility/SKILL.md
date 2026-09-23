---
name: news-impact-analysis-fetch-fragility
description: How to run the news-impact-analysis recurring goal when news_fetch returns output that gets truncated by the tool wrapper.
---

When calling `news_fetch` for the news-impact-analysis goal, the raw fetched JSON sometimes gets truncated in the tool's own output display. To reliably get the full list of headlines:

1. Re-call `news_fetch` a second time if the first result is truncated.
2. If still truncated, append the content you did capture to `scripts/.news_fetch.log`.
3. Read the rest from that file with `read` (with offset if needed).
4. If the log file doesn't exist, save the full news payload there first, then read it.

Never re-run a fresh fetch cycle expecting new data if the log shows the current cycle was already processed. Deduplicate by reading `scripts/.news_reasoning.log` (scratch, per-cycle reasoning) and `scripts/.news_reports.log` (durable, what was actually told a human) before re-analyzing.

Always write both logs: `.news_reasoning.log` for silent per-headline reasoning, `.news_reports.log` for the timestamped reported lines. Both files may be truncated in display — use `read` with an offset to see the full contents.
