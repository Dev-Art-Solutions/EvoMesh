# Cognitive runtime baseline

This document records the pre-Milestone 1 architecture at commit `a9a4ba3` and the
incremental migration boundary for the cognitive-runtime program. It describes the
code that exists, rather than the target architecture in the implementation brief.

## Baseline and validation

- Branch: `main`, aligned with `origin/main` at `a9a4ba3` when the work began.
- Test collection: 805 tests.
- The ordinary Windows pytest temp root was inaccessible (`WinError 5` for
  `%LOCALAPPDATA%\\Temp\\pytest-of-AI`). The authoritative baseline therefore uses an
  isolated ignored path under `.runtime` with `--basetemp` and a local cache path.
- Complete isolated baseline: **805 passed in 178.57 seconds**.

## Current cognitive flow

`AgentRuntime.run_cycle` in `agents.py` chooses the current goal and calls the configured
behavior. `BDIReasoner.cycle` in `bdi.py` then performs the actual interpreter loop:

1. `BDIBehavior.perceive` creates percepts.
2. `MindState.revise` updates keyed beliefs.
3. `BDIBehavior.options` proposes `Desire` values and the reasoner adopts missing goals.
4. `BDIReasoner.reconsider` preserves the current commitment unless its plan ended, its
   goal closed, a higher-priority goal appeared, or one of its context beliefs changed.
5. `PlanLibrary.select` is consulted before model planning.
6. One `PlanStep` is executed, either deterministically in a behavior, through the
   harness, or through `CycleContext.think`.
7. `AgentRuntime._apply` records outcome, attempts, cadence, notes and completion.

This is already a real commitment-based BDI loop: a model-created plan is retained across
cycles instead of being regenerated every tick. The main Milestone 1 gap is that goals
and their completion/dependency policy are not yet first-class enough.

## Existing structured state

- `contracts.py`
  - `MindState` owns beliefs, goals and intentions and is persisted as part of each
    `AgentDefinition`.
  - `Goal` contains description, priority, attempts, recurrence/cadence, notes and basic
    status. It has no kind/parameters, graph relations, predicates, evidence, deadline,
    utility metadata or structured retry policy.
  - `Intention` contains the selected plan, ordered steps, cursor and relevant belief
    keys.
  - `Message` is a natural-language envelope with sender and recipient identifiers.
- `bdi.py`
  - `PlanRecipe` supplies a matcher, ordered text steps, one action type and context keys.
  - `PlanLibrary` selects the first matching recipe before the planner model is used.
  - There is no rule engine and no goal-manager service.
- `memory.py` keeps human-readable `memory.md` and `context.md` projections.
- `storage.py` persists whole `AgentDefinition` JSON documents in SQLite. This is the
  compatibility boundary for structured goals in Milestone 1: extending Pydantic models
  keeps existing rows readable without a destructive schema migration.
- `messaging.py` provides persisted delivery plus in-memory mailboxes; messages are not
  yet semantic ACL envelopes.
- `environment.py` wires repositories, agents, providers, behaviors, harness jobs and the
  evolver. It is the composition root for later cognitive services.
- `behaviors.py` contains configured Architect, Guardian, Evaluator and Evolver roles.
  Guardian and Evaluator already perform substantial deterministic work; Evolver still
  drives the complete improvement pipeline.
- `evolution.py` isolates candidates, validates them, repairs bounded failures, reviews
  diffs and promotes accepted generations. Pipeline state is stored through the generic
  repository state table rather than first-class improvement entities.
- `codebase.py` derives deterministic codebase evidence and also parses a Markdown
  improvement backlog. That `Improvement` type is a source-analysis record, not yet the
  persisted improvement lifecycle planned for later milestones.

## Persistence boundaries and SQLite schema

`SQLiteRepository` owns the database. Migration 1 creates:

- `agents(id, definition)` -- the complete agent definition, including `MindState`, as
  JSON;
- `messages(id, payload)` -- message JSON;
- `skills` and `agent_skills`;
- `filesystem_grants`;
- generic `state(key, value)` JSON for pipeline/runtime state;
- append-only `mutation_history` JSON.

Candidate metadata and validation results live in generation directories. Agent memory
and shared context are Markdown files under the configured workspace. Existing user rows
must remain readable as goal fields are added with defaults.

## Model-call inventory

