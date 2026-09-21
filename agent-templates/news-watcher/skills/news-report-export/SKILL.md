---
name: news-report-export
description: A human asks for headlines as a file instead of chat text -- "the last 10 news as a PDF", "export this to Excel", "send me a CSV of today's gold news". Two tool calls in order, then the exact reply shape that hands the file back.
---

Two calls, in this order -- never try to build the file's content yourself,
and never skip straight to `document_write` with invented headlines.

1. `news_fetch` for the headlines. A plain "last N news" is a live fetch
   (default limit 10, or whatever N was asked); "today's" / "last few
   hours" / anything about a specific window is history, so use
   `{"from_cache": true, "since_hours": <n>}` instead (see `news-triage` for
   the full live-vs-cache rule -- it applies here exactly the same, the only
   difference is what happens to the result afterward).
2. `document_write` with the headlines as one table. Pick the file's
   extension from what was actually asked (`.pdf` for "PDF", `.xlsx` for
   "Excel"/"spreadsheet", `.csv` for "CSV", `.docx` for "Word"/"document" --
   default to `.pdf` if the human did not say):

   ```json
   {
     "path": "news-2026-09-21.pdf",
     "title": "Latest headlines",
     "headers": ["Headline", "Published", "Link"],
     "rows": [["...title...", "...published...", "...link..."], ...]
   }
   ```

   One row per headline `news_fetch` returned, in the same order. `title`
   is optional and ignored for `.csv`/`.xlsx` (they have no heading concept)
   -- still safe to pass it every time.

Then reply with **only** a line naming the file, exactly:

```
FILE: news-2026-09-21.pdf
```

That line (not a description of the file, not "I've created a PDF for
you") is what hands the file back to the human -- Telegram uploads it as a
real document, the desktop chat panel renders it as a link. A relative path
is enough; it is resolved against this agent's own workspace, where
`document_write` already wrote it. Skipping this line, or paraphrasing it
in prose instead of a bare `FILE:` line, means the file was created and the
human never sees it.
