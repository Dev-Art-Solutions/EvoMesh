# Cognitive Runtime Bypass Audit

Date: 2026-09-25  
Scope: production paths under `src/evomesh` at `c05e2e4`  
Authority targets: `GoalManager`, `CognitiveModelService`, coordination,
`Blackboard`, `ImprovementControl`, `ProcedureLearner`, `EventBus`.

This is an execution audit, not a desired-component checklist. Provider
implementations are transport and therefore intentionally call their own HTTP
clients; tests may call providers directly. All other findings below refer to
normal runtime behavior.

## Inventory summary

### Model-call paths

| Path | Operation/reason | Boundary status |
|---|---|---|
| `BDIReasoner._plan_with_model` via `CycleContext.think` | novel plan / no plan match | compliant |
| `BDIBehavior.execute` via `CycleContext.think` | step reasoning / plan step requires reasoning | compliant |
| `BDIBehavior.respond` via `CycleContext.think` | chat / human response | compliant |
| `AgentRuntime._sanitize_report` | report formatting / deterministic format rejected | compliant |
| `AgentRuntime._summarize` | memory summary / memory budget exceeded | compliant |
| `HarnessRunner._ask` native and fallback | tool loop / tool selection requires model | compliant |
| `Environment.request_model_inference` | direct inference / explicit request | compliant, intentionally broad external API |

Only `CognitiveModelService.generate/chat` calls `provider.generate/chat` in
production runtime code. No P0 boundary bypass was found.

### Goal-selection paths

`MindState.next_goal/open_goals` delegate to `GoalManager`; BDI deliberation
calls `GoalManager.next_goal`. Agent status/context/reporting uses the compatible
`MindState` facade. The conflicting path is BDI reconsideration, which compares
raw priorities instead of GoalManager scores and policy.

### Delegation paths

Structured delegation flows through `WorkItem`, `CapabilityRegistry`,
`ContractNet`, ACL messages, Blackboard state and delegated goals. The harness
`ask_agent` tool remains a free-form synchronous question/reply path and does
not create a WorkItem. Human chat is intentionally free-form and is not a
delegation bypass.

### Improvement-selection paths

Evidence-backed candidates are synchronized, scored and selected through
`ImprovementControl`. When no controlled item is selected, `EvolverBehavior`
still directly chooses scout, dead-module and untested-export work. Those
fallbacks are concrete and deterministic, but remain outside the improvement
authority and evidence lifecycle.

## Findings

