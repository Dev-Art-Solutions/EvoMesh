# Cognitive architecture gap analysis

This analysis maps EvoMesh at commit `a9a4ba3` to the architectural intent in
`EVOMESH_ARCHITECTURE_INTENT_DETAILED.md`. It is a migration guide, not an argument for
parallel replacements of working subsystems. The baseline flow and model-call inventory
are documented in `cognitive-runtime-baseline.md`.

| Area | Current capability | Desired capability | Gap | Recommended change | Risk and migration notes | Tests required |
| --- | --- | --- | --- | --- | --- | --- |
| BDI | Real perceive/revise/options/reconsider/commit/execute loop; intentions survive cycles | Reconsider on all meaningful structured transitions and prefer deterministic layers | Dependency, deadline, event, delegation and predicate transitions were not represented | Keep `BDIReasoner`; feed it `GoalManager`, rules and later typed events rather than replacing it | Preserve commitment and the existing one-step-per-cycle behavior | Commitment, dependency change, predicate completion, higher-priority preemption, zero-LLM path |
| Goals | Persisted description, priority, attempts, cadence and notes | Typed goals with graph relations, predicates, utility, evidence, ownership and retry policy | Goal lifecycle was spread between `MindState`, `BDIReasoner` and `AgentRuntime._apply` | Extend `Goal` compatibly and centralize lifecycle policy in a stateless `GoalManager` over `MindState` | Old JSON rows must load with defaults; legacy description-only completion remains compatible until migrated | Legacy JSON, graph validation/cycles, transitions, retry/backoff, recurrence, each predicate type |
| Plans | Python `PlanRecipe`, callable match, ordered steps, context belief keys; library precedes model planning | Stable procedural abstraction with preconditions, outcome predicates, capabilities, provenance and persisted reuse statistics | Matching existed, but plan metadata/outcomes were too thin | Extend `PlanRecipe`; persist per-agent use/success/failure in `MindState`; add richer step types incrementally | Do not force YAML or invalidate existing recipes | Matching/preconditions, statistics, known-plan zero-call execution, failed-plan accounting |
| Memory | Budgeted `memory.md`, `context.md`, beliefs and intentions | Separate working, semantic, episodic and procedural memory with relevance retrieval | Durable text combines semantic and episodic material; procedural stats were absent | First expose logical repository interfaces and projections; migrate storage later without discarding Markdown | Avoid a large storage rewrite and keep human-readable projections | Compatibility readers, relevance/budget tests, episodic/procedural persistence |
| Events | Message queues, watcher announcements, runtime wake flags and scheduled cycles | Typed event dispatcher with deterministic handlers before model wakeups | Signals exist but are subsystem-specific and not a shared event vocabulary | Introduce typed events and dispatcher after rule/goal foundations; bridge existing wakes/messages | Avoid duplicate delivery and model wakeups for deterministic changes | Event ordering/deduplication, belief/goal events, zero-model handler, scheduled fallback |
| Rules | Deterministic behavior code and hard-coded routing predicates | Small bounded forward-chaining service that derives beliefs/goals/events/actions | No reusable rule abstraction or firing bound | Add a dependency-free `RuleEngine` in the BDI path; cap and deduplicate firings per cycle | Rules must not become an opaque second runtime | Matching, chained derivation, cap/loop protection, duplicate goal prevention |
| Agent communication | Persisted natural-language `Message` plus mailbox delivery | ACL performatives with correlation, goal/task references and structured payload | Runtime must interpret coordination from prose | Extend the existing envelope compatibly; keep free-form human chat | Existing console/Telegram messages must remain readable | Legacy message round-trip, each performative, correlation/reply, expiry and deterministic dispatch |
| Capabilities | Permissions, harness roots, tools, skills and behavior configuration imply capability | Explicit persisted capability declarations and deterministic queries | No common vocabulary or matcher | Add capability records to agent definitions and a registry derived partly from current grants/tools | Declared capability must never grant permission | Matching, unavailable-permission rejection, persistence, dynamic load/status |
| Delegation | Agents can send messages and harness jobs; Evolver owns an internal staged pipeline | First-class bounded `WorkItem`, assignment, result/failure and help flow | Delegated work is not durable structured state | Add repository interface and `WorkItem`; route by capability before adding Contract Net bids | Do not create many new LLM loops; roles can be services/configurations | Lifecycle/dependencies/retries, assignment/reassignment, structured result, parent completion |
| Stall detection | Cycle hang watchdog, attempt limits, harness time/step bounds | Algorithmic progress/failure signatures and goal/task stall events | Detects a hung call, not repeated ineffective work | Record step/failure/artifact deltas and evaluate a deterministic stall policy | Avoid treating long legitimate validation as stalled | Repeated signature, no-progress threshold, reset on progress, help/reassign/escalate |
| LLM invocation model | Calls are budgeted; known plans and some scheduled goals bypass planning | Named cognitive services, explicit invocation reasons, compact task packets and telemetry | `CycleContext.think` is still a generic planning/execution/chat gateway; direct call sites lack unified metrics | Milestone 2 should begin with an instrumented provider/service gateway and invocation reason enum, then split services | Instrument without changing provider dialects or prompt behavior first | Per-reason count/size/duration/failure, no-call scenario, compact packet budgets |
| Self-improvement | Isolated candidates, deterministic validation, bounded repair, review and promotion; codebase evidence sources | Evidence -> backlog -> triage -> bounded work items -> independent review/validation -> verify effect | `EnvironmentEvolver` still coordinates most stages; Markdown backlog is not a persisted lifecycle | Specify `self-improvement-v2.md` before changing Evolver, then introduce entities/services behind current pipeline | Preserve isolation, validators, promotion safety and human controls; no self-certification | Evidence/dedup/score, WIP/budget, review-vs-validation, verification window, ineffective outcome |
| Validation | Ruff/Pyright/pytest/candidate checks and a separate model-assisted review step | Deterministic validator independent from semantic reviewer and explicit success criteria | Existing separation is useful, but improvements are not first-class and post-deploy proof is absent | Reuse candidate validators; attach results to improvement/work-item lifecycle later | Never let implementing work edit its own validation/promotion boundary without approval | Validator immutability/role separation, criteria coverage, promotion gates |
| Observability | Runtime states, logs, harness sessions, mutation history and some metrics | LLM calls by reason/goal, deterministic ratio, plan/rule hit rate, delegation/stall/improvement outcomes | Cannot yet prove the architecture reduces inference | Add structured cognitive telemetry before broader Milestone 2 changes; build repeatable mock benchmark | Logging alone is insufficient; avoid high-cardinality unbounded storage | Metrics accuracy, bounded retention, repeated-vs-novel benchmark |

