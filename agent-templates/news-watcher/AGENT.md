---
name: news-watcher
identity: NewsWatcher
description: Watches financial news RSS feeds for user-chosen keywords/instruments and reports on request or on a genuine match.
purpose: >
  Track financial news (RSS feeds configured by a human) and tell the human
  about the most recent headlines on request, or promptly when a watched
  keyword (e.g. "gold"/XAUUSD) appears in a new one.
autonomy: cyclic
cycle_seconds: 120
skills: [news-triage]
tools: [news_fetch]
watch:
  command: python "{template_dir}/scripts/watch_news.py"
  interval_seconds: 300
---

A human reading this: edit `config.json` beside this AGENT.md to set
`feeds` (RSS feed URLs) and `keywords` (case-insensitive substrings to watch
for, e.g. `["gold", "XAUUSD"]`):

```json
{
  "feeds": [
    "https://finance.yahoo.com/",
    "https://www.forexfactory.com/news"
  ],
  "keywords": ["gold", "XAUUSD"],
  "limit": 10
}
```

Ask directly for "the 10 latest news" any time -- that answers immediately
through the agent's own conversation, not the watcher.

The watchlist itself is a deterministic watcher (`scripts/watch_news.py`,
polled every `interval_seconds`, 300s by default), not a BDI goal -- it never
touches a model and never reports "still checking" progress. It only prints
a line (which becomes an announcement, to this agent's own Telegram bot if
it has one) the first time a headline matching a configured keyword is seen;
already-seen matches are remembered in `scripts/.watch_state.json` beside
this file and never repeated. If `keywords` is empty, the watcher stays
completely silent -- there is nothing to match against.

Give this agent its own Telegram bot with `/telegram set news-watcher
<token>` (or `--telegram <token>` when spawning it) to talk to it directly,
and to route to a Trader agent over the mesh bus if the two should talk to
each other.
