# Self-Improvement V2

EvoMesh self-improvement is an evidence-backed workflow, not an agent waking up
and inventing cleanup. The existing generation pipeline remains the execution
mechanism during migration; the backlog becomes the control plane around it.

## Lifecycle

`ImprovementScout` observes runtime events and metrics. It may propose an
`Improvement`, but it cannot edit source. Every proposal contains concrete
evidence and measurable success criteria. `ImprovementTriage` rejects or blocks
unsupported items, deduplicates them through a stable fingerprint, and exposes
the complete priority calculation.

`ImprovementCoordinator` selects the highest-scoring eligible item, enforces
improvement and work-item WIP limits, and decomposes it into bounded
`WorkItem`s. Capability routing and Contract Net choose executors. The
coordinator does not normally write code.

Completion requires two different judgments:

1. review against the stated success criteria;
2. deterministic validation of executable gates.

Promotion moves an item to `VERIFYING`, not directly to `VERIFIED`. Runtime
observations are compared with the stored baseline and target. Failure to move
the metric results in `INEFFECTIVE`; exhausted budgets result in `BLOCKED` or
`NEEDS_HUMAN`.

## Persistence and compatibility

The structured backlog serializes to ordinary JSON-compatible repository
state. The existing Markdown improvement list remains a human-controlled input
during migration; it is not silently rewritten. Existing candidate worktrees,
validation, repair, review, approval and rollback behavior remain in force.

## Separation of responsibilities

- Scout: evidence collection and proposals only.
- Triage: support, duplication, dependency and transparent score decisions.
- Coordinator: scheduling, budgets, assignment and lifecycle.
- Architect/Researcher: read-only design and evidence artifacts.
- Coder: bounded source change for one work item.
- Reviewer: semantic objective verdict, read-only.
- Validator: deterministic gates.

Unrelated discoveries become new proposals. They do not expand the active work
item.


## Closure v2: idle, executed, verified

The architecture closure (plans/EVOMESH_ARCHITECTURE_CLOSURE_PLAN_V2.md, §18)
tightened four places. The tests named here are the evidence.

**An empty backlog is idle.** With no eligible, evidenced improvement (a
failing test, a logged fault, an open item in `docs/evolution/improvements.md`,
a human's objective), the Evolver reports that nothing substantive is left.
It opens no candidate, adopts no fallback and makes no model call. The old
exploring behaviour, a read-only scout generation followed by the dead-module
"wire or delete" backlog, is `evolution.scout_when_idle` and off by default.
A speculative proposal stays `TRIAGED` (`tests/test_idle_evolution.py`).

**Execution goes through a seam.** `ImprovementControl.begin` routes the work,
then `WorkExecutor.submit` starts it under the awarded agent's identity, with
the isolated workspace and the executor's own reference. The returned handle
is stored on the work item, so a restart recovers it. `settle` inspects each
handle through its own executor, and `PENDING` and `UNKNOWN` never settle as
success. `cancel` stops work through the executor and leaves the improvement
`BLOCKED`, not done. `GenerationExecutor` is one adapter; tests substitute
another (`tests/test_work_executor.py`).

**Verification needs real observations.** An `Observation` has an id, an
observer, the evidence kinds it covers, the number of eligible requests it
processed, and its health. The same reading counts once. An unhealthy
observer, a reading with no eligible requests, a missing plan, or an empty
`sync()` records an `inconclusive_reason` and never verifies. The evolver's
observers are the baseline suite (one reading per tree state) and the runtime
log (counted only when the mesh ran since the last reading). Work no observer
can measure, such as a human's backlog item, waits in `VERIFYING` until
`/improvements verify <id> <reason>`.

**Verdicts are bound to a revision.** Review and validation record the
candidate revision they judged: a digest of its code diff, `docs/evolution`
excluded. A new validation of a different revision clears the old review. A
cancelled required work item is not success unless it was waived with a
recorded reason. A candidate that touches the protected surface
(`codebase.PROTECTED_PATHS`: admission, verification and promotion rules, the
closure tests, approvals, evidence, live config) fails validation and needs a
human's review.

W3 (`tests/test_w3_improvement.py`) runs the whole loop on a real off-by-one
defect in a fixture package. The real acceptance check fails on the live tree.
A routed candidate fixes it, the check passes on the candidate, and a separate
review agrees on the same revision. After promotion, one post-change suite
observation verifies it, and the next cycle idles with no model call. With the
observer disabled the fix stays `VERIFYING`, and so does a candidate that only
weakens its own test.