## Smallest migration sequence

1. Finish the existing Milestone 1 boundary: structured goal compatibility, `GoalManager`,
   bounded rules, richer plan metadata/statistics and the zero-model deterministic scenario.
2. Start Milestone 2 with invocation reasons and metrics, before changing prompt composition.
   This satisfies the detailed intent's “instrument and clarify” priority while preserving the
   staged plan's already-started cognitive-core dependency order.
3. Add compact task packets and explicit cognitive services behind the existing provider
   abstraction; keep `CycleContext.think` as a compatibility facade until callers migrate.
4. Introduce typed events and logical memory repositories, then deterministic stall detection.
5. Write and review `self-improvement-v2.md` before any significant Evolver decomposition.
6. Only after those foundations, add capabilities, ACL messages, WorkItems and delegation.

## Architectural decisions established by Milestone 1

- Structured goal state remains inside persisted `MindState`; `GoalManager` is a stateless
  policy service, not a second database or competing source of truth.
- Description-only goals and existing serialized definitions remain valid.
- Explicit predicates opt into deterministic proof; legacy goals retain their existing behavior
  until migrated.
- Rule firing happens after belief revision and before intention reconsideration, is bounded,
  and introduces no model call.
- Known-plan selection stays ahead of model planning, and plan usage is structured state.
- Permissions remain independent from goal, rule and capability declarations.

## Immediate risks to watch

- A manually blocked goal must not be auto-unblocked as if it were merely waiting on a
  dependency; blocked reasons therefore need to distinguish dependency/schedule/retry waits
  from impossibility or human intervention.
- A finished plan with unmet explicit predicates must not close the goal; it must be
  reconsidered or escalated under bounded retry/stall policy.
- Utility scoring must preserve existing priority behavior by default and expose every weight.
- Rule effects must not create the same open goal on every cycle.
- Metrics added next must observe calls without silently changing prompts, provider routing or
  validation behavior.
