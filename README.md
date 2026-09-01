<p align="center"><strong>EvoMesh</strong></p>
<p align="center">A local-first environment where agents live, collaborate, learn skills, and evolve safely.</p>

## What it is

EvoMesh is an experimental, open-source multi-agent runtime for local AI models. The environment owns agent lifecycle, messaging, persistent state, skills, permissions, model access, health, and evolutionary history. Agents communicate through the environment rather than private network endpoints.

## Why it exists

Most agent systems treat agents as prompts around API calls. EvoMesh treats them as persistent participants in a shared world, with structured minds, reusable capabilities, explicit access grants, and a generational path for improving the runtime itself.

## Current status

Version 0.1.0-alpha.2 is an early runnable foundation. Implemented: the console, SQLite persistence, asynchronous messaging, system-agent bootstrap, a goal-driven execution cycle for every agent, file-backed agent memory, budgeted prompts for small local models, a one-shot Agent Architect, BDI-shaped state, local model adapters, filesystem grants, built-in skills, isolated candidate workspaces, an autonomous evolution pipeline with self-repair, supervisor metadata, and automated tests. Experimental: model-authored mutations and manual generation promotion. Planned: richer mutation authoring, stronger OS sandboxing, web/Telegram channels, and autonomous promotion policies.

## Core ideas and architecture

```text
Human -> Console Channel -> EvoMesh Environment
                              |-- Agent Registry / Lifecycle
                              |-- Async Message Bus -> Mailboxes
                              |-- SQLite State
                              |-- Workspace -> memory.md / context.md
                              |-- Skill Registry -> Permission Policy
                              |-- Local Model Providers
                              `-- Candidate Workspace -> Validator

Every agent runs two loops:

  mailbox loop   inbound message -> budgeted prompt -> reply
  cycle loop     perceive -> revise beliefs -> reconsider -> execute one plan step

Supervisor metadata (outside candidate) -> ACTIVE / LAST-KNOWN-GOOD / CANDIDATE
```

The environment is explicit application state, not a global singleton. SQLite access sits behind a repository. Model and channel contracts are replaceable. Generation supervisor metadata lives outside candidate workspaces. A behavior object decides what one cycle does, so an agent's autonomy is replaceable without touching the runtime.

## Quick start

For Windows, the easiest installation is the self-contained desktop archive from [GitHub Releases](https://github.com/Dev-Art-Solutions/EvoMesh/releases). Extract it to a writable directory, install `uv`, and run `start-evomesh.bat`. The archive includes the .NET desktop runtime; Ollama remains optional.

To run from source, use the following steps.

Prerequisites: Python 3.13+, [uv](https://docs.astral.sh/uv/), Git, and optionally Ollama.

```bash
git clone https://github.com/Dev-Art-Solutions/EvoMesh.git
cd EvoMesh
uv sync
cp evomesh.yaml.example evomesh.yaml
uv run evomesh
```

On PowerShell use `Copy-Item evomesh.yaml.example evomesh.yaml`. EvoMesh still boots when a configured local model is absent and reports an actionable provider warning; agent inference requires the provider to become available.

### Windows Control Center

On Windows, double-click `start-evomesh.bat`. It synchronizes the Python environment and opens the WinForms Control Center. From there you can:

- start and stop the mesh;
- automatically attach to an already running local mesh through the localhost control channel;
- chat with the selected agent and send slash commands;
- ask Agent Architect to create a new agent;
- start or stop individual agents;
- dynamically load installed Ollama models into dropdowns in both Agents and Settings, and assign a different provider/model to each agent;
- inspect live agent phases, goals, memory, and the shared world context;
- edit `evomesh.yaml` while the mesh is stopped.

Settings that require a restart are visible but disabled while EvoMesh is running. Per-agent model changes remain available at runtime and safely restart only the affected agent. For direct terminal use, run `start-evomesh-console.bat`.

Settings also exposes provider and model assignments for the four built-in agents: Agent Architect, Guardian, Evaluator, and Environment Evolver. These assignments are stored under `system_agents` in `evomesh.yaml` and are reconciled with persisted agent state on the next mesh start.

The local control channel listens only on `127.0.0.1:8765`. Closing the Control Center detaches without stopping the mesh; use **Stop Mesh** for a graceful shutdown. Startup diagnostics are persisted in `.runtime/logs/control-center.log`, and mesh logs in `.runtime/logs/mesh.log`.

## Local model configuration

`evomesh.yaml` configures Ollama (default), InferHub, or another local OpenAI-compatible endpoint. No cloud AI account is required. For Ollama, install the configured model, for example `ollama pull qwen3`. InferHub is optional and uses its OpenAI-compatible local endpoint.

EvoMesh is built for small context windows. `runtime.prompt_chars`, `memory_chars`, `context_chars`, and `inbox_chars` cap every prompt, and the defaults suit a 4k-token model; raise them if your models are larger. Reasoning-model `<think>` blocks are stripped before anything is stored or re-prompted, including the common case where the chat template already opened the block and the model returns only the closing tag.

Each provider also takes `timeout_seconds` (default 600). A 30B model on a busy GPU can need minutes for one answer, and a timeout is reported by name rather than as an empty error.

Each agent persists its own `provider` and `model_name`. The runtime passes that model on every inference request, so agents can use different Ollama models concurrently. Use the Agents tab in the Control Center or:

```text
/models ollama
/model "Research Agent" qwen3:14b ollama
/model "Fast Router" qwen3:4b ollama
```

## Agent Architect and agents

Run `/chat architect` and describe the agent you need in one sentence. Architect derives the name, purpose, constraints, filesystem access, skills, and provider/model from that sentence, gives the local model a single optional pass to improve the wording, and shows you a complete draft. It does not interview you: refine the draft by saying what to change (`name: Scout`, `model: ollama:qwen3:4b`, or any plain sentence, which sharpens the purpose). `/confirm` persists, starts, and selects it; `/cancel` discards it.

```text
evomesh> /chat architect
evomesh> read my markdown notes in D:/notes and write a weekly summary
Draft ready.
  name:     Notes Summarizer
  purpose:  read my markdown notes in D:/notes and write a weekly summary
  model:    ollama:qwen3
  skills:   Markdown.Read, Filesystem.Write, Filesystem.Read
  access:   D:/notes
  first goal: read my markdown notes in D:/notes and write a weekly summary
