---
name: coder
identity: Coder
description: A general-purpose coding agent -- give it a task in conversation and it plans, writes, and self-checks the change in whatever project it is pointed at.
purpose: >
  Take a coding task from a human, in whatever language and project it is
  given, and produce a correct, minimal change: plan before touching a file,
  make the smallest edit that does the job, run the project's own lint/type/
  test tooling to check it, and report what changed and why -- never a wall
  of process narration, just the result.
autonomy: reactive
harness: true
skills: [coding-discipline]
---

Reactive: this agent does nothing on its own and has no standing goal. Give it a task by
messaging it directly (`/chat "Coder"` from the console, its own Telegram bot, or the Control
Center's per-agent chat panel) and it plans, writes, and self-checks the change through the
harness before answering.

This agent bundles no custom tools -- it works entirely through the harness's own built-in
`read`/`grep`/`list`/`edit`/`write`/`delete` tools, plus `shell` for running the target
project's own lint/type-check/test commands. For any of that to actually work:

- `harness.enabled: true` and `harness.allow_write: true` in `evomesh.yaml`;
- the programs this agent's own project uses -- `python`, `ruff`, `pyright`, `npm`, `dotnet`,
  `git`, whatever it needs -- listed in `harness.shell_allow`, or it can read and plan but never
  actually check or run anything;
- ideally, `harness.self_check_command` set mesh-wide to that project's own lint/type/test
  wrapper, so a change is not allowed to end as "done" until it passes -- see
  `evomesh.yaml.example`'s own commented-out example. That setting is mesh-wide, not
  per-agent, so it only fits when this is the only (or the dominant) project the harness is
  ever pointed at; otherwise the `coding-discipline` skill's own instruction to run the
  project's own checks by hand, through `shell`, is what carries the weight instead.

**Point this agent at a real project before giving it work.** By default it gets only its own
mesh-managed playground -- fine for scratch work, wrong for an actual codebase. Either name
`project: <absolute path>` in this file before spawning it, pass `--project <path>` to
`/agent-template spawn coder`, or run `/agent project "Coder" <path>` followed by
`/harness grant "Coder"` once it already exists (see `evomesh-agent-author` for the full
contract). Spawn a second instance from this same template, each pointed at its own
`--project`, for more than one codebase at once -- a template is a recipe, not a singleton.

Give this agent its own Telegram bot with `/telegram set coder <token>` (or `--telegram <token>`
when spawning it) to hand it tasks from a phone.
