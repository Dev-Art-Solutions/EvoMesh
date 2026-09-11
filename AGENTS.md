# Extending EvoMesh with an AI coding assistant

This file is for an agentic coding assistant (OpenAI Codex, or any other reading `AGENTS.md` by
convention) working inside this checkout. If you are Claude Code, prefer the fuller, topic-split
guidance under `.claude/skills/evomesh-agent-author/`, `evomesh-skill-author/`, and
`evomesh-tool-author/` instead -- this file is the condensed version of the same three.

EvoMesh extends itself through three declarative, file-based mechanisms. None of them load Python
into the running mesh process; each is a Markdown file with YAML frontmatter, parsed by
`evomesh/agent_templates.py`, `evomesh/skills.py`, and `evomesh/tools.py` respectively.

## 1. Agent templates -- `agent-templates/<name>/AGENT.md`

A whole agent, bundled with what it needs, installed and instantiated as one unit.

```yaml
---
name: trader                    # required, must match the directory name
purpose: >                       # required
  Watch an MT5 account and execute a strategy only when asked.
autonomy: cyclic                  # cyclic | reactive, default cyclic
cycle_seconds: 120
goals:
  - text: Check account and open positions; note only what changed
    recurring: true
    interval_seconds: 300
skills: [trading-strategy]        # bundled at skills/trading-strategy/SKILL.md beside this file
tools: [mt5_query, mt5_signal]     # bundled at tools/<name>/TOOL.md beside this file
watch:                              # optional -- see below
  command: python "{template_dir}/scripts/watch_positions.py"
  interval_seconds: 5
---
```

Bundled `skills/<name>/` and `tools/<name>/` subdirectories are auto-installed into the live mesh
the first time the template is instantiated. Naming a skill or tool that is not bundled assumes it
is already installed some other way.

**The watcher (`watch`) is not optional to understand.** An agent's `cycle_seconds` (60s+) is what
its own model reasons on -- every tick costs a whole model turn. State that moves on a scale of
seconds (open orders, a price level, a queue) belongs in `watch.command` instead: a plain command
run on its own short `interval_seconds`, completely outside the agent's cognition, that prints
nothing when there's nothing to report and one line of plain text when a threshold is crossed --
that line becomes an announcement to the agent's own channels. Never lower `cycle_seconds` to
match a fast-moving thing; write a watcher instead.

`{template_dir}` in `watch.command` resolves to the template's own **absolute** installed
directory, substituted before the command is parsed -- always use it for a bundled script path,
never a bare relative one; the watcher's actual working directory at run time is the agent's own
playground, not the template directory.

Install with `/agent-template install <directory>`; bring up a running instance (more than once,
if wanted, with different names/tokens) with `/agent-template spawn <template> [name] [--provider
p] [--model m] [--telegram token]`.

Validate: `python -c "from evomesh.agent_templates import parse_agent_template; from pathlib import
Path; p = Path('agent-templates/<name>/AGENT.md'); print(parse_agent_template(p,
p.read_text(encoding='utf-8')))"`.

## 2. Skills -- `skills/<name>/SKILL.md`

A description a model decides to read, never a capability the mesh executes on its behalf --
```yaml
---
name: web-research
description: Fetch a web page and use its content to answer a question, instead of guessing from memory.
---

Use the `fetch` tool. Do not answer from memory when a URL is available...
```
`name` and `description` are both required; nothing else. The body is a procedure in prose --
*when* and *in what order* to call things the agent already has, never a new mechanism. If a draft
skill needs a capability no available tool provides, write a tool instead (below).

Install with `/skill install <path-or-url-or-directory>`.

## 3. Custom tools -- `tools/<name>/TOOL.md`

```yaml
---
name: check-site
description: Check whether a URL responds, and how fast.
command: python "{tool_dir}/scripts/check.py"
parameters:
  - name: url
    description: The URL to check, including scheme (https://...).
    required: true
---
```
`name`, `description`, `command` required; `parameters` optional. `{tool_dir}` resolves to the
tool's own absolute directory, substituted before parsing. A call shells out through the same
allow-listed subprocess path the harness's `shell` tool uses; it only runs if the basename of the
command's first word (e.g. `python`) is listed in `harness.shell_allow` in `evomesh.yaml`.

**The one rule that actually matters:** a parameter's value is appended as one more argv entry, in
declared order, *only for parameters the model actually supplied that turn* -- an omitted optional
parameter is skipped, not passed as an empty slot. All-required parameters, or exactly one optional
parameter declared last, are safe. **Two or more optional parameters are not safe** -- if the model
supplies the second but not the first, your script receives the second value where it expects the
first. Collapse multiple optional fields into one required JSON-string parameter instead
(`payload`/`request`, parsed with `json.loads` in the script) -- this is exactly why the trading
suite's `mt5_signal` and `news_fetch` tools are shaped that way.

Install with `/tool install <path-or-url-or-directory>`.

Validate: `python -c "from evomesh.tools import parse_tool; from pathlib import Path; p =
Path('tools/<name>/TOOL.md'); print(parse_tool(p, p.read_text(encoding='utf-8')))"`, then actually
run the bundled script by hand with a couple of argv shapes to confirm positions land where the
script expects them.

## A worked example

`agent-templates/trader/` and `agent-templates/news-watcher/` are a complete, working pair built
this way -- an MT5 trading agent with a deterministic order/equity watcher, and a financial-news
agent with a keyword watchlist, each installable and instantiable on their own. Read them end to
end before writing a new template from scratch.
