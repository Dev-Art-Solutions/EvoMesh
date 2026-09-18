---
name: mql5-conventions
description: MQL5-specific rules for writing an Expert Advisor, indicator, or script -- read this before writing or editing any .mq5/.mqh file.
---

MQL5 looks like C++ and is not one in the ways that matter here. Follow these rules for any
`.mq5` (program) or `.mqh` (include/header) file.

## Which event handlers exist, and what goes in them

- **Expert Advisor** (an automated strategy, runs continuously on a chart): `OnInit()` --
  one-time setup (indicator handles, input validation), return `INIT_SUCCEEDED` or a specific
  `INIT_*` failure code, never leave it implicit. `OnDeinit(const int reason)` -- release
  indicator handles (`IndicatorRelease`), never assume the terminal cleans these up for you.
  `OnTick()` -- runs on every new tick; this is where a strategy actually trades. `OnTimer()` --
  only if `EventSetTimer` was called in `OnInit`. `OnTrade()`/`OnTradeTransaction()` -- react to
  the account's own trade events, not user input.
- **Indicator**: `OnCalculate(...)` is the whole program; `#property indicator_chart_window` or
  `_separate_window` plus `indicator_buffers`/`indicator_plots` declare what it draws, before
  any code runs.
- **Script**: `OnStart()` is the whole program; runs once when dragged onto a chart, has no
  ongoing event loop at all.

Naming a handler that does not exist for the file's own program type (`OnCalculate` in an EA,
`OnTick` in a script) is a silent no-op, not a compile error worth relying on -- get the program
type and its handler set right before writing anything else.

## Trading: use CTrade, not raw OrderSend plumbing

`#include <Trade\Trade.mqh>` and a `CTrade` instance is the standard way to open, modify, and
close a position -- it handles the request/result structures and retry-on-requote plumbing that
raw `OrderSend`/`MqlTradeRequest` calls otherwise repeat by hand in every EA. Check
`CTrade::ResultRetcode()` (or `GetLastError()` for a raw call) after every trade operation and
log or return on failure -- MQL5 trade calls fail silently into a return value, not an exception.

Never risk-size, submit, or modify an order because a task merely *mentions* trading, price
levels, or a strategy in passing -- only when explicitly asked to place, size, or change a live
or demo position. The same caution the news-impact-analysis/trading-strategy skills already
apply elsewhere in this project applies here: analysis and code review are not authorization to
act on an account.

## Memory and lifetime

Every `new` needs a matching `delete` -- MQL5 has no garbage collector. An indicator handle from
`iCustom`/`iMA`/etc. needs `IndicatorRelease` in `OnDeinit`, not just on program end; a chart
object created with `ObjectCreate` needs `ObjectDelete`. A CArrayObj/CList of pointers deletes
its own elements only if told to (`FreeMode(true)`) -- check which container you're using rather
than assuming.

## Never block the terminal thread

No `Sleep()` (or any blocking wait) inside `OnTick`, `OnCalculate`, or `OnTimer` -- these run on
the terminal's own UI-adjacent thread, and a blocked one freezes the chart for the human watching
it, not just this program. Poll or retry via `OnTimer` instead of sleeping inline. `MessageBox`
in an EA blocks the same way -- use `Print()`/`PrintFormat()` for anything that is not a genuine,
rare interactive prompt a human is actually expected to see and dismiss.

## Compiling: verify, don't just claim

If `config.json` beside this template's `AGENT.md` names a `metaeditor_path` (a local
`metaeditor64.exe`, since MetaEditor is the only real MQL5 compiler and it is not a dependency
this project installs for you) and `shell_allow` includes it, compile what you wrote with
`"<metaeditor_path>" /compile:"<file>" /log` and read the resulting `.log` file before answering
-- a nonzero-warning-or-error compile is not "done". If neither is configured, say plainly in
your final answer that the file was written but not compiled, rather than claiming it builds.

## Python alongside MQL5

This agent also writes plain Python -- most often a client or test harness talking to an already
running MT5 terminal via the official `MetaTrader5` package (`pip install MetaTrader5`,
Windows-only, requires a running terminal with an active login), or scripts for a bridge service
like this project's own MT5-Execution-Bridge. The `coding-discipline` skill's plan/edit/check/test
loop applies to that Python exactly the way it does to any other Python task -- MQL5's own rules
above are additional, not a replacement.
