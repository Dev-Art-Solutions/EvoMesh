# ADR-001: Keep structured goal state in MindState and lifecycle policy in GoalManager

## Status

Accepted for Cognitive Runtime Milestone 1.

## Context

EvoMesh already persists each complete `AgentDefinition`, including `MindState`, as JSON in
SQLite. Goal selection and transitions were distributed across `MindState`, `BDIReasoner` and
`AgentRuntime._apply`. The cognitive architecture requires richer goal data, deterministic
dependency scheduling and explicit completion predicates without a destructive migration or a
second competing source of runtime truth.

Constraints:

- existing serialized agent definitions must continue to load;
- current BDI commitment and cadence behavior must remain intact;
- Milestone 1 must not add a new dependency or require a database rewrite;
- future goals and work items will eventually need repository interfaces and independent rows;
- routine lifecycle decisions must not invoke a model.

## Options considered

| Option | Benefits | Costs | Complexity | Valid when |
| --- | --- | --- | --- | --- |
| Extend `Goal` inside `MindState`; use a stateless `GoalManager` policy service | Compatible JSON defaults, one source of truth, independently testable policy, incremental migration | Whole agent JSON remains the persistence unit; service is recreated at call sites | Low | Current milestone and existing storage model |
| Add normalized goal tables immediately | Strong querying and independent writes | Schema migration, synchronization with embedded goals, larger blast radius | High | Cross-agent goal queries and write contention prove necessary |
| Put all lifecycle methods directly on `Goal`/`MindState` | Few files and simple calls | Entities become coupled to filesystem evidence, scheduling policy and runtime services | Medium | Only trivial local transitions are ever required |
| Create a parallel cognitive-state store | Can model the target freely | Two sources of truth, migration ambiguity, silent drift | Very high | Not acceptable under incremental compatibility constraints |

## Decision

Structured state remains in the existing `Goal` objects embedded in `MindState`.
`GoalManager` is a stateless deterministic service over one `MindState`; it owns dependency
validation, runnable transitions, scoring, predicates, deadlines, retries and completion.
`MindState.open_goals` delegates runnable selection to this service so existing callers use the
new policy without a parallel API migration.

Explicit success/failure predicates are opt-in. Description-only legacy goals retain the old
completion behavior until migrated. New fields all have backward-compatible defaults.

## Rationale

1. It moves lifecycle intelligence out of prompts while preserving EvoMesh's proven persistence
   boundary and BDI loop.
2. It is the smallest change that makes graph and predicate behavior load-bearing rather than a
   dead module.
3. It leaves a clean later seam: a goal repository can replace embedded persistence when actual
   WorkItem/cross-agent query requirements justify the migration.
4. A plain service and explicit data models are easier to inspect and test than decorators,
   hidden hooks or a second framework.

## Trade-offs

- Goal queries still require loading an agent definition.
- Policy construction is cheap but repeated at call sites.
- Some legacy transition code remains in `AgentRuntime._apply` during incremental migration.
- The first condition vocabulary is intentionally small and does not provide an expression
  language.

These costs are accepted because normalized persistence, a rule DSL and wholesale transition
rewrites can be added later; removing premature parallel state would be much harder.

## Consequences

- **Positive:** existing rows load unchanged; deterministic selection and proof-based completion
  are immediately available; no model or third-party dependency is added.
- **Negative:** lifecycle ownership is temporarily shared with compatibility behavior in
  `AgentRuntime._apply`.
- **Mitigation:** focused regression tests cover legacy BDI behavior, while new tests cover graph,
  deadline, retry and predicate policy. Later milestones should move remaining transitions behind
  `GoalManager` before introducing a normalized repository.

## Revisit triggers

Reconsider embedded goal persistence when any of the following becomes true:

- WorkItems require transactional cross-agent dependency updates;
- goal graphs need queries without loading whole agent definitions;
- concurrent goal writers cause lost updates;
- schema migration for the improvement backlog establishes a reusable repository pattern;
- measured state size makes full-definition writes materially expensive.
