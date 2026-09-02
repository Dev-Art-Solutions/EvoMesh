<p align="center"><strong>EvoMesh</strong></p>
<p align="center">A local-first environment where agents live, collaborate, learn skills, and evolve safely.</p>

## What it is

EvoMesh is an experimental, open-source multi-agent runtime for local AI models. The environment owns agent lifecycle, messaging, persistent state, skills, permissions, model access, health, and evolutionary history. Agents communicate through the environment rather than private network endpoints.

## Why it exists

Most agent systems treat agents as prompts around API calls. EvoMesh treats them as persistent participants in a shared world, with structured minds, reusable capabilities, explicit access grants, and a generational path for improving the runtime itself.

## Current status

Version 0.2.0-alpha.1 is an early runnable foundation. Implemented: the console and Windows Control Center, SQLite persistence, asynchronous messaging, system-agent bootstrap, a goal-driven execution cycle for every agent, file-backed agent memory, budgeted prompts for small local models, a one-shot Agent Architect, BDI cognition with belief revision and reconsideration, local model adapters, filesystem grants, built-in skills, isolated candidate workspaces, an autonomous evolution pipeline with self-repair, generations committed and pushed under the mesh's own identity, restart-into-a-generation, Telegram as a second console, and a coding harness whose tools read, search, edit and write inside a job root, run by a worker off the cycle. The Evolver authors each generation through that harness, so a generation can change more than one file. Experimental: model-authored generations and manual promotion. Planned: transcript compaction, a shell tool, stronger OS sandboxing, and autonomous promotion policies.

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
- edit `evomesh.yaml`, including the Telegram bot and the git identity a generation is committed under.

The header status is checked continuously, not once at startup: a mesh you started from the launcher script, or one that restarted itself into a new generation, is picked up on its own, and the timestamp next to `RUNNING`/`STOPPED` says when that was last verified.

Settings are read when the mesh boots, so saving them while it runs offers to restart it for you. Per-agent model changes remain available at runtime and safely restart only the affected agent. For direct terminal use, run `start-evomesh-console.bat`, which also brings the mesh back up when it restarts into a new generation.

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

## The harness: a model that looks before it answers

Everywhere else in EvoMesh a model is asked one question and whatever comes back is the answer. The harness is the other shape: it hands the model tools, runs what it asks for, gives it the results, and asks again — until it answers without calling a tool, or a cap ends the job.

Three tools let it look — `read` (with line numbers and a window), `grep`, `ls` — and two let it act: `edit` and `write`. All five are confined to the job's root. Turn the harness on with `harness.enabled: true`; changing files additionally needs `harness.allow_write: true`, because turning it on to ask questions should never quietly grant it the ability to edit your checkout.

```text
evomesh> /harness ask "which module decides when an agent reconsiders?"
  grep {"pattern": "def reconsider"}
  read {"path": "src/evomesh/bdi.py", "offset": 210, "limit": 40}
bdi.py:reconsider() decides, and it never calls a model.
  3 steps, 11.2 s, 2 tool calls, native tools, session: .runtime/harness/000001.jsonl
```

```text
evomesh> /harness do "make second() add 2 instead of 1" 
  read {"path": "src/evomesh/dup.py"}
  edit src/evomesh/dup.py
    @@ -10,4 +10,4 @@
     def second() -> int:
         total = 0
    -    total += 1
    +    total += 2
  3 steps, 18.1 s, 2 tool calls, 1 read/1 changed, native tools
```

**`edit` refuses a target that is not unique.** If the text you asked to replace appears three times, the tool says so, shows the lines around each match, and changes nothing. That refusal is the reason the tool exists: a replacement that silently takes the first of three matches produces a change that passes every check and does the wrong thing, which is worse than the whole-file rewrite it replaces — that one at least fails loudly. `write` refuses to replace an existing file unless you pass `overwrite`, so creating and replacing stay different intentions.

**A `shell` tool exists, and it is off.** `harness.shell_allow` is an allow-list of program names; with nothing in it the tool is not offered to the model at all. There is no shell interpreter behind it — the command is split and run directly, so `|`, `&&` and `$(...)` are arguments rather than operators, and `curl x | python` is refused for `curl`. Everything runs in the job root, one command is bounded by `harness.shell_seconds`, and none of this is a sandbox: EvoMesh's filesystem policy is an application-level control, and a shell tool makes that sentence matter more rather than less.

**Models that cannot call tools still get to use them.** Most models that fit on a small card have no tool calling in their chat template, so the harness falls back to a one-line JSON protocol in plain text and drives the same tools through it. A harness that only worked on tool-calling models would not work on the hardware this project is built for.

Two things are deliberate. Tool output is truncated **by the tool**, which says how many lines it withheld and which offset asks for them — so a model can request the rest instead of silently believing it has seen a whole file. And a job that runs out of steps or wall clock ends as `capped`, not `failed`: it did not go wrong, it ran out of room, and those need different responses.

