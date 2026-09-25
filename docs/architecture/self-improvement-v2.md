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

