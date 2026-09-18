---
name: mt5-coder
identity: MT5 Coder
description: Writes MQL5 (Expert Advisors, indicators, scripts) and the Python that talks to MetaTrader 5, on request -- plans, writes, and self-checks before answering, and compiles through MetaEditor when one is configured.
purpose: >
  Take an MQL5 or MT5-Python coding task from a human and produce a correct,
  minimal change: plan before touching a file, follow MQL5's own event-model
  and memory-lifetime rules, write the smallest change that does the job, and
  verify it -- by compiling through MetaEditor when configured, and by
  running the project's own checks for anything in Python -- before
  reporting what changed and why.
autonomy: reactive
harness: true
skills: [coding-discipline, mql5-conventions]
---

Reactive: this agent does nothing on its own and has no standing goal. Give it a task by
messaging it directly (`/chat "MT5 Coder"` from the console, its own Telegram bot, or the
Control Center's per-agent chat panel).

Like `coder` (see that template if you have not already), this agent bundles no custom tools --
it works through the harness's own built-in `read`/`grep`/`list`/`edit`/`write`/`delete`/`shell`.
For any of that to work:

- `harness.enabled: true` and `harness.allow_write: true` in `evomesh.yaml`;
- `python` in `harness.shell_allow` for any Python work, and the *basename* of your MetaEditor
  executable (e.g. `metaeditor64`) if you want compiled verification of `.mq5` changes -- see
  `config.json` below.

**Point this agent at a real project before giving it work.** By default it gets only its own
mesh-managed playground. Either name `project: <absolute path>` in this file before spawning it
(e.g. your MQL5 `Experts`/`Indicators`/`Scripts` folder, or a repo like MT5-Execution-Bridge),
pass `--project <path>` to `/agent-template spawn mt5-coder`, or run
`/agent project "MT5 Coder" <path>` followed by `/harness grant "MT5 Coder"` once it already
exists (see `evomesh-agent-author` for the full contract).

A human configures MetaEditor-based compile verification by editing `config.json` beside this
`AGENT.md` directly (not through conversation):

```json
{
  "metaeditor_path": "C:\\Program Files\\MetaTrader 5\\metaeditor64.exe"
}
```

Leave `metaeditor_path` empty (or the file absent) to skip compiled verification entirely --
the agent still writes `.mq5`/`.mqh` files and says plainly, per `mql5-conventions`, that it did
not compile them. `metaeditor64` (the basename, no path or extension) must also be in
`harness.shell_allow`, or the agent has the path but the harness still refuses to run it.

For the Python side, name `self_check: "<command>"` in this file before spawning it, or run
`/agent self-check "MT5 Coder" "<command>"` once it already exists, to run this agent's own
project's lint/type/test command automatically before a writing job is allowed to end -- this
agent's own `self_check_command` always wins over `harness.self_check_command`'s mesh-wide
setting, so it never fights with `coder` or another `mt5-coder` instance pointed at a different
project over one shared value. `/agent self-check "MT5 Coder" clear` goes back to the mesh-wide
setting.

Give this agent its own Telegram bot with `/telegram set mt5-coder <token>` (or
`--telegram <token>` when spawning it) to hand it tasks from a phone.
