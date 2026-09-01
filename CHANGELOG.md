# Changelog

All notable changes to EvoMesh are documented in this file.

## [Unreleased]

### Fixed

- SQLite runs in WAL with a 30s busy timeout. Every agent loop writes through its own
  connection, so the default rollback journal had writers taking exclusive locks on each
  other and a contended write surfaced as `database is locked` after five seconds --
  which reached a human as a failed test rather than a busy disk.
- Validation gives pytest its own `--basetemp` inside the candidate. On a host where
  the shared temp root is not writable by the running user, every `tmp_path` test
  errored at fixture setup, so no candidate could pass validation and each one was
  reported as failed for something it did not do.
- A validation failure that names a host problem -- a permission error, a full disk,
  an unreachable network -- no longer enters the repair stage or condemns the
  candidate. It is reported as not validated, and the candidate keeps its status.

### Added

- Promotion now lands the generation. The candidate's commit is cherry-picked onto the
  checkout the mesh runs from, so a promoted generation is code in the tree rather than
  a number in a metadata file.
  - Never applied over uncommitted changes in the checkout, and a pick that conflicts is
    aborted rather than left half-applied. Under a promotion policy either refusal parks
    the candidate for a human instead of discarding it.
  - `/evolution rollback` resets the tree to the commit the last promotion replaced.
  - `/evolution status` reports `RESTART REQUIRED` while the tree is ahead of the running
    process; starting the mesh clears it. The restart is deliberately manual -- a
    rollback path cannot live inside the process it may have to rescue.
  - Candidate scratch (`MUTATION_OBJECTIVE.md`, validation records) is ignored, so it
    never rides along into a promoted commit.
- `evolution.auto_promote` closes the loop. A candidate that validated is promoted, one
  that failed is discarded, and the next pass starts without asking anyone.
  - The policy acts only on a verdict validation produced. With validation off, or when
    the host blocked the run, the candidate still waits for a human.
  - Promotion moves supervisor metadata; it does not yet swap the code the running mesh
    executes.
- The evolution pipeline repairs its own candidates. A failed validation now enters a
  `repair` stage instead of going straight to a verdict, and re-validates afterwards.
  - Ruff's own `--fix` runs first and costs no model call, so a generation is no longer
    lost to an autofixable lint rule; only what survives it is sent to the model.
  - A model repair is prompted with the failing command, its real output, and the file
    the last change wrote, and must return one whole corrected file.
  - Repairing stops on evidence, not just on a budget: an identical failure means the
    repair changed nothing, and the candidate goes to the human as it stands.
  - `evolution.max_repairs` (default 2) bounds the attempts; `0` restores the previous
    single-shot behaviour.
  - A model that cannot author a usable repair reports the failure rather than resetting
    the pipeline, which previously stranded the candidate and opened another.
  - `/evolution status` reports the repair count, and the verdict reads
    `validation passed after 1 repair attempt`.
- Real BDI cognition. Agents run the Rao and Georgeff practical-reasoning loop:
  perceive, revise beliefs, generate options, commit to an intention with a plan, and
  execute one step of it per cycle.
  - Beliefs are keyed and revisable, so a percept replaces the belief it contradicts
    instead of stacking a near-duplicate beside it.
  - Behaviors can generate desires from what they now believe. Guardian wants to
    investigate when it perceives an agent stopped, and discharges that goal when the
    mesh recovers.
  - Intentions are commitments: a plan is adopted once and executed across cycles.
    Reconsideration is triggered only by the plan running out, the goal closing, a
    higher-priority goal appearing, or a revision of a belief the plan declared it
    depends on -- and never costs a model call.
  - An intention can be held rather than consumed while it waits on an external
    decision, so a parked agent keeps one commitment instead of one per cycle.
  - Means-ends reasoning is a plan library first, the model only as a fallback.
    Guardian, Evaluator and the Evolver now reason with no model reachable at all, and
    a generic agent makes one planning call per goal rather than one per cycle.
  - The Evolver's mutation pipeline is now a plan, visible as a checklist in
    `/intentions`, and `/evolution promote|discard` makes it reconsider on its own.
- Console commands `/beliefs` and `/intentions`.
- Beliefs and the committed plan are carried in every prompt, within their own budget
  (`runtime.beliefs_chars`), and written to `context.md`.
- Goal-driven execution cycle for every agent. Each agent now runs a mailbox loop and a
  cycle loop that advances its highest-priority open goal through perceive, deliberate,
  act, and reflect, on a configurable interval with a staggered first tick.
- File-backed agent memory: `workspace/agents/<agent>/memory.md` for durable facts and
  `context.md` for working context, plus a shared `workspace/context.md` world snapshot.
  Memory is compacted into a summary once it outgrows its budget.
