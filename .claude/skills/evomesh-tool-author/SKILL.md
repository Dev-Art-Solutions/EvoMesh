---
name: evomesh-tool-author
description: Author or edit an EvoMesh custom tool (tools/<name>/TOOL.md or a template's bundled tools/<name>/TOOL.md) -- a named, parameterized command an EvoMesh agent calls directly. Use when asked to add a "tool" for an EvoMesh agent, especially one that wraps a script or an HTTP call.
---

# Authoring an EvoMesh custom tool

A custom tool is declarative: a `command` template plus named parameters, described in `TOOL.md`.
No Python is loaded into the running mesh process -- `harness_tools.build_custom_tool()` turns the
declaration into a real tool whose `run` shells out through the exact same allow-listed subprocess
path the harness's own `shell` tool uses. A custom tool can never run anything beyond what its own
`command` already names, and never anything at all unless that program is allow-listed (see below).

## File shape

```
tools/<name>/TOOL.md
tools/<name>/scripts/...     the script(s) the command runs; referenced with {tool_dir}
```

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

`name`, `description`, and `command` are required; `parameters` defaults to none. `{tool_dir}` in
`command` is substituted with the tool's own **absolute** installed directory before the command is
parsed -- a harness job's root is whatever the job is about (an agent's own playground, most of the
time), never reliably this tool's own directory, so a bare relative script path breaks the moment
the calling job is rooted anywhere else.

## The one rule that actually matters: parameter → argv position

`command` is `shlex`-split **once**, into a fixed prefix. At call time, each parameter the model
*actually supplied that turn* is appended, in the order declared in `parameters` -- but only the
ones supplied. An omitted optional parameter is not passed as an empty slot; it is skipped
entirely. That means:

- **all-required parameters** are always safe -- position never shifts, because every parameter is
  always present.
- **exactly one optional parameter, declared last**, is safe -- it is either present at the end or
  absent, nothing before it can shift.
- **two or more optional parameters** are **not safe** in general: if the model supplies the second
  one but not the first, your script receives the second value where it expects the first, with no
  way to tell. This is a real bug class, not a theoretical one -- it is exactly why the trading
  suite's `mt5_signal` and `news_fetch` tools collapse several optional fields into one **required**
  JSON-string parameter (`payload` / `request`) that the script parses itself with `json.loads`,
  instead of declaring `stop_loss`, `take_profit`, `limit`, etc. as separate optional parameters.

When a tool genuinely needs more than one optional field, prefer the JSON-payload pattern over
multiple optional parameters:

```yaml
parameters:
  - name: payload
    description: >
      JSON object, e.g. {"symbol": "XAUUSD", "risk_percent": 1.0, "stop_loss": 1900.0}.
      Required: symbol, risk_percent. Optional: stop_loss, take_profit.
    required: true
  - name: bridge_url
    description: "Base URL of the service (default http://127.0.0.1:8200)"
    required: false
```

(One optional field after a single required one is still safe -- the ambiguity is specifically
between *multiple* optional fields.)

## Activation: the allow-list

A tool only runs if the basename of its command's first word (extension stripped, e.g. `python`,
not `python.exe`) appears in `harness.shell_allow` in `evomesh.yaml`. Installing a tool never
changes that list -- `/tools` marks an inactive tool as such, and the fix is a human adding the
program by name, never the tool itself trying to widen its own allow-list. Say this in the tool's
description or the template's body so it is not a silent surprise.

## Output discipline

Tool output is captured (stdout+stderr combined) and trimmed to the harness's own result-size caps
before it reaches a model's prompt -- keep a script's own output terse and structured. Prefer
printing JSON for anything with more than one field; a human-readable table is harder for a small
model to extract a single value from reliably than `json.dumps(..., indent=2)` is.

## Installing

```
/tool install <path-or-url-or-directory>
```

Same one-step mechanism as `/skill install`. Bundled under an agent template instead
(`agent-templates/<template>/tools/<name>/`), it installs automatically the first time that
template is spawned (see `evomesh-agent-author`).

## Validating a draft

```
python -c "from evomesh.tools import parse_tool; from pathlib import Path; p = Path('tools/<name>/TOOL.md'); print(parse_tool(p, p.read_text(encoding='utf-8')))"
```

Then actually run the bundled script by hand with a couple of argv shapes (all-required present;
optional present; optional absent) to confirm positions land where the script expects them --
parsing success only means the frontmatter is well-formed, not that the argv contract is sound.
