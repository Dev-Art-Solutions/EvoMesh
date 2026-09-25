# Architecture closure v2 — entry gap analysis (AC-00)

Plan: `plans/EVOMESH_ARCHITECTURE_CLOSURE_PLAN_V2.md`.
Base commit: `815cd6c3422512358987c4fd92b9fffc333327f6` (main). Implemented
directly on `main` of this repository, with the live mesh stopped (by the
owner's instruction) so the Evolver does not commit into the tree meanwhile.
Python 3.13.15, `uv.lock` unchanged, quality commands: pytest, ruff, pyright
(`docs/architecture/closure-evidence/quality-gate-logs/baseline-*.txt`:
907 passed, 8 skipped — the docs-venv document tools, not provisioned here —
ruff clean, pyright 0 errors).

## Current symbols the plan's contracts map onto

| Plan concept | Existing code | Status |
|---|---|---|
| Goal order / terminal state / recurrence | `goal_manager.GoalManager` (transition, refresh, complete, record_failure, preemption) | already verified (Phase 2 tests) |
| Intention commit / pause / resume | `bdi.BDIReasoner`, `contracts.Intention` | partial: no execution reference on an intention |
| Model calls and accounting | `cognitive_services.CognitiveModelService` (reason, sizes, server tokens) | already verified; per-root budgets missing |
| Tool authorization | `harness_tools._resolve` + `_permit` with `ToolContext(policy, agent_id)`; `permissions.FilesystemPolicy.require` rechecks grants per call | reusable as the adapter boundary |
| Peer assignment / work state | `coordination.WorkItem`, `ContractNet`, `Environment._make_delegate_work`, delegated goals | partial: work items persisted as one blackboard blob (whole-blob rewrite), eligibility ignores offline agents |
| Shared facts | `blackboard.Blackboard` | reusable through its API |
| Improvement selection | `improvements.ImprovementControl` | partial: fallbacks (scout, maintenance) are adopted as work; verification counts `sync()` calls as observations |
| Executor seam | `improvements.WorkExecutor.outcome`, `evolution.GenerationExecutor` | partial: outcome only, no submit/inspect/cancel |
| Learned procedures | `procedural_learning.ProcedureLearner` (textual steps) | legacy: keep as compatibility path, never typed authority |

## Requirement map

| Requirement | Status | Note |
|---|---|---|
| Typed ProcedureDefinition, immutable revision + digest | missing | new `procedures.py` |
| Seven step kinds, one binding/predicate grammar | missing | |
| Static validator (graph, dataflow, adapters, capabilities) | missing | |
| Admission registry separate from content | missing | new tables |
| Execution record + operation journal, CAS claims | missing | `storage.py` exposes only per-object upserts; add narrow atomic operation |
| Direct action adapters preserving policy | missing | adapters over `_resolve`/`_permit` |
| BDI selection of typed procedures (incl. scheduled goals) | missing | `deliberate` order change |
| Evidence-backed completion into GoalEvaluationContext | partial | context type exists; nothing fills tool/validator results |
| Explicit CognitiveStep, no tools, bounded, counted | missing | via `CognitiveModelService` |
| Branch / validate / delegate / await / complete | missing | |
| Root budgets with reservation | missing | `WorkBudget.max_model_calls` exists but is not enforced |
| Crash windows, reconciliation, unknown effects | missing | |
| Cancellation / pause of executions | missing | |
| Real trace capture with provenance, candidate extraction | missing | legacy learner is textual |
| Manual promotion, degradation, revision pinning | missing | |
| Empty backlog is idle | missing | B-009 adopted scout/maintenance fallbacks as work (reverted here) |
| Executor submit/inspect/cancel seam | partial | |
| Observer-based verification with unique observations | missing | current verification counts sync calls |
| Operator controls for typed procedures | missing | extend console |
| Benchmarks B1-B12, acceptance manifest | missing | extend `benchmarks/` |

## Decisions recorded before implementation

- One new contract module (`procedures.py`) and one runtime module
  (`procedure_runtime.py`); storage gets four narrow tables and one atomic
  transition method. No new scheduler: the BDI cycle calls `advance` once.
- The normative wire format is JSON-compatible data validated by pydantic
  models with `extra="forbid"`; YAML examples in the plan are translated to the
  same data in fixtures.
- Schema subset: `type`, `properties`, `required`, `additionalProperties:false`,
  `items`, `maxItems`, `enum`, `minimum`, `maximum`, `maxLength`. Anything else
  is refused at admission.
- Learned-candidate provenance comes from an approved binding map per goal kind
  (plan §16.3 step 3); equal values are only a consistency check, never proof.
- Improvement verification needs a named observer with health and unique
  observation IDs; the baseline suite run on the promoted tree is the
  observer for test/backlog evidence; log absence of a fault stays
  inconclusive.