| ID | Priority | File / symbol | Old behavior / bypass | Authoritative replacement | Risk | Migration test | Status |
|---|---:|---|---|---|---|---|---|
| B-001 | P0 | `bdi.py:BDIReasoner.reconsider` | Preempts only when another goal has a numerically lower raw priority. Ignores utility, deadline score, threshold and non-preemptible policy. | `GoalManager.should_preempt` using one configurable policy. | Thrashing or failure to preempt urgent/high-value work; competing scheduler semantics. | Utility beats raw priority; below-threshold commitment stays; deadline override; non-preemptible kind. | migrated |
| B-002 | P0 | `agents.py:AgentRuntime._apply` | Writes `ACTIVE`, `STALLED` and `DONE` directly and separately implements recurrence scheduling. | `GoalManager.transition`, `complete`, `record_failure`, `mark_stalled`. | Illegal transitions and restart-dependent lifecycle drift. | Every runtime outcome follows the state machine; recurrence schedule has one implementation. | migrated |
| B-003 | P0 | `bdi.py:BDIReasoner._execute` | Writes `BLOCKED` directly for impossible steps; plan exhaustion/result prose can become `goal_done` when no success predicate exists. | GoalManager transition/evaluation plus explicit compatibility completion policy. | Model prose or empty plan can certify completion; invalid transitions bypass policy. | Exhausted plan without completion evidence does not close structured goal; impossible transition is validated. | migrated |
| B-004 | P0 | `console.py:ConsoleChannel._command_goal` | Human done/drop writes terminal status directly and maps drop to `FAILED`. | Explicit GoalManager human override / cancel transition. | Wrong audit meaning and no legal-transition enforcement. | `/goal done` records DONE; `/goal drop` records CANCELLED; both persist and cannot be reopened by a late cycle. | migrated |
| B-005 | P0 | `goal_manager.py:GoalManager.refresh` | Failed dependencies are treated like incomplete dependencies and block forever; no explicit dependency-failure policy. | Refresh policy that fails, cancels or explicitly blocks dependants with provenance. | Permanent silent deadlock. | Failed dependency deterministically moves dependant according to configured default and emits a change. | migrated |
| B-006 | P1 | `contracts.py:MindState.add_goal`, BDI desires, console/templates | Callers can append goals without `GoalManager.create`, so graph validation and lifecycle initialization are optional. | GoalManager creation entry point; retain a compatibility facade that delegates. | Invalid references/cycles can enter persisted state. | All public creation paths reject dependency cycles and missing references. | open |
| B-007 | P1 | `environment.py:_unblock_goal_dependents` | Event handler scans all agents and infers a BLOCKED→RUNNABLE transition around `refresh`. | GoalManager returns transition records/events; environment only dispatches them. | Duplicated transition detection and missed status changes. | One dependency completion produces one idempotent `GOAL_UNBLOCKED`. | open |
| B-008 | P1 | `harness_tools.py:ask_agent`, `environment.py:_make_ask_agent` | Agent-to-agent work can be transferred as free-form synchronous text with no WorkItem, capability check, budget or durable result. | Structured WorkItem delegation for task-like requests; keep query-only ACL for simple questions. | Hidden work, duplicated status polling and unrestricted routing. | Task request creates bounded WorkItem; query remains chat; missing capability rejects deterministically. | open |
| B-009 | P1 | `behaviors.py:EvolverBehavior._plan` fallback branches | Scout/dead-module/untested targets can be selected outside `ImprovementControl`. | Convert each deterministic source into evidence-backed candidates before selection. | Two improvement authorities and inconsistent WIP/verification. | Every opened autonomous generation has an `improvement_id` and evidence. | open |
| B-010 | P1 | `cognition.py:CycleContext.build_prompt` | Reads full budget slices of memory/context/world and recent inbox before proving relevance. No selection provenance is recorded. | Relevance-first section budgets and out-of-prompt provenance records. | Small models receive stale/irrelevant context; prompt efficiency cannot be explained. | Oversized irrelevant memory is excluded; task/goal/contract survive; provenance names every included source. | migrated |
| B-011 | P1 | `cognitive_services.py:ContextAssembler` | Clipping preserves the first section but can discard task/output contract, and section budgets are not explicit. | Priority-aware per-section allocator that always preserves task, goal and output contract. | Model may see context but lose the requested operation or schema. | 4k/8k packets retain required sections and remain within budget. | migrated |
| B-012 | P1 | `blackboard.py:Blackboard.publish_fact` | A fact with the same key replaces the prior source/value silently. | Versioned/conflict-preserving fact records or source namespace. | Contradictory shared knowledge loses provenance. | Conflicting publishers remain inspectable; expiry does not erase history unexpectedly. | open |
| B-013 | P1 | `events.py:EventBus`, Environment subscriptions | Dispatch is globally keyed only by event type; relevant beliefs/goals are filtered after wake or not at all. No burst coalescing/backpressure. | Filtered subscriptions and per-agent coalesced wake queue. | Event storms and unnecessary cycles. | Irrelevant event does not wake; burst wakes once; duplicate is idempotent. | open |
| B-014 | P1 | `procedural_learning.py`, `contracts.py:LearnedProcedure` | Matching is conservative but identity lacks parameter schema/capabilities/predicates and degradation policy is limited. | Declarative procedure v2 lifecycle and validator-backed demotion. | Reuse in incompatible context or stale capability set. | Schema mismatch refuses reuse; failures demote; restart preserves statistics. | migrated |
| B-015 | P1 | `coordination.py:ContractNet` | Success history is global per agent tuple, not capability/task-specific; WorkItem lacks requester/deadline/result references and NEEDS_HUMAN. | Measurable task/capability history and complete lifecycle. | Wrong agent ranking and incomplete recovery semantics. | Capability-specific history changes winner; budget exhaustion escalates. | open |
| B-016 | P1 | `environment.py:_assist_stalled_agent` | TTL prevents immediate duplicate help, but assistance has no causation chain/depth bound. | WorkItem cause/depth fields and loop rejection. | A→B→A help cycles across repeated stalls. | Reciprocal/depth-exhausted assistance becomes NEEDS_HUMAN without another task. | open |
| B-017 | P1 | `progress.py:ProgressTracker` | Signature is mostly repeated text; structured condition/step/child/work progress is not the primary measure. | Structured progress snapshot with textual failure signature as one signal. | False stalls or missed progress. | Completed step/artifact/condition resets no-progress even with repeated summary. | open |
| B-018 | P1 | persistence of EventBus and active WorkItems | Event history is diagnostic-only and active work recovery has no explicit restart transition/lease. | Persist durable causation/work state and reconcile active work on boot. | Duplicate delegation or work stuck ACTIVE after crash. | Crash/restart never executes completed work twice; stale ACTIVE work is explicitly recovered. | open |
| B-019 | P2 | `Environment.request_model_inference` | Broad external direct-inference entry point uses explicit telemetry but no TaskPacket. | Keep for human/architect compatibility; require bounded packet or documented exemption. | Context metadata is weaker than normal agent calls. | Direct request remains bounded and telemetry-complete. | open |
| B-020 | P2 | Markdown `memory.md`, `context.md`, `WorldContext` | Compatibility projections can still influence prompts alongside structured state. | Make structured state authoritative; label projections and select narrowly. | Duplicate/stale facts in prompt, not state corruption. | Structured fact wins over conflicting projection text. | open |

## Phase A gate

- Model-call paths: fully enumerated; no P0/P1 direct-provider bypass.
- Goal-selection paths: fully enumerated; B-001 through B-007 require migration.
- Delegation paths: fully enumerated; B-008, B-015 and B-016 require migration.
- Improvement-selection paths: fully enumerated; B-009 requires migration.
- Context/shared-state/event/procedure bypasses: B-010 through B-014 and
  B-017 through B-020.

The first implementation batch is B-001 through B-005: one authoritative goal
state machine, evaluation and preemption policy. It is the smallest change that
removes the known P0 conflicts before further context, procedure or coordination
hardening.
