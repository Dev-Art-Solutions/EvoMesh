---
name: news-triage
description: How to answer "give me the latest news" versus when a watched keyword deserves an unprompted alert.
---

A direct request for "the N most recent headlines" is answered right away
with `news_fetch` (default limit 10) -- this is a plain reactive answer, not
the watchlist goal.

On the recurring watchlist goal, call `news_fetch` and only ever report a
genuine new match against the keywords configured in `config.json` --
silence is correct every cycle nothing new appeared. Never re-report a
headline already mentioned in a previous cycle's notes; check your own
memory/context first.

A request for history rather than "what's new right now" -- "what came out
about gold today", "anything on the Fed in the last few hours" -- is
`news_fetch` with `{"from_cache": true, "since_hours": <n>}` (add
`"keywords"` to narrow it), not a fresh live fetch: the cache holds
everything actually seen over `config.json`'s `cache_days`, while a live
fetch only shows a source's current snapshot and has already lost whatever
scrolled off it.

A human sets the RSS feeds and watched keywords in `config.json` beside this
agent's template directly, not through conversation.
