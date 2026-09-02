# CLAUDE.md

Guidance for Claude Code when working in this repository. Keep it to what is **non-obvious** —
[README.md](README.md) already holds the user-facing pitch, the CLI reference and the config
reference, and [CHANGELOG.md](CHANGELOG.md) holds what changed when.

## What this is

EvoMesh is a local-first, experimental multi-agent **environment**, not a framework of prompts
around an API call. The environment owns agent lifecycle, messaging, persistent state, skills,
filesystem grants, model access, health and evolutionary history; agents talk *through* it rather
than to each other. Everything runs against local models (Ollama by default, InferHub or any
OpenAI-compatible local endpoint), so every design decision assumes a **small context window and a
slow model**, not GPT-class headroom.

Version lives in [pyproject.toml](pyproject.toml); tags and the Windows package use the
`v0.1.0-alpha.N` form of the same number. Python 3.13+, `uv`, plus a .NET WinForms Control Center on Windows.

## Repository layout

```text
src/evomesh/     the whole runtime (see the module map below)
desktop/         EvoMesh.Desktop — WinForms Control Center (net8.0-windows)
tests/           unit, cycle, reachability, publishing and restart-scenario coverage
docs/evolution/  one entry per generation, written by the mesh into its own commit
scripts/         run-dev.ps1 / run-dev.sh / run-supervised.ps1 / package-windows.ps1
*.bat            start-evomesh.bat (Control Center), start-evomesh-console.bat (terminal)
data/            SQLite state (ignored)
workspace/       per-agent memory.md / context.md + shared world context (ignored)
generations/     supervisor metadata and candidate workspaces (ignored)
.runtime/logs/   mesh.log, control-center.log (ignored)
```

| Module | Holds |
|---|---|
| `environment.py` | the explicit application state: registry, lifecycle, mailboxes, world context |
| `contracts.py` | the replaceable seams — model provider, channel, behavior, repository |
| `agents.py` | agent records, status/phase, per-agent provider+model |
| `behaviors.py` | what one cycle *does*; one behavior per agent kind (largest file after `evolution.py`) |
| `bdi.py`, `cognition.py` | beliefs/desires/intentions, reconsideration, prompt budgeting, `strip_reasoning` |
| `memory.py` | `memory.md` append + compaction, `context.md` rewrite |
| `evolution.py` | the whole generational pipeline: supervisor metadata, candidates, validation, repair, promotion, publishing, backlog |
| `codebase.py` | `project_map()` — surveys `src/evomesh/`, resolves the import graph, marks load-bearing vs dead |
| `harness.py`, `harness_tools.py`, `harness_session.py`, `harness_queue.py` | the tool loop, its tools, its JSONL record, and the job queue an agent submits work to. **Flat modules on purpose** — `codebase.py` globs `*.py` one level deep, so a subpackage would be invisible to the reachability ratchet and absent from the map the Evolver is given |
| `git.py` | `GitRepository`, `GitIdentity`, `PublishPolicy` — per-invocation identity, never `git config` |
| `storage.py` | SQLite behind a repository; nothing else opens the database |
| `models.py` | Ollama / OpenAI-compatible providers, per-request model, `timeout_seconds` |
| `console.py`, `control.py`, `telegram.py` | the three channels onto the same command router |
| `architect.py` | the one-shot Agent Architect |
| `skills.py`, `permissions.py` | skill registry and path grants (application-level, not an OS sandbox) |

## Build / test / run

