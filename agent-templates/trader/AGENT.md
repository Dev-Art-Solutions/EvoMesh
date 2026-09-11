---
name: trader
identity: Trader
description: Watches an MT5 account through the local Execution Bridge and executes a trading strategy only when asked; a deterministic watcher alerts on equity/loss thresholds every few seconds.
purpose: >
  Watch the MT5 account through the local Execution Bridge, keep a human
  informed of open positions and account health, and execute a trading
  strategy through mt5_signal only when explicitly asked to.
autonomy: cyclic
cycle_seconds: 120
goals:
  - text: Check account and open positions with mt5_query; note only what changed since last time
    priority: 4
    recurring: true
    interval_seconds: 300
skills: [trading-strategy]
tools: [mt5_query, mt5_signal]
watch:
  command: python "{template_dir}/scripts/watch_positions.py"
  interval_seconds: 5
---

This agent needs the MT5 Execution Bridge running locally (see the
MT5-Execution-Bridge repo) and `harness.shell_allow` in evomesh.yaml to
include `python`, or `mt5_query`/`mt5_signal` stay installed but inactive.

A human tunes the deterministic order watcher by editing `config.json` next
to this AGENT.md directly (not through conversation):

```json
{
  "bridge_url": "http://127.0.0.1:8200",
  "equity_floor": null,
  "equity_drop_percent": 5.0,
  "max_position_loss": null
}
```

- `equity_floor`: alert once account equity drops below this absolute value.
- `equity_drop_percent`: alert once equity drops this many percent below the
  highest equity seen since the watcher last started.
- `max_position_loss`: alert once any single open position's floating loss
  reaches this absolute value.

The watcher polls every `interval_seconds` (5s by default) and only speaks up
when one of these is crossed -- everything else this agent does (checking
positions on request, executing a strategy) goes through its own
conversation cycle instead, on its own `cycle_seconds` (120s by default).

Give this agent its own Telegram bot with `/telegram set trader <token>` (or
`--telegram <token>` when spawning it) to talk to it directly.
