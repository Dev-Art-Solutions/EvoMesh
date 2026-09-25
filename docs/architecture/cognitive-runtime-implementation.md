# Cognitive Runtime Implementation Map

This map records the load-bearing implementation of the cognitive evolution
program (`plans/EVOMESH_COGNITIVE_EVOLUTION_PLAN.md`,
`plans/EVOMESH_ARCHITECTURE_INTENT_DETAILED.md`). Every module listed here is
called from the running mesh, not only from tests.

## Deterministic cognitive core

- `GoalManager` owns goal predicates, dependencies, deadlines, retry policy,
  utility-based selection and stale-goal detection (`/status` → `stale_goals`).
- `RuleEngine` runs every BDI cycle after belief revision. Its rules are the
  behavior's built-ins (the Guardian's degradation rule) plus the agent's own
  `AgentDefinition.rules` (from a template's `rules:` or `/rules`), validated
  where they are declared. Inputs: bus events addressed to the agent since its
  last cycle and a `belief_changed` event per revised belief. Outputs: derived
  beliefs, proposed goals, `RULE_EVENT`s on the bus and `REQUEST_ACTION`s
  handled by `BDIBehavior.on_rule_action` (`announce`, `wake`).
- BDI keeps a committed intention and re-plans only after relevant change or
  failure. `BELIEF_CHANGED` and `GOAL_CREATED` are published every cycle.
- `ProgressTracker` detects identical failures and no-progress cycles; a stall
  is signalled once, on the cycle that reaches the threshold.

## Procedural learning

`BDIReasoner` feeds every finished model-planned or learned intention, success
or failure, to `ProcedureLearner` as an `ExecutionTrace`, persisted (bounded)
in `MindState.execution_traces`. Three identical clean successes for the same
goal promote a `LearnedProcedure`; before a planning call the reasoner reuses
the procedure learned for that goal, so the call is not made. A procedure that
fails as often as half its successes is forgotten. `/procedures <agent>` shows
learned and pending patterns; `approve` promotes one early, `forget` drops one.

## Selective model use

`CognitiveModelService` is the only runtime boundary that calls a provider. Each
call carries a cognitive operation and reason plus agent, goal and task IDs,
sizes, duration and outcome. `TaskPacket`/`ContextAssembler` build the bounded
prompt. Required task, goal and output-contract sections receive space before
optional evidence. Every section has an explicit priority/budget and produces
an out-of-prompt provenance record with available/included characters and
truncation state. Memory and working notes are selected by task/goal terms with
only a small recent fallback; inbox history is included only for chat and
unstructured-input operations. The 4k/8k regression scenarios deliberately
use oversized irrelevant context and prove required sections remain intact.
`/status` exposes aggregate call telemetry.

## Memory, events and the blackboard

Working context, semantic beliefs, bounded episodes, execution traces and
learned procedures are separate. The typed `EventBus` has bounded history.
The `Blackboard` is shared, bounded and persisted (`repository` state
`blackboard`): revised beliefs become facts (`<agent>.<key>`), files a harness
job wrote become artifacts, and open work items are listed; its projection is
part of every agent's world snapshot and `world.md`.

## Multi-agent cooperation

Capabilities persist on definitions (system agents seeded; template agents from
`capabilities:` or derived from their tools). `WorkItem` transfers an explicit
objective, inputs, outputs, conditions and budget. An accepted `DELEGATE`
becomes a priority-2 `delegated_work` goal recording its requester; completing
it closes the work item, stores the result as a fact, sends `RESULT` back and
publishes `TASK_COMPLETED`; failing it sends `FAILURE` and spends budget.

A stall delegates a diagnosis to the `health.verify` capability via Contract
Net. The Guardian answers it from runtime state without a model call. A request
is not repeated while one is open, expires after an hour, and is cancelled when
the stalled goal finishes.

## Evidence-backed evolution

`ImprovementControl` is the control plane of the Evolver's plan stage:

1. **Evidence → backlog.** Logged faults, `docs/evolution/improvements.md`
   items, a red baseline, recurring non-environmental runtime events, and
   `PROPOSAL:` lines jobs report instead of widening their scope are proposals
   with a stable source ref, explicit priority factors and a verification plan.
   Triage rejects environmental failures and waits for recurrence (runtime
   events ×3, discoveries ×2, or `/improvements release`).
2. **Prioritize.** The best-scoring READY improvement whose dependencies are
   verified is worked, one at a time. A scout runs only when nothing evidenced
   is left.
3. **Delegate.** Each generation is a `WorkItem` routed by the `code.edit`
   capability and published on the blackboard; a retry reuses its budget.
4. **Review and validate.** The read-only review verdict and deterministic
   validation are recorded separately.
5. **Settle.** A work item closes from its generation's recorded outcome
   (`GenerationSupervisor.outcome`), so no ending path is missed. An exhausted
   budget becomes `NEEDS_HUMAN`, announced once.
6. **Measure.** A finished improvement is `VERIFYING` until its evidence stays
   gone for its observation window → `VERIFIED`, or `INEFFECTIVE` if it comes
   back. A verified improvement whose evidence returns is reopened.

`/improvements` shows the backlog; `depend <id> <on-id>` builds the dependency
graph (cycles refused) and `epic <id> <name>` groups improvements into epics.

## Verification commands

Use an isolated temp directory on Windows:

```powershell
$env:PYTEST_ADDOPTS='--basetemp=<scratch dir> -p no:cacheprovider'
.\.runtime\baseline-venv\Scripts\python.exe -m pytest -q
.\.runtime\baseline-venv\Scripts\python.exe -m ruff check .
.\.runtime\baseline-venv\Scripts\python.exe -m pyright src
```
