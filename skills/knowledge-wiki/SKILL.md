---
name: knowledge-wiki
description: How to keep your own knowledge wiki (wiki_search / wiki_read / wiki_write) so what you learn compounds instead of being re-derived every time.
---

# Keeping a knowledge wiki

You have a small wiki of your own: Markdown pages you write, one topic each,
plus an index. Your memory.md is a diary that gets compacted; the wiki is
what you *concluded* from it. Pattern from Andrej Karpathy's "LLM wiki".

## Before you research or answer

1. `wiki_search` with the key words of the question. The prompt already shows
   matching index lines under "Knowledge pages" when there are any.
2. `wiki_read` the one or two pages that match. Answer from them if they
   answer it -- do not fetch or re-derive what a page already says.

## After you learn something durable

Durable = still true next week and useful again: how a source behaves, what
moves an instrument, a procedure that worked, a human's stated preference.
Not durable: today's headline, a one-off number.

1. `wiki_search` for an existing page on the topic.
2. If one exists, `wiki_read` it and **merge**: rewrite the whole page with
   the new fact in its place, removing what it contradicts. Never append a
   duplicate line.
3. Otherwise `wiki_write` a new page. Title = the topic, not the date.
4. `summary` is one line saying what the page answers -- it is all that is
   shown in the index.
5. Link related pages with `[[page-name]]`.

## Page shape

```markdown
# Gold (XAUUSD) drivers

- Falls when real yields rise; rises on Fed-cut expectations.
- Safe-haven bid on geopolitical shocks, usually fades within days.

Sources: ForexLive 2026-09-28; see also [[fed-calendar]].
```

Keep a page under ~40 lines. Two topics = two pages.

## Your reports

`recent_reports` returns what you actually sent the human, newest first --
use it when asked for "your last analyses" instead of recalling them.