```bash
uv sync
uv run ruff check .
uv run pyright
uv run pytest
uv run evomesh          # console
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs exactly those four on Linux with a
mock provider — no Ollama, no GPU — plus a Windows job that builds the Control Center, runs its two
self-tests (`--self-test`, `--control-self-test`) and packages the desktop archive.

**On this machine, pytest needs its own temp root.** The shared temp directory is not writable for
this user, so every `tmp_path` fixture errors at setup. Validation inside a candidate already passes
`--basetemp` into the generation; when you run the suite by hand use `.pytest-tmp/`:
`uv run pytest --basetemp=.pytest-tmp`.

**Do not round-trip source files through `Get-Content`/`Set-Content`.** PowerShell mojibakes UTF-8
here; use the editing tools or `sed`.

## Load-bearing design rules

These are the reasons the code looks the way it does. Breaking one silently is how this project
regresses.

1. **The environment is explicit state, not a global singleton**, and SQLite sits behind
   `SQLiteRepository`. Model and channel contracts are replaceable; if a new feature needs a
   database handle or a provider, take it through the contract.
2. **Agents never call each other.** Direct and broadcast messages go through per-agent async
   mailboxes with correlation ids and durable audit records. A behavior that reaches into another
   agent object has broken the model the whole project exists to demonstrate.
3. **Every prompt is assembled under a hard character budget** (`runtime.prompt_chars`,
   `memory_chars`, `context_chars`, `inbox_chars`), in a fixed order: identity, goal, beliefs,
   plan, memory, working context, inbox. This is not tidiness — an oversized prompt gets truncated
   by the model server *from the oldest end*, which is exactly where memory lives. Budgeting means
   the trim is ours and the newest facts survive. Never append to a prompt outside the budget.
4. **`<think>` blocks are stripped before anything is stored or re-prompted**, including the case
   where the chat template opened the block and the model returns only the closing tag.
   `strip_reasoning` is the one place that happens.
5. **BDI is the actual loop, not a set of BDI-shaped fields.** Beliefs are *keyed*, so a percept
   revises the belief it contradicts instead of stacking a near-duplicate; beliefs come only from
   perception, while what an agent *concludes* is durable memory. Reconsideration is **pure code
   and never calls a model**: it fires only when the plan ran out, the goal closed, a higher
   priority goal appeared, or a belief the plan *declared a dependency on* was revised. Which
   beliefs a plan depends on is a deliberate choice — the Evolver depends on
   `evolution.awaiting_human`, **not** on `evolution.stage`, because its own plan is what moves the
   stage and depending on it would make it abandon and re-adopt its plan every cycle.
6. **Means-ends reasoning is a library lookup first, a model call second.** Guardian, Evaluator and
   the Evolver run entirely from plan libraries and keep reasoning with no model reachable at all.
   A generic agent gets **one** planning call per goal, not per cycle. If the model is down or
   returns something that is not a plan, the agent commits to a single-step plan and carries on.
7. **One cycle advances exactly one evolution stage** (`plan → propose → validate → repair →
   report → await-human`). This is what keeps a tick from becoming a ten-minute validation run, and
   what stops a second candidate being opened while one still waits for a human.
8. **Candidates are copies; supervisor metadata lives outside them.** `active`,
   `last_known_good`, `candidate` and `restart_required` are written atomically under
   `generations/`, never inside the candidate tree — the rollback path has to outlive a process that
   may not come back.
9. **"Not validated" is not "failed".** A run blocked by the host (permission error, full disk,
   unreachable network — see `ENVIRONMENT_MARKERS`) skips repair, keeps the candidate's status, and
   is reported as not validated. Nothing was learned about the candidate either way, and burning a
   repair attempt on the host's fault is worse than useless. `auto_promote` deliberately refuses to
   act where there is no verdict.
10. **Repair is cheapest-first and bounded.** `ruff check --fix` before the model; then one file at
    a time with the exact command, its real output and the file the last change wrote. It stops on
    a pass, on a byte-identical failure (proof the repair changed nothing), or at
    `evolution.max_repairs`.
11. **Git is the lineage.** Promotion cherry-picks the candidate's commit onto the checkout and
    moves the metadata — one canonical tree, one commit to reset to. Two refusals are deliberate and
    must stay: a generation is **never** applied over uncommitted changes (the candidate is parked,
    not discarded — the candidate is fine, the destination is not), and a pick that does not apply
    cleanly is aborted rather than left half-applied.
12. **The mesh commits under its own identity, passed per invocation** (`git.author_name` /
    `author_email`, default `Mesh Evo Agent`). It never edits the checkout's git config, and it
    signs correctly on a machine with no `user.name` at all. **The push is the last step, never a
    gate**: a missing remote or unusable credentials leave the generation landed and report the
    reason under `published:`.
13. **Exit code 86 means "start me again".** It is deliberately not 0, so a plain `/exit` is never
    mistaken for a restart request. Whoever launched the process (Control Center,
    `start-evomesh-console.bat`, `run-supervised.ps1`) brings it back up and the durable
    `restart_required` flag clears on boot.
14. **Unreachable code fails validation, as a ratchet.** `tests/test_reachability.py` walks the same
    import graph `codebase.py` builds. Modules that were already dead are listed in
    [docs/evolution/known-dead-modules.txt](docs/evolution/known-dead-modules.txt) and tolerated;
    anything new is not. That file is also the Evolver's backlog — wiring one of those into a
    running module just makes its line stale. **Do not "clean up" by deleting the list.**
15. **status and phase are different things.** `status` is the persisted desired lifecycle
    (`candidate`/`active`/`stopped`); `phase` is what the agent is doing right now, rebuilt on every
    boot and never read from disk. An agent that cannot start reports `offline` with a reason
    instead of coming back labelled `active` with no loop behind the label.

    **Amended by the harness queue: the phase list gained `awaiting-harness`.** It is a phase and
    not a status, because an agent waiting on a job is `active` and is doing something.
    Considered and rejected: reusing `waiting-human` — the two read identically on a roster and
    mean opposite things, one blocked on a person who may never return and one on a worker that
    certainly will. The phase is *derived* in `runtime_states()` from the queue, never stored, and
    an `offline` agent stays offline: a queued job cannot revive an agent with no loop.
16. **Runtime dependencies stay at five** — `aiosqlite`, `httpx`, `pydantic`, `pydantic-settings`,
    `pyyaml`. A local-first runtime that drags in a framework has stopped being the thing. Add one
    only with an argument recorded here.
17. **The control port binds `127.0.0.1:8765` only.** This is why the Telegram bot cannot stop the
    mesh: a shutdown from a phone would leave nothing running that could be asked to start again.
    Adopted Telegram chat ids are kept in the database, **not** written back into `evomesh.yaml` —
    the mesh editing a human's config file from the inside is a surprise nobody asked for.
18. **`memory.md`, `context.md` and the world context are plain Markdown on purpose.** A human can
    read or edit them while the mesh runs and the agent picks the change up next cycle. Keep them
    human-readable; do not "optimise" them into a binary or a database blob.

## How a generation is authored

**A generation is a harness job in the candidate workspace.** The propose stage builds an objective
(`mutation_objective()` — the project map, the standing rules, the ask), submits it, and the worker
runs the tool loop; the repair stage does the same with the failing command and its real output.
There is **no `FileMutation`, no `parse_mutation`, no whole-file JSON contract** — it was deleted
rather than kept beside the new path, because two mutation paths is one path nobody exercises.

What that bought, and what it cost, are both worth knowing:

- **The record comes from what was written, not from what the model said.** `record_harness_changes`
  reads the session's `edit`/`write` entries, so a model that claims two files and touches three no
  longer writes its own history. Each `GenerationChange` carries the unified diff, and
  `docs/evolution/<n>.md` prints it.
- **A job that changed nothing is not a candidate.** The pipeline reports it and stops rather than
  validating an untouched copy — which would pass, and a generation that passes while changing
  nothing is the dead-module failure wearing a verdict.
- **The propose stage now spans several cycles** (submit, observe, record) and is still one stage:
  the run is in the worker and the cycle only checks its handle. That is what keeps rule 7 true.
- **The environment grants the agent the root it hands out.** A candidate is a directory the mesh
  created *for that agent to work in*, and the harness runs under that agent's grants — so without
  the grant every tool is denied, which is exactly what the first real generation discovered. The
  grant is scoped to that one disposable copy and is visible in `/grant` like any other.
- **There is deliberately no `validate` tool.** Validation is a five-minute subprocess with its own
  stage and its own verdict; behind a tool a model could burn its whole step budget on `uv sync`,
  and "not validated" (rule 9) would become something the model reports about itself.

Three properties of the loop are load-bearing and are the reasons it is shaped this way:

- **A model with no tool calling drives the same tools through a text protocol**, permanently for
  that job once the provider has refused once. Most models that fit on a small card cannot call
  tools, so this is not a nicety — it is what makes the harness run on this project's target
  hardware. Re-trying native tools each turn would spend a step budget rediscovering the refusal.
- **The tool truncates, and says what it withheld** (rule 3 applied to tool output). A silent trim
  makes the model believe it has seen a whole file.
- **A refusal is a tool result, never an exception.** Containment, grants, bad arguments and unknown
  tool names all come back as text the model can act on; only a host failure ends the job. A loop
  that dies on the first denied path cannot work under least privilege at all.
- **`edit` refuses a target that is not unique, and that refusal is the reason it exists.** A
  replacement that silently takes the first of three matches produces a candidate that passes ruff,
  pyright and pytest and does the wrong thing — strictly worse than the whole-file rewrite it
  replaces, because that one fails loudly. The refusal carries the match count *and the lines around
  each match*, so widening the anchor costs no second read. Do not add an `occurrence: 2` argument:
  it lets a model that cannot widen an anchor guess an index instead, and every wrong guess is a
  silent wrong edit.
- **A harness job is asked for, not done.** An agent submits to `HarnessQueue` and keeps cycling;
  the worker runs the tool loop and the result comes back as an **ordinary inbound message**
  (rule 2), with the audit record every message gets. This is what keeps rule 7 true — the run
  happens in the worker, the cycle only checks its handle, so one tick still advances one stage.
  One worker by default: two tool loops on one card queue inside the GPU, where nothing can see
  them, instead of in a queue where `/harness status` can. A second submit from an agent that
  already has an open job **returns the first handle**, because a behavior submitting once per
  cycle would otherwise fill the queue with copies that all edit the same files. The queue is
  **not durable** and stopping the mesh cancels what is in it — reported to the submitter as a
  message, because the only thing worse than a cancelled job is a plan step no event will finish.
- **Writing is two gates, not one.** `harness.enabled` turns the harness on; `harness.allow_write`
  decides whether any job may change a file. A read-only job is not given the write tools at all,
  and a writing job on a mesh that forbids writes gets a refusal *naming the setting* — which the
  model can report — rather than a capability that silently is not there.

`plans/` is the maintainer's build briefs and is **gitignored in full**, so a fresh clone has none of
it; a phase cited by number is a pointer into that working copy. The decisions those briefs settle
land here, in `CHANGELOG.md` and in `docs/evolution/`, which is where a reader was going to look.
When starting a phase, read its brief first; when writing one, read `plans/CLAUDE.md` — the format,
the 250-line budget and the eight-item release checklist live there.

## Where EvoMesh is published

Four sibling checkouts under `D:\Projects\Dev-art solutions\`. Anything shipped is expected to reach
all of the relevant ones — a feature that exists only in this repository has not been released.

| Property | Repo | What it is |
|---|---|---|
| Code | this repo → `github.com/Dev-Art-Solutions/EvoMesh` | source, releases, the Windows desktop archive |
| Docs | `../evomesh.devart.solutions` → `evomesh.devart.solutions` | the documentation site |
| Company site | `../Website` → `devart.solutions` | the EvoMesh product card, hero copy and footer link |
| Blog | `../blog.devart.solutions` → `blog.devart.solutions` | the release write-ups |

**The docs site** is one static document, `src/index.html`, with no build step; assets, vendored
fonts and vendored Bootstrap/highlight.js live under `src/assets/`. Its theme is a Renaissance
engineering notebook ("draw the machine like Leonardo, document it like an engineer") and its rules
are in that repo's README — the ones that bite: every visual value is a CSS custom property in
`:root` (never hard-code a colour), **section ids are public URLs and must be preserved**, a new
section must be registered in *both* the desktop index and the offcanvas index, and fonts are
vendored deliberately because a local-first project should not make its documentation call a font
CDN. Keep every example synchronized with the real CLI and `evomesh.yaml`, and never describe
planned or experimental behaviour as complete.

**The company site** mentions EvoMesh in three places in `index.html` — the services copy, the
product card, and the footer link. Touch it when the positioning changes, not on every release.

**The blog** is a Next.js + MongoDB app, and posts are published through the `devart.solutions`
MCP connector, not by editing the repo. Four things about it are learned the hard way:

- the connector is **insert-only** — no update, no delete, and the slug locks on creation, so the
  post must be complete and correct in one shot;
- published posts live at `https://blog.devart.solutions/blog/<slug>`. `devart.solutions/blog`
  returns 404 and that is *not* a failed post;
