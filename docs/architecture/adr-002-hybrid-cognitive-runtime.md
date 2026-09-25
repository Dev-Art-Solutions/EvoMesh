# ADR-002: Explicit hybrid cognitive runtime boundaries

Status: accepted

## Context

Provider calls were indistinguishable from the runtime's control loop, context
was assembled as broad prose, and ordinary coordination had no typed contract.
That made model usage difficult to explain, measure, route, or remove.

## Decision

All runtime model calls pass through `CognitiveModelService` with an explicit
service and invocation reason. Calls record bounded telemetry. `TaskPacket` and
`ContextAssembler` preserve the goal and apply a hard context budget.

Routine cognition uses deterministic services first: Goal Manager, rules,
events, progress tracking, capability matching, Contract Net, work items and
the blackboard. Semantic ACL messages coexist with human free-form chat.

Memory is logically separated: current context is working memory, beliefs are
semantic memory, episodes are structured history, and learned procedures are
procedural memory. Existing Markdown memory stays as a human-readable durable
projection during migration.

## Consequences

Model use is attributable and benchmarkable. Repeated failures stop before an
unbounded retry loop. Coordination contracts can be handled without language
interpretation. New persisted fields are additive and have defaults, so old
agent records remain readable.