Every job writes one JSONL file under `.runtime/harness/`, flushed as it runs, so a job that hangs still leaves the whole story up to that moment.

### Any agent can be given the harness

`/harness grant "Notes Summarizer" D:/notes` lets an ordinary agent use the tools inside one directory, and `/harness revoke` takes it back. A granted agent takes a plan step with tools rather than with a prompt when the step starts with a looking verb — *investigate*, *find*, *search*, *check*, *inspect*, *review*, *diagnose* — and everything else stays a plain model call. The finding is written into the agent's memory as that step's outcome.

### An agent asks for a job rather than stopping to do one

A tool loop takes minutes; a cycle has to stay a tick. So an agent submits a job to a queue and keeps running — it still answers `/chat`, still reports what it is working on, still appears in `/agents` — and simply commits to nothing new while a job of its own is open. Its phase reads `awaiting-harness`, which is deliberately not `waiting-human`: one is blocked on a person who may never come back, the other on a worker that certainly will.

When the job finishes, the result arrives in the agent's mailbox **as an ordinary message**, with the audit record every message gets. Nothing calls back into the agent, and no behaviour has to know a worker exists.

```text
evomesh> /harness status
workers: 1, open jobs: 1
  job 2 [evolver] running, 7 steps: wire metrics into a module that runs
  job 1 [console] answered -- 4 steps, 67.1 s, 4 tool calls, native tools
The queue is not durable: stopping the mesh cancels what is in it.
```

`harness.workers` defaults to 1. Two tool loops on one card do not go twice as fast; they queue inside the GPU, where nothing can see them, instead of in a queue where you can. Stopping the mesh cancels open jobs and **tells the submitter** — the only thing worse than a cancelled job is a plan step no event will ever finish.

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

Some failures are not the candidate's to fix. A candidate is a copy of a tree that already validated, so when the output names a host problem — a permission error, a full disk, an unreachable network — no rewrite of one file would help. Those runs skip repair entirely and are reported as **not validated** rather than failed, and the candidate keeps its candidate status, because nothing was learned about it either way. Validation also gives pytest its own `--basetemp` inside the generation: on a host whose shared temp root this user cannot write, every `tmp_path` test would otherwise error at fixture setup and every candidate would be condemned for it.

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

### Deciding without a human

`evolution.auto_promote: true` lets the verdict decide: a candidate that validated is promoted, one that failed is discarded, and the pipeline starts the next pass instead of parking. The Evolver then runs a closed loop — open, mutate, validate, repair, decide — for as long as its goal stays open, paced by `evolution.cycle_seconds`.

The policy only ever acts on a verdict validation actually produced. With `auto_validate` off, or when the run was blocked by this machine, there is no verdict: promoting would ship unchecked code and discarding would throw away work for the host's fault, so it still stops and waits for you.

### What promotion actually does

Promoting cherry-picks the candidate's commit onto the checkout the mesh runs from, then moves `active` and `last_known_good` in the supervisor metadata. Git is the lineage, so the generation lands as an ordinary commit on an ordinary history rather than by swapping a directory — one canonical tree, and a commit to reset to when a generation turns out to be a mistake. `/evolution rollback` does exactly that reset.

Two refusals are deliberate. A generation is never applied over uncommitted changes in the checkout, so work in progress is never clobbered — under a promotion policy the candidate is parked for you instead of discarded, because the candidate is fine and the place it was going is not. And a change that does not cherry-pick cleanly aborts the pick rather than leaving the tree half-applied.

### Making the change count

A mutation used to be proposed with no picture of the code it was changing. The
Evolver did the only thing it could with that: it invented a plausible new
module. Those modules validated -- ruff, pyright, pytest and the smoke check are
all perfectly happy with well-written code nobody calls -- and landed as dead
weight. Ten of them accumulated, 431 lines that never execute.

Two things now stop that.

**The model is shown the package.** `evomesh.codebase` surveys `src/evomesh/`,
resolves the import graph, and hands the Evolver a map: which modules are
load-bearing and how many things depend on them, and which are dead. The
instruction that follows tells it to improve a module that runs, or to edit one
so it imports a dead module and brings that code to life. A brand new file is
named as the wrong answer, because with one file per mutation there is nothing
left to import it.

**Unreachable code fails validation.** `tests/test_reachability.py` walks the
same import graph and fails on any module that nothing imports and nothing runs.
A candidate that adds one goes to the repair stage, where the model is told to
wire it into a module that already runs rather than rewrite the orphan again.

It is a ratchet, not a cleanup order. The modules that were already unreachable
are listed in `docs/evolution/known-dead-modules.txt` and tolerated; anything new
is not. That list is also the Evolver's backlog: wiring one of those into the
running mesh is real work, and doing it just makes its line there stale.

### The backlog: why each generation exists