evomesh> /confirm
```

This matters most on small models. A six-question interview drifts and is forgotten before it ends, so no agent ever got created; one pass with deterministic fallbacks always produces a working definition.

## Goals and the execution cycle

Every agent runs a cycle on an interval, not only when spoken to. One cycle is one turn of the BDI loop described below: perceive, revise beliefs, reconsider whether the current commitment still holds, then execute a single step of the committed plan and write down what it learned.

The model is asked for a four-line answer (`STEP`, `RESULT`, `FACT`, `DONE`), which small models can actually produce; unformatted answers and `<think>` blocks are handled rather than discarded. A goal never closes on its first cycle, because a small model will rubber-stamp `DONE: yes` the first time it reads one. Goals marked recurring, including every system agent's standing goal and the purpose of an agent you create, never close at all.

```text
/goals <agent>                     what it is working on
/goal add <agent> "<text>" [prio]  give it something to do
/cycle <agent>                     make it think right now
```

Cadence and concurrency are controlled by `runtime.cycle_seconds` and `runtime.stagger_seconds`. Each agent serialises its own model calls, so a chat reply and a cycle never race for the same small model.

### Asking an agent what it is working on

You can simply ask, in chat, and the answer is not the model guessing. Every reply is prompted with a `CURRENT WORK` block the runtime fills in as the message arrives: phase, cycles completed, the goal and plan step in hand, the last finished step, the last error, and how long until the next cycle. A behavior that drives a pipeline of its own adds to it — ask **Environment Evolver** and you get the live stage (`plan`, `propose`, `validate`, `repair`, `report`, `await-human`), the candidate generation number, the objective, the file it changed, the workspace path, and the validation verdict, read at the moment you ask rather than remembered from the last cycle.

```text
evomesh> /chat evolver
evomesh> what are you working on?
Environment Evolver> Generation 3 is open for "Improve EvoMesh by one validated
candidate generation at a time". I am on the propose step, asking the model for
one small, safe file change; nothing is validated yet.
```

`/agents` gives the same picture for every agent at once, without spending a model call.

## Agent state

Status and phase are separate, which is what makes the roster honest:

- **status** is the desired lifecycle: `candidate`, `active`, `stopped`. It is persisted.
- **phase** is what the agent is actually doing right now: `offline`, `starting`, `idle`, `thinking`, `acting`, `waiting-human`, `error`. It is rebuilt on every boot and never read from disk.

An agent that cannot start is reported `offline` with the reason, instead of coming back labelled `active` with no loop behind the label.

```text
evomesh> /agents
Guardian [system] active/idle ollama:qwen3 cycles=4
    goal: Keep a current picture of mesh health and report anything degraded.
    last: All 4 agents healthy, provider ready.