| Site | Classification | Why it is called | Migration boundary |
| --- | --- | --- | --- |
| `CycleContext.think` | planning, execution, chat | BDI novel-plan creation, default step execution and direct replies | Known plans/rules/predicates must bypass it; later split into explicit services |
| `AgentRuntime._sanitize_report` | execution/formatting | Rewrites a final recurring report that failed deterministic filtering | Keep as bounded fallback after regex filtering |
| `AgentRuntime._summarize` | other/memory | Compresses long agent memory | Later memory separation and explicit synthesis service |
| `HarnessRunner._ask` | execution | Selects/calls tools through native chat or the text protocol | Keep bounded by harness step/time limits; later instrument by service/task |
| `Environment.request_model_inference` | other/general | Explicit external inference entry point, used by console/architect flows | Later route through named cognitive services |
| architecture drafting via environment inference | planning | Converts a human need into an agent definition | Already explicit user-driven planning |
| Evolver harness jobs | evolution/scouting, planning, execution, review, repair | Scout/plan/decompose/edit/review operations are encoded as bounded harness objectives | Replace progressively with backlog/coordinator/work-item roles; never self-certify |

Providers themselves (`models.py`) only implement transport (`generate`/`chat`); they do
not decide when cognition is required.

## Milestone 1 concrete change list

1. Extend `contracts.Goal` compatibly with kind/parameters, graph relationships, ownership,
   predicates, retry policy, progress/evidence/artifacts, utility inputs and deadlines.
2. Add a load-bearing `GoalManager` used by `MindState`/`BDIReasoner` for graph validation,
   runnable-state transitions, predicate evaluation, retries, recurrence and deterministic
   selection.
3. Add a transparent configurable scoring-policy interface with a deterministic default.
4. Add a bounded forward-chaining rule engine that can derive beliefs, propose goals, emit
   events and request deterministic actions; wire it before deliberation in the BDI cycle.
5. Extend `PlanRecipe`/`PlanLibrary` with stable trigger/precondition metadata and success/
   failure statistics while preserving callable matchers and existing behaviors.
6. Make plan completion consult explicit predicates when present. A model or an exhausted
   plan cannot close a goal whose success conditions are not proven.
7. Add focused unit tests plus a zero-model-call integration scenario: percept -> rule ->
   structured goal -> known deterministic plan -> explicit success predicate -> achieved.
8. Run Ruff, Pyright and all tests with an isolated pytest temp directory; record the
   before/after call-count measurement and remaining Milestone 2 boundary here.

## Migration constraints

- Existing description-only goals remain valid and retain their current behavior.
- Existing serialized agent definitions load through defaults; no row is rewritten merely
  by starting the runtime.
- Existing Python `PlanRecipe` definitions remain valid.
- Rule firing is capped and duplicate firings are suppressed within a cycle.
- New completion semantics are opt-in through explicit conditions until existing goals are
  migrated.
- No new model invocation is introduced by Milestone 1.

## Milestone 1 report

Completed against the staged Milestone 1 boundary.

Delivered:

- backward-compatible structured goal fields for kind/parameters, ownership, graph relations,
  predicates, deadline, retry policy, utility, progress, evidence and artifacts;
- a stateless `GoalManager` over persisted `MindState`, with dependency validation and cycle
  detection, automatic dependency/schedule/retry unblocking, distinct manual block reasons,
  deterministic utility selection, hard deadlines, bounded retry/backoff, parent/child handling,
  predicate evaluation and stall queries;
- explicit predicate types for belief equality, artifacts, tool results, child completion,
  validator results and human approval;
- a bounded forward-chaining `RuleEngine` wired between belief revision and deliberation, able to
  derive beliefs, propose deduplicated goals, emit typed seed events and request deterministic
  actions;
- stronger `PlanRecipe` matching/preconditions/provenance plus persisted per-agent selection and
  success/failure statistics;
- BDI completion semantics that refuse to close a predicate-backed goal merely because a plan ran
  out or a model claimed completion;
- the required architecture gap analysis and ADR for the structured-goal policy boundary.

Measured model-call behavior (repeatable in `tests/test_rules.py`):

| Scenario | Planning calls | Execution calls | Total model calls |
| --- | ---: | ---: | ---: |
| Novel description-only goal through `ReflectiveBehavior` | 1 | 1 | 2 |
| Percept -> rule -> typed goal -> known deterministic plan -> success predicate | 0 | 0 | 0 |

Final validation:

- `pytest`: **819 passed in 219.86 seconds** using an isolated `.runtime` base temp;
- Ruff over `src` and `tests`: **passed**;
- Pyright over `src`: **0 errors, 0 warnings**;
- focused goal/rule/BDI regression set: **74 passed**.

The next change set (Milestone 2) should begin with the detailed architecture intent's
instrumentation priority:

1. define explicit invocation reasons and cognitive service types;
2. instrument every provider call with agent/goal/service, prompt/output size, duration and result;
3. add deterministic/model-step counters and a repeatable metrics snapshot;
4. introduce compact task packets/context assembly behind the existing `CycleContext.think`
   compatibility facade;
5. split planning, interpretation, synthesis and failure reflection into narrow services;
6. then add logical memory separation, typed event dispatch and deterministic stall detection.

No Evolver decomposition should begin until `self-improvement-v2.md` defines its evidence,
lifecycle, role separation, budgets and post-deployment verification boundaries.
