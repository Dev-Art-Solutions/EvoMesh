---
name: evomesh-agent-author
description: Author or edit an EvoMesh agent template (agent-templates/<name>/AGENT.md) that bundles skills, tools, and an optional deterministic watcher into one installable, instantiable agent. Use when asked to create a new EvoMesh agent, a "template" for one, or to extend EvoMesh with a new agent type.
---

# Authoring an EvoMesh agent template

An agent template is a whole agent, bundled with what it needs, installed and instantiated as one
unit — not a Python class, not code loaded into the running mesh. One template is one directory:

```
agent-templates/<name>/
  AGENT.md              required — identity, purpose, goals, what it bundles
  config.json            optional — a human-editable settings file your bundled
                          scripts read (thresholds, feed URLs, whatever the
                          template needs); nothing in evomesh's own code reads
                          this file, only scripts you write
  skills/<skill-name>/SKILL.md         bundled skills, one directory each
  tools/<tool-name>/TOOL.md            bundled tools, one directory each
  scripts/*.py           optional — a bundled watcher script, or anything a
                          tool/skill script needs; not auto-installed anywhere,
                          referenced by absolute path via {template_dir}
```

Read `evomesh-skill-author` and `evomesh-tool-author` (in this same skills directory) before
writing the bundled `skills/*` and `tools/*` — the frontmatter rules there are not repeated here.

## AGENT.md frontmatter

```yaml
---
name: trader                 # required — must match the directory name
identity: Trader              # optional, defaults to name
description: One line for `/agent-templates` search results.   # optional, defaults to purpose
purpose: >                    # required — this agent's own operating purpose,
  Watch an MT5 account and execute a strategy only when asked.  # what its prompt is built from
provider: ollama               # optional, defaults to the mesh's default provider
model: qwen3                   # optional, defaults to that provider's configured model
autonomy: cyclic                # cyclic | reactive (default cyclic)
cycle_seconds: 120               # optional; a REACTIVE agent never uses this
goals:                            # optional list, each becomes a standing goal
  - text: Check account and open positions; note only what changed
    priority: 4                    # optional, default 5
    recurring: true                 # optional, default false
    interval_seconds: 300            # optional — re-checked on this schedule,
                                      # independent of cycle_seconds
    cron: "*/15 * * * *"               # optional, mutually sensible with
                                        # interval_seconds (pick one)
    notify: true                        # optional, default false — announce
                                         # this goal's progress and completion
skills: [trading-strategy]           # names this template bundles or expects installed
tools: [mt5_query, mt5_signal]        # same
watch:                                 # optional deterministic watcher — see below
  command: python "{template_dir}/scripts/watch_positions.py"
  interval_seconds: 5
---

Everything after the closing `---` is a Markdown body for a human who opens this file —
not read by the mesh. Use it to document config.json's fields and any setup the template needs
(a running local service, an API key, `harness.shell_allow` entries).
```

Both `name` and `purpose` are required; parsing fails otherwise (`InvalidAgentTemplateError`).
`goals[].text` is required per goal; everything else in a goal has the defaults shown above.

## The deterministic watcher — when to use `watch`, and why

An agent's `cycle_seconds` is what its own model reasons on: 60 seconds or more, because every
tick costs a whole model turn. If what you're watching moves on a scale of seconds — open orders,
a price level, a queue depth — do **not** lower `cycle_seconds` to match. That turns "check every
5 seconds" into "call an LLM every 5 seconds just to notice nothing changed," which is slow, and
wasteful on the small local models this project targets.

Instead, give the template a `watch.command`: a plain command, run on its own `interval_seconds`,
completely outside the agent's cognition. It should:

- print **nothing** when there is nothing to report (silence is the correct, common case),
- print **one line of plain text** when a threshold is crossed — that line becomes an
  announcement to the agent's own channels (its private Telegram bot if it has one, the mesh-wide
  one otherwise),
- exit 0 whether or not it found something to say; a non-zero exit is logged as a failure, not an
  alert, and never reaches an agent's channel,
- never touch a model itself — it is deterministic Python (or anything executable), not another
  agent turn.

`{template_dir}` in the command is substituted with the template's own **absolute** installed
directory before the command is parsed — write your script path as
`"{template_dir}/scripts/whatever.py"`, never a bare relative path. The watcher's working directory
at run time is the *agent's own playground*, not the template directory, so a relative path resolves
against the wrong place; this exact bug (an unresolved relative substitution) was caught and fixed
building the Trader template — do not reintroduce it if you ever touch `agent_templates.py` itself.

A watcher script that needs human-tunable thresholds should read them from `config.json` beside
`AGENT.md` (resolve the path from the script's own `__file__`, the same way `watch_positions.py`
does it in `agent-templates/trader/`) — that file is meant to be hand-edited, not driven through
conversation.

## Bundled skills and tools: naming rules

`instantiate()` only auto-installs a bundled skill/tool the first time — it looks for
`agent-templates/<name>/skills/<skill-name>/` (or `tools/<tool-name>/`) and installs it into the
mesh's live `SkillRegistry`/`ToolRegistry` if that subdirectory exists. Name a skill or tool in
`goals`/`skills`/`tools` that is **not** bundled there only if you know it is already installed
some other way — otherwise the agent gets a dangling reference to something that was never
registered.

If `tools` is non-empty, `instantiate()` automatically grants the new agent harness access rooted
at its own playground, so its custom tools can actually run. Two things still have to be true, or
the tool exists but silently cannot run:

- `harness.enabled: true` in `evomesh.yaml`;
- the tool's underlying program (its command's first word, e.g. `python`) listed in
  `harness.shell_allow`.

Say this explicitly in the template's Markdown body, so whoever installs it knows to check.

## Installing and spawning

```
/agent-template install <directory>          # copies the whole bundle in, live, no restart
/agent-template show <name>                   # preview an installed template's AGENT.md
/agent-template spawn <template> [name] [--provider p] [--model m] [--telegram token]
```

`spawn` is callable more than once per template — a second instance with a different name and its
own Telegram bot is exactly `spawn` called again. A template is a recipe, not a singleton; a
watcher script keyed to a shared `config.json`/state file next to the template directory is shared
across every instance spawned from it — call that out in the body if it matters for a given template.

## Validating a draft before calling it done

From the EvoMesh repo root, with its own virtualenv:

```
python -c "from evomesh.agent_templates import parse_agent_template; from pathlib import Path; p = Path('agent-templates/<name>/AGENT.md'); print(parse_agent_template(p, p.read_text(encoding='utf-8')))"
```

A raised `InvalidAgentTemplateError` names exactly what is wrong. If the template has bundled
tools, also confirm each one parses (see `evomesh-tool-author`) and, if you can, actually spawn the
agent against a disposable `Environment`/`MockProvider` and confirm it starts, the way
`tests/test_agent_templates.py` does — that catches placeholder-resolution and cwd mistakes a pure
frontmatter check cannot.