```

## Memory: memory.md and context.md

Each agent owns two Markdown files under `workspace/agents/<agent>/`:

- **`memory.md`** durable facts it has learned, appended one line per cycle and compacted into a summary once it outgrows its budget;
- **`context.md`** its working context, rewritten every cycle with the current goal, the last outcome, the next step, its open goals, and its recent inbox.

`workspace/context.md` holds the shared world context the environment regenerates: the roster, each agent's phase and goal, and the evolution stage. All three are plain Markdown, so you can read or edit them while the mesh runs, and the agent picks the change up on its next cycle.

Every prompt is assembled under a hard character budget (`runtime.prompt_chars` and friends), carrying identity, goal, current beliefs, the committed plan, memory, working context, and recent inbox in that order. This is the fix for agents that lost their memory partway through a goal: an oversized prompt is truncated by the model server from the oldest end, which is exactly where memory sits. Budgeting here means the trim is ours and the newest facts always survive.

```text
/memory <agent>      /context <agent>      /context world
```

## BDI cognition

Agents run the Rao and Georgeff practical-reasoning loop, not a set of BDI-shaped fields:

```text
percepts := perceive()
B        := brf(B, percepts)      revise beliefs
D        := options(B, I)         which goals are worth having
I        := filter(B, D, I)       commit to one, with a plan
           execute one step of that plan
           drop I when achieved, or when it became impossible
```

**Beliefs** are keyed, so a new percept *revises* the belief it contradicts instead of stacking a near-duplicate beside it. Guardian holds `provider.ready` and `mesh.degraded`; the Evolver holds `evolution.stage`. Beliefs come only from perception. What an agent concludes during a cycle is durable memory, written to `memory.md`.

**Desires** are goals, ranked by priority. A behavior can generate new ones from what it now believes: when Guardian perceives that an agent stopped, it *wants* to investigate, and that desire becomes a goal that outranks its routine sweep. When the mesh recovers, the investigation is discharged rather than left open.

**Intentions** are commitments. Adopting one means picking a plan and then executing one step of it per cycle, across cycles, without re-deciding. That is the difference between having intentions and having impulses.

```text
evomesh> /intentions evolver
[active] plan 'evolve-generation' for: Improve EvoMesh by one validated candidate generation
    [x] open an isolated candidate generation
    [x] propose and apply one mutation
    [ ] validate the candidate
    [ ] repair the candidate while validation fails
    [ ] hand the candidate to the human
```

### Reconsideration

Re-deliberating every tick is as broken as never re-deliberating. An agent reconsiders only when something makes its commitment questionable: the plan ran out, the goal closed, a higher-priority goal appeared, or a belief the plan *declared it depends on* was revised. That check is pure code and never calls a model.

Which beliefs a plan depends on is deliberate. The Evolver does not depend on `evolution.stage`, because its own plan is what moves the stage — depending on it would make the agent abandon and re-adopt its plan once per cycle. It depends on `evolution.awaiting_human`, which flips only when it hands a candidate over and when you promote or discard it. So `/evolution discard` is what makes the Evolver drop its held plan and start a fresh pass, on its own.

While it waits on you it *holds* its intention: the step is not consumed, so it keeps one commitment instead of completing and re-adopting a plan every cycle it spends waiting.

### Plans

Means-ends reasoning is a library lookup first and a model call only as a fallback. Guardian, Evaluator and the Evolver run entirely from their plan libraries, so they keep reasoning with no model reachable at all. A generic agent asks the model once to break its goal into 2-4 steps, then spends the following cycles executing them — one planning call per goal rather than one per cycle, which is the difference between affordable and not on a small local model. If the model is down or answers with something that is not a plan, the agent commits to a single-step plan and carries on.

```text
/beliefs <agent>      what it currently holds true
/goals <agent>        its desires, by priority
/intentions <agent>   what it committed to, and the plan it is running
```

## Skills

The shared registry supports discovery, attachment, and invocation. Built-ins are `Filesystem.Read`, `Filesystem.Write`, `Markdown.Read`, `Markdown.Write`, `Git.Status`, and `Git.Diff`. A skill never grants path access by itself.

## Filesystem access grants

Use `/grant <agent> <path> read|write` and `/revoke <agent> <path>`. Paths are resolved before comparison, descendants inherit only the granted operation, and denials are structured exceptions. These are application-level controls, not an OS security sandbox.

## Messaging

Direct and broadcast messages use per-agent asynchronous mailboxes, correlation/conversation IDs, and durable audit records. Agent business behavior does not call other agent objects directly.

## Evolution and generations

The Environment Evolver is a staged pipeline, and one cycle advances exactly one stage:

```text
plan -> propose -> validate -> report -> await-human
                      ^           |
                      `- repair <-'
```

