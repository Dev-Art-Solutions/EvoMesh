---
name: trading-strategy
description: When and how to check the MT5 account and place a trade through the Execution Bridge -- the Bridge enforces every risk rule, this only says when to ask it to.
---

Before doing anything else in a cycle whose goal mentions positions or
equity, call `mt5_query` with `resource: account` and then `resource:
positions`, and compare against what you noted last time -- report only
what changed, not the whole state again.

Only call `mt5_signal` when a human explicitly asked you to execute a trade
or a strategy, never on your own initiative from a routine check-in. Always
set a `stop_loss` unless the human explicitly said not to. `risk_percent` is
a percentage of account equity, not a lot size -- keep it small (0.5-2)
unless told otherwise.

The Bridge, not you, is the risk authority: it can and will reject a signal
(spread too wide, daily loss limit hit, too many open positions already, no
stop loss). Report a rejection plainly, in the words the Bridge gave back --
never retry a rejected signal on your own.

A human tunes the deterministic order watcher (which alerts on equity/loss
thresholds every few seconds, independent of your own cycle) by editing
`config.json` beside this agent's template directly -- that is not something
you configure through conversation.