- the blog is behind a Cloudflare WAF that blocks any request whose body contains a shell command,
  so a post shows the JSON, never the `curl`;
- the connector's session id expires roughly every 20–25 minutes and clears itself — retry rather
  than giving up, and **never publish a second copy** of a post that may have landed.

Two EvoMesh posts exist so far: *"our self-evolving runtime never evolved anything"*
(`evomesh-agents-that-actually-deliberate`) and *"we asked an agent what it was working on, and it
told us what was broken"* (`evomesh-asking-an-agent-what-it-is-working-on`). Both follow the house
shape: name the thing that was broken, in the title.

## Release cadence

Implement → keep `ruff`/`pyright`/`pytest` green → bump `<version>` in `pyproject.toml` → update
`CHANGELOG.md` and `README.md` → tag `v0.1.0-alpha.N` → update the docs site if any documented
behaviour changed → write the blog post. Generations the mesh lands are *not* releases: they are
ordinary commits with `docs/evolution/<number>.md` beside them.

## Code style

- `from __future__ import annotations` at the top of every module; Pydantic models for anything
  persisted or parsed; `StrEnum` for closed sets.
- Ruff line length 100, `E,F,I,UP,B,ASYNC`; Pyright `standard` over `src` and `tests`.
- `asyncio_mode = "auto"` — async tests need no decorator.
- Comments are rare, and they explain **why**, never what. The existing ones are the model: read the
  block above `ENVIRONMENT_MARKERS` or the header of `known-dead-modules.txt` before writing one.
- Tests get an isolated cwd from `tests/conftest.py`, because a test that writes into the checkout
  overwrites the world context of a mesh running from the same directory, which then reports agents
  the test suite invented.