It opens a candidate generation on its first cycle after boot, so evolution actually starts rather than waiting to be asked. One stage per cycle means a tick never becomes a ten-minute validation run, and it never opens a second candidate while one is still waiting for you.

Candidates are copied into isolated generation directories and never overwrite the active tree. The supervisor tracks active, candidate, and last-known-good generations in atomic metadata. Validation runs `uv sync`, Ruff, Pyright, pytest, and the smoke check, and writes a result record; a candidate that was never validated is reported as `not validated`, not as failed. Promotion stays human-controlled, and promoting or discarding releases the pipeline for the next objective.

### Self-repair

A failed candidate is not automatically a dead one. When validation fails, the Evolver goes back and fixes its own work before it asks you for anything, and it does that in the cheapest order available:

1. **The linter's own fixer.** If Ruff reported findings it marked fixable, `ruff check --fix` runs and costs nothing. Losing a whole generation to `UP017` is not evolution, it is bookkeeping.
2. **The model.** Anything the fixer cannot touch — a Pyright error, a failing test — goes back to the model with the exact command, its real output, and the file the last change wrote, asking for one corrected file.

Each repair is followed by a full re-validation, and the pipeline stops repairing on whichever of these comes first:

- validation passes, and the candidate is handed over as passed after *n* repairs;
- the failure comes back byte-identical, which is proof the repair changed nothing;
- `evolution.max_repairs` attempts are used up.

Every outcome still ends in a verdict for you. A model that cannot author a usable repair reports the failure as it stands rather than resetting the pipeline and stranding the candidate. Set `evolution.max_repairs: 0` for the old single-shot behaviour, where the first failure is final.

```text
/evolution status
/evolution start <objective>
/evolution promote [n]   /evolution discard [n]   /evolution rollback
```

Set `evolution.autonomous: false` to park the Evolver, `evolution.auto_validate: false` to skip the validation suite, or `evolution.max_repairs` to bound how often it may fix its own candidate.

## Git history

Git is the intended evolutionary lineage. The initial substrate records mutation objectives/results and isolates code. Rich model-authored patches, structured mutation commits, generation tags, and promotion UX remain experimental follow-up work.

## Project structure

```text
src/evomesh/   runtime, contracts, bdi, cognition, memory, behaviors, storage, evolution
desktop/       Windows Forms Control Center
tests/         unit, integration, cycle, and restart scenario coverage
data/          local SQLite state (ignored)
workspace/     per-agent memory.md and context.md, shared world context (ignored)
generations/   supervisor metadata and candidate workspaces (ignored)
scripts/       development launchers
*.bat          one-click Windows launchers
```

## Development and candidate validation

```bash
uv sync
uv run ruff check .
uv run pyright
uv run pytest
```

CI runs the same checks on Python 3.13 without Ollama or a GPU by using a mock provider.

## Roadmap

Near-term milestones include a Skill Architect, richer mutation application and Git lineage, more capable long-term memory, a local web channel, Telegram, PDF skills, benchmark-based promotion, and stronger process isolation.

## Security / experimental status

EvoMesh executes local model output and is designed to modify candidate code. Treat it as experimental. Filesystem policies are enforced inside EvoMesh but are not a hardened sandbox. Review candidates before promotion, use least-privilege grants, keep backups, and never expose local model endpoints or EvoMesh state to untrusted networks without additional controls.

## Contributing

Open an issue before large architectural changes. Keep features local-first, preserve environment-mediated messaging and permissions, add deterministic tests, and update documentation to match what actually works.

## License

MIT. See [LICENSE](LICENSE).
