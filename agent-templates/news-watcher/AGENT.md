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
skills: [news-triage, news-report-export]
tools: [news_fetch]
learn_skills: true
watch:
  command: python "{template_dir}/scripts/watch_news.py"
  interval_seconds: 300
  # Three feeds fetched in parallel at 10s each; room to spare.
  timeout_seconds: 60
---

A human reading this: edit `config.json` beside this AGENT.md to set
`feeds` (RSS feed URLs), `keywords` (case-insensitive substrings to watch
for, e.g. `["gold", "XAUUSD"]`), and `cache_days` (how many days of fetched
headlines to keep on disk, default 3):

```json
{
  "feeds": [
    "https://finance.yahoo.com/",
    "https://www.forexfactory.com/news"
  ],
  "keywords": ["gold", "XAUUSD"],
  "limit": 10,
  "cache_days": 3
}
```

Ask directly for "the 10 latest news" any time -- that answers immediately
through the agent's own conversation, not the watcher. Ask for history
beyond a single live snapshot with `news_fetch`'s `{"from_cache": true,
"since_hours": 24}` -- every live fetch, whether from a direct question or
from the watchlist below, feeds a durable cache
(`scripts/.news_cache.jsonl`, beside this AGENT.md) so headlines already pushed
out of a feed's own "latest" list are not simply gone; entries older than
`cache_days` are pruned automatically.

Ask for the headlines as a file instead -- "the last 10 news as a PDF",
"export today's gold news to Excel" -- and the `news-report-export` skill
takes over: `news_fetch` for the headlines, then the mesh-wide
`document_write` tool to build a real `.pdf`/`.xlsx`/`.csv`/`.docx`, handed
back the same way any agent hands back a file it created (a `FILE: <path>`
line in its reply, which Telegram uploads as a real document). Needs
`document_write`'s own venv provisioned once, mesh-wide -- see
`scripts/install-docs-env.ps1`/`.sh` in the repo root.

`learn_skills: true` above grants this agent `learn_skill` (see README's
Skills section) -- a request that combines its tools in some way none of
its own skills already cover (a format `document_write` supports that
`news-report-export` does not name, say) can end up saved as a new skill
under `skills/`, written by the agent itself once it has actually worked
the procedure out and used it. `/learn revoke "NewsWatcher"` turns this
back off; `/learn status "NewsWatcher"` shows whether it is currently on.

The watchlist itself is a deterministic watcher (`scripts/watch_news.py`,
polled every `interval_seconds`, 300s by default), not a BDI goal -- it never
touches a model and never reports "still checking" progress. It only prints
a line (which becomes an announcement, to this agent's own Telegram bot if
it has one) the first time a headline matching a configured keyword is seen;
already-announced matches are remembered in `scripts/.watch_state.json`
beside this file and never repeated -- a separate, smaller record than the
cache above, which keeps every headline's content, not just which links were
already announced. If `keywords` is empty, the watcher stays completely
silent -- there is nothing to match against.

Give this agent its own Telegram bot with `/telegram set news-watcher
<token>` (or `--telegram <token>` when spawning it) to talk to it directly,
and to route to a Trader agent over the mesh bus if the two should talk to
each other.