Every generation writes `docs/evolution/<number>.md` **into its own commit**,
alongside an updated index. The entry records the objective, every file it
touched with the reason the model gave, each self-repair, the validation
commands with their exit codes, and the output of whatever failed.

The reasoning travels with the change. A month later the question about any of
these commits is "why did it do that", and the answer is in the repository
rather than in a SQLite file on one machine.

### Publishing a generation

A landed generation is committed by the mesh, under its own identity, and pushed to the remote. Both halves matter: a history where the agent's commits are signed with whatever `git config` happens to hold is a history where nobody can tell the agent's work from their own, and a generation that never leaves the machine has not really shipped.

```yaml
git:
  author_name: Mesh Evo Agent
  author_email: mesh-evo-agent@evomesh.local
  auto_push: true
  remote: origin
  branch: ""      # empty means the branch the checkout is already on
```

The identity is passed to git per invocation, so the mesh never edits your checkout's configuration and still signs its commits correctly on a machine that has no `user.name` set at all.

The push is the last step, never a gate. A remote that is not configured, a detached HEAD, or credentials git cannot supply leaves the generation exactly where it was — in the tree, validated, promoted — and the reason is reported by `/evolution status` under `published:` rather than swallowed.

### Restarting into it

The running process executes the code it started with, so a landed generation does nothing until the process comes back up on it. With `evolution.auto_restart: true` (the default) it does:

1. the generation lands and is pushed;
2. `restart_required` is written to the supervisor metadata — a durable flag, because the rollback path has to outlive a process that may not come back;
3. every channel is told what is about to happen, and after `evolution.restart_delay_seconds` the process exits with code **86**;
4. whoever launched it — the Control Center, or `start-evomesh-console.bat` — brings it back up, and the flag clears on boot.

Exit code 86 is the whole contract: it means *start me again*, and it is deliberately not 0, so a plain `/exit` is never mistaken for a restart request. `/restart` asks for the same thing by hand, which is also what the Control Center offers after you save settings.

Set `evolution.auto_restart: false` to go back to being told rather than restarted; the flag is still raised and `/evolution status` still says `RESTART REQUIRED`.

Set `evolution.autonomous: false` to park the Evolver, `evolution.auto_validate: false` to skip the validation suite, `evolution.max_repairs` to bound how often it may fix its own candidate, or `evolution.auto_promote` to take yourself out of the loop.

## Telegram

Talk to the mesh from your phone. The bot is a second console onto the same running environment — every message goes through the same command router the Control Center uses, so `/status`, `/agents`, `/evolution status`, `/chat <agent>` and plain conversation all behave identically.

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token it gives you.
2. Open the Control Center's **Settings** tab, paste it under **TELEGRAM**, tick **Enable the Telegram bot**, and save. Saving while the mesh runs offers the restart that picks it up.
3. Send `/start` to your bot. With **Let the first chat claim the bot** on, that first chat is adopted and remembered; after that, strangers are turned away by chat id.

```yaml
telegram:
  enabled: true
  token: "123456789:AA..."
  allowed_chat_ids: []      # empty + adopt_first_chat: the first /start claims it
  adopt_first_chat: true
  poll_timeout_seconds: 30
  announcements: true       # say when a generation lands or the mesh restarts
```

Everything about the bot is driven from the Control Center's **Settings** tab, and
two buttons there answer the questions the settings file cannot:

- **Test token** asks Telegram directly, with the token in the box. It needs
  neither a save nor a running mesh, so a typo, a revoked bot or a missing
  network is reported before you commit to a restart. On success it names the
  bot: `@your_bot`.
- **Live status** asks the running mesh (`/telegram status`), and is the only
  place a chat that claimed the bot at runtime appears. Adopted chats are kept
  in the database rather than written back into `evomesh.yaml`, because the mesh
  editing a human's config file from the inside would be a surprise nobody asked
  for.

**Allow chat id** and **Revoke chat id** act on the running mesh through
`/telegram allow <id>` and `/telegram revoke <id>`, taking the last id in the
box. All four commands work from the console too.

Two things the bot deliberately will not do. It cannot stop the mesh — the control port only listens on localhost, so a shutdown from a phone would leave nothing running that could be asked to start again. And it remembers its update offset across restarts, so the command that triggered an automatic restart is not replayed by the process that comes back up.

## Git history

Git is the evolutionary lineage. Each generation is one commit, authored by the mesh, cherry-picked onto the checkout and pushed to the remote. Generation tags and richer model-authored patches remain follow-up work.

## Project structure

```text
src/evomesh/   runtime, contracts, bdi, cognition, memory, behaviors, storage, evolution,
               codebase (import graph), telegram
desktop/       Windows Forms Control Center
tests/         unit, integration, cycle, reachability, and restart scenario coverage
docs/evolution/ one entry per generation: what changed and why, written by the mesh
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
