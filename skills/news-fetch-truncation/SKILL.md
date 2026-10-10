---
name: news-fetch-truncation
description: What to do when a news_fetch result comes back cut off, so you can see every headline in the window you are assessing without guessing at cache files.
---

## Why the result is cut off

Every tool result is capped at 4000 characters (`harness.tool_result_chars`).
`news_fetch` prints one `{"title", "published"}` line per headline, so roughly
25-30 headlines fit. A bigger `limit` is cut at a line boundary, and the result
ends with `[... N more lines withheld, this tool has no offset, so ask for
less: narrow its arguments ...]`. The data is not damaged. You asked for more
than one result can show.

Calling it again with the same request returns the same cut-off view. Do not
retry and expect a different result.

## Getting the full window

Ask for less per call instead of reading files around the tool:

1. Narrow by topic: `{"keywords": ["gold"], "limit": 20}`, then the next
   instrument's keywords. Each call fits on its own.
2. Narrow by time: `{"from_cache": true, "since_hours": 6}` returns only what
   arrived since your last cycle. That is usually just the new headlines.
3. Leave out `"links": true` unless you really need URLs. It doubles every line.

If you do need the raw history, it is `scripts/.news_cache.jsonl` in your
playground (beside `config.json`): one JSON object per line with `title`,
`link`, `published`, `source`, `fetched_at`. Read it with `read` and an
`offset`. It holds only fetched headlines. It has no assessments, verdicts or
`reported` flags, and no other cache file exists (`_cache/news_cache.json`,
`.news_reasoning_cache.json` and the like are made up).

## Knowing what is already assessed

Do not keep or grep your own logs for dedup. The `recent_reports` tool returns
what you have already reported. A headline that is already there, or is
`published` before your last cycle, is not new. Headlines you looked at and
did not report need no record: if they did not clear `min_confidence` then,
they will not now. See the news-impact-analysis skill for how to judge and
report.
