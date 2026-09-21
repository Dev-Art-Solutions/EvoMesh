---
name: news-analyzer
identity: NewsAnalyzer
description: Periodically reviews financial headlines and judges which ones could plausibly move a watched instrument's price, in which direction, and how confidently.
purpose: >
  Read the same kind of financial headlines NewsWatcher watches for keywords,
  but go one step further: for each new headline, reason about whether it is
  likely to move price for a human-configured instrument, in which direction,
  and how confident that judgment is -- then tell the human only the
  genuinely confident calls. Never place, size, or suggest a specific trade;
  this agent informs a human's (or a Trader agent's own operator's) decision,
  it does not act on it.
autonomy: cyclic
cycle_seconds: 120
goals:
  - text: >
      Fetch the latest financial headlines with news_fetch. For each one you
      have not already assessed (check your own memory/context first), judge
      which configured instrument it plausibly affects, the likely direction
      (bullish/bearish/neutral), and your confidence (low/medium/high) per
      the news-impact-analysis skill. Report only headlines at or above
      config.json's min_confidence; silence is correct when nothing new
      clears that bar.
    priority: 5
    recurring: true
    cron: "*/30 * * * *"
    notify: true
    # Deterministic backstop for the skill's "only send report lines, never
    # narrate what you did" rule (see agents.py's _apply_report_pattern) --
    # a model that ignores that rule and reports its own bookkeeping instead
    # (e.g. "appended this cycle's assessment to the scratch log") gets that
    # line silently dropped rather than forwarded to a human. Must match
    # news-impact-analysis's own report-line format exactly if that format
    # ever changes.
    report_pattern: "^[A-Za-z0-9_.]+ (bullish|bearish|neutral) \\((low|medium|high)\\): .+ -- .+$"
skills: [news-impact-analysis]
tools: [news_fetch]
---

A human reading this: edit `config.json` beside this AGENT.md to set `feeds` (RSS
feed URLs, same shape as NewsWatcher's), `instruments` (a map of symbol to the
keywords/substrings that plausibly relate to it, e.g. `{"XAUUSD": ["gold", "xau"],
"EURUSD": ["euro", "ecb", "fed"]}`), `min_confidence` (`"low"`, `"medium"`, or
`"high"` -- only assessments at or above this are reported), and `cache_days` (how
many days of fetched headlines to keep on disk, default 3):

```json
{
  "feeds": [
    "https://finance.yahoo.com/",
    "https://www.forexfactory.com/news"
  ],
  "instruments": {
    "XAUUSD": ["gold", "xau"],
    "EURUSD": ["euro", "ecb", "fed"]
  },
  "min_confidence": "medium",
  "cache_days": 3
}
```

Every `news_fetch` call, live or from the recurring goal, feeds a durable cache
(`scripts/.news_cache.jsonl`, beside this AGENT.md) so a headline that scrolls off a
feed's own "latest" list is not simply gone -- ask for `{"from_cache": true,
"since_hours": 24}` (see the news-impact-analysis skill) to catch up on anything
missed rather than only ever judging whatever a source happens to show right now.
Entries older than `cache_days` are pruned automatically.

Every report line this agent has ever actually sent (not its per-headline reasoning,
which lives in its own never-pruned `scripts/.news_reasoning.log` scratchpad) is also
appended to `scripts/.news_reports.log`, beside this AGENT.md -- ask this agent directly
for "your last analysis" or "what you've found recently" any time to have it read that
log back, rather than re-running the recurring cycle's own "anything new" check, which
answers a different question and stays silent once nothing has changed since the last
pass.

This agent never trades and never talks to a Trader agent directly -- EvoMesh agents
only ever reach each other through the mesh's own mailboxes, and nothing wires this
template to Trader's. The bridge is a human: give this agent its own Telegram bot with
`/telegram set news-analyzer <token>` (or `--telegram <token>` when spawning it), and
either read it alongside Trader's bot or point both at the same chat so one place shows
both "here's a position" and "here's why price might move."

The `news_fetch` tool this agent uses reaches the network through the harness, so
`harness.enabled: true` must be set in `evomesh.yaml` and `python` must be listed in
`harness.shell_allow` -- the same requirement NewsWatcher's own tool has. Ask directly
for "the latest headlines" any time for a plain fetch, separate from the recurring
analysis goal; that answers immediately through the agent's own conversation. Widen or
tighten the cron schedule with `/goal ...` once this agent is running, if 30 minutes is
too often or too rare for how fast the configured feeds actually update.