- Character budgets for every prompt (`runtime.prompt_chars`, `memory_chars`,
  `context_chars`, `inbox_chars`), sized for small local models.
- Replaceable per-agent behaviors, with dedicated ones for Guardian, Evaluator, Evolver,
  and Architect.
- First-class goals with priority, status, attempts, progress notes, and a recurring flag.
- Observed agent phase (`offline`, `starting`, `idle`, `thinking`, `acting`,
  `waiting-human`, `error`) reported separately from the persisted desired status.
- Console commands `/cycle`, `/goals`, `/goal add|done|drop`, `/memory`, `/context`, and
  `/evolution start|promote|discard|rollback`.
- Settings sections `runtime:` and `evolution:`, and a `workspace_path`.
- Localhost control channel so the Windows Control Center can attach to an already running mesh.
- Persistent Control Center and mesh logs under `.runtime/logs`.
- Dynamic Ollama model dropdowns in both Agents and Settings, populated directly from the configured Ollama instance.
- Per-system-agent provider/model settings for Architect, Guardian, Evaluator, and Evolver.

### Changed

- Agent Architect no longer runs a six-question interview. It derives a complete draft from
  one sentence, uses at most one model call to improve the wording, and is refined by
  instruction rather than by answering questions.
- The Environment Evolver is a staged pipeline that opens a candidate generation on its
  first cycle and advances one stage per tick, parking at `await-human` until the candidate
  is promoted or discarded.
- System agents boot with a standing goal instead of an empty mind.

### Fixed

- Discarding a candidate generation left its directory behind while removing its
  metadata, so the next candidate collided with the leftovers and failed to open.
- The Environment Evolver never did anything: `EnvironmentEvolver` was never constructed by
  the runtime, so the agent was a chat echo with no pipeline behind it.
- Agent status was meaningless. Agents were persisted as `active` whether or not a loop was
  running, Architect was reported `active` while explicitly excluded from all loops, and a
  stale status from a previous run survived a restart. Status and phase are now separate,
  and an agent that cannot start reports `offline` with the reason.
- Agents lost their memory partway through a goal, because prompts carried no memory and
  were truncated by the model server from the oldest end.
- A goal could be closed by a small model rubber-stamping `DONE: yes` on its first look at
  it, leaving the agent idle forever.
- Guardian reported running agents as offline: its view of the mesh was snapshotted when it
  started rather than resolved per cycle, and it printed raw agent ids.
- A candidate generation that was never validated was recorded as failed.
- Candidate generations contained the live SQLite database and every agent's memory,
  because the workspace copy only skipped directories named `data` and `workspace`. It
  now excludes the configured state paths explicitly, which also removes a race between
  the copy and the running mesh.
- Every agent came back `stopped` after a restart: shutting the mesh down persisted the
  same status a human `/agent stop` does. A shutdown now leaves desired status untouched,
  and changing an agent's model no longer disables it.
- Agent Architect accepted whatever a small model returned, so a draft could be degraded
  to `name: D:/notes`, `name: agent`, or a purpose of `read`. Model output must now be a
  plausible improvement or the derived value stands.
- Reasoning-model `<think>` blocks leaked into stored memory and subsequent prompts.
- Saving settings from the Control Center silently dropped `workspace_path`, `runtime:`,
  and `evolution:` from `evomesh.yaml`.
- Robust Windows `uv.exe` discovery and actionable startup diagnostics.
- Explicit button foreground/background colors for readable labels across Windows themes.
- Settings model dropdown no longer closes while Ollama models are refreshed.

## [0.1.0-alpha.1] - 2026-08-30

### Added

- Local-first multi-agent runtime with persistent SQLite state and asynchronous messaging.
- Agent Architect interview flow for creating and starting agents.
- Per-agent Ollama and OpenAI-compatible provider/model selection.
- Built-in skills with explicit filesystem access grants.
- Candidate generation workspaces, validation, and supervisor metadata.
- Windows Forms Control Center for lifecycle, chat, agents, models, and settings.
- One-click Windows launchers for the Control Center and console.
- Self-contained Windows x64 release packaging and Windows CI validation.
- Repository-scoped NuGet configuration for deterministic public builds.

### Changed

- Agent model assignments can be changed at runtime by restarting only the affected agent.
- Restart-required settings are disabled in the Control Center while the mesh is running.

### Known limitations

- EvoMesh is experimental and its permissions are application-level controls, not an OS sandbox.
- Evolution promotion remains human-controlled.
- The packaged desktop application targets Windows x64; the Python runtime still requires `uv`.
- Local model weights and Ollama are not bundled.

[0.1.0-alpha.1]: https://github.com/Dev-Art-Solutions/EvoMesh/releases/tag/v0.1.0-alpha.1
