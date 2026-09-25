# Cognitive Runtime Implementation Map

This map records the load-bearing implementation after the cognitive evolution
program. It complements the baseline and gap report; it is not a replacement
for the detailed architecture intent.

## Deterministic cognitive core

- `GoalManager` owns goal predicates, dependencies, deadlines, retry policy and
  utility-based selection.
- `RuleEngine` performs bounded forward chaining and can assert beliefs, create
  goals, emit events or request actions.
- BDI keeps a committed intention and re-plans only after relevant change or
  failure. Plan selection and outcomes are counted in persisted state.
- `ProgressTracker` detects identical failures and no-progress cycles before an
  unbounded retry loop.

## Selective model use

`CognitiveModelService` is the only runtime boundary that calls a provider. Each
call carries a cognitive operation and reason plus agent, goal and task IDs,
sizes, duration and outcome. `TaskPacket` applies a hard budget while preserving
the objective. The environment status exposes aggregate call telemetry.

## Memory and events

Working context, semantic beliefs, bounded structured episodes and learned
procedures are separate. Markdown memory/context files remain readable
projections for compatibility. The typed `EventBus` has bounded diagnostic
history; goal completion unblocks dependants and wakes only affected agents.

## Multi-agent cooperation

Agent capabilities persist in definitions. `WorkItem` transfers an explicit
objective, inputs, outputs, conditions and budget. ACL performatives are parsed
by the runtime without a model call. Contract Net ranks capable agents by match,
load and history and supports exclusion during reassignment. The blackboard
holds provenance-bearing facts, artifacts, events and work status.

Stalls emit structured events. The runtime delegates an assistance work item to
another capable agent when available; otherwise it broadcasts a structured help
request. Delegation never changes the receiver's permissions or capabilities.

## Evidence-backed evolution

The V2 backlog persists evidence, explicit score factors, success criteria,
dependencies, work items and verification observations. Scout, Triage and
Coordinator are separate services. WIP and attempt budgets terminate failed
work as `NEEDS_HUMAN`. Review against the objective and deterministic validation
must both pass before `VERIFYING`; measurements then decide `VERIFIED` or
`INEFFECTIVE`.

Built-in agents expose explicit role capabilities. Existing isolated candidate
workspaces, approvals, validators, rollback and permissions remain authoritative
during the migration from the legacy Evolver pipeline.

## Procedural learning

Successful goal executions produce structured traces. A procedure is promoted
automatically only after three matching successes and no matching failure, or
earlier with explicit human approval. The resulting procedure is stored in the
agent's procedural memory; future plan-library adapters can select it without a
planning call. Plan reuse statistics are already persisted by `MindState`.

## Verification commands

Use an isolated temp directory on Windows:

```powershell
$env:PYTEST_ADDOPTS='--basetemp=.runtime/final-tests -o cache_dir=.runtime/final-cache'
.\.runtime\baseline-venv\Scripts\python.exe -m pytest -q
.\.runtime\baseline-venv\Scripts\python.exe -m ruff check .
.\.runtime\baseline-venv\Scripts\python.exe -m pyright src
```
