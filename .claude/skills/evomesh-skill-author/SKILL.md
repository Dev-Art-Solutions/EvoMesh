---
name: evomesh-skill-author
description: Author or edit an EvoMesh runtime skill (skills/<name>/SKILL.md or a template's bundled skills/<name>/SKILL.md) -- a description an EvoMesh agent decides to read, never code that runs. Use when asked to add a "skill" for an EvoMesh agent, not a Claude Code skill.
---

# Authoring an EvoMesh skill

Careful: this is about **EvoMesh's own** runtime skill format (`evomesh.skills.SkillRegistry`), not
a Claude Code skill like this one. They happen to share the same shape on purpose -- an EvoMesh
skill is read by a model the same way a Claude Code skill is loaded by this assistant -- but an
EvoMesh `SKILL.md` lives inside an EvoMesh checkout's `skills/` directory (or a template's
`agent-templates/<template>/skills/<name>/`) and is read by whatever local model is running that
agent, not by Claude Code.

## What a skill is, and is not

An EvoMesh skill is a description an agent's model decides to read -- **never** a capability the
mesh executes on its behalf. There is no "run this skill" method anywhere in the codebase. A skill
must never claim to add a mechanism that is not already a real tool call the agent has: the
harness's built-in tools (`read`, `search`, `list`, `fetch`, `edit`, `write`, `shell` if allowed)
or one of its own declared custom tools (see `evomesh-tool-author`). A skill only adds the
*procedure* -- when to reach for which call, and in what order -- in prose.

If a draft skill describes doing something no available tool call can actually do, that is a sign
the thing you actually need is a new custom tool (or a change to an existing one), not a skill.

## File shape

```
skills/<name>/SKILL.md
skills/<name>/scripts/...      optional -- bundled commands the body names,
                                run by the agent with its own harness tools,
                                never auto-executed by installing the skill
```

Frontmatter needs exactly two fields, both required:

```yaml
---
name: web-research
description: Fetch a web page and use its content to answer a question, instead of guessing from memory.
---

Use the `fetch` tool. Do not answer from memory when a URL is available...
```

`description` is what a model sees in the catalog line before deciding whether to read the rest --
`render_catalog()` shows only `name: description (read <path> for how)`, never the body, so a vague
description means the skill is never read even when it would have helped. Say specifically when
this applies ("when the goal mentions X"), not generically ("helps with research").

The body is free-form Markdown, read verbatim by the model with the same `read` tool it reads any
other file with. Keep it a procedure, not a restatement of what a tool already documents about
itself -- assume the model can read `TOOL.md`/its own tool list; say what order to call things in,
what to check before acting, what never to do on its own initiative.

## Installing

```
/skill install <path-or-url-or-directory>
```

A lone `SKILL.md`, one fetched over HTTP, or a directory bundling scripts beside it -- all
installed live, no restart. Bundled under an agent template instead (`agent-templates/<template>/
skills/<name>/`), it is installed automatically the first time that template is spawned (see
`evomesh-agent-author`) -- do not also `/skill install` it separately unless you want it available
mesh-wide before any agent using it exists yet.

## Validating a draft

```
python -c "from evomesh.skills import parse_skill; from pathlib import Path; p = Path('skills/<name>/SKILL.md'); print(parse_skill(p, p.read_text(encoding='utf-8')))"
```

An `InvalidSkillError` names exactly what is wrong (missing frontmatter delimiter, missing `name`
or `description`, frontmatter that is not a YAML mapping).
