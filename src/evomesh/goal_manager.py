"""Structured goal lifecycle, dependencies, predicates and deterministic choice."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from evomesh import cron
from evomesh.contracts import (
    Goal,
    GoalCondition,
    GoalConditionKind,
    GoalStatus,
    MindState,
    now_utc,
)

TERMINAL_GOAL_STATUSES = frozenset(
    {GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.CANCELLED}
)


@dataclass(frozen=True)
class GoalEvaluationContext:
    """Structured evidence available to success/failure predicates."""

    artifact_root: Path | None = None
    tool_results: Mapping[str, Any] = field(default_factory=dict)
    validator_results: Mapping[str, bool] = field(default_factory=dict)
    human_approvals: frozenset[str] = frozenset()


class GoalScoringPolicy(Protocol):
    def score(self, goal: Goal, *, at: datetime) -> float: ...


@dataclass(frozen=True)
class UtilityGoalScoringPolicy:
    """Transparent utility policy; higher scores run first.

    Priority remains dominant for compatibility (1 is more important than 5).
    The other terms make the target policy explicit without letting a model rank
    ordinary work. Weights are configuration points, not hidden prompt wording.
    """

    priority_weight: float = 100.0
    urgency_weight: float = 20.0
    expected_value_weight: float = 5.0
    strategic_value_weight: float = 5.0
    effort_weight: float = 1.0
    risk_weight: float = 2.0
    age_weight: float = 0.01

    def score(self, goal: Goal, *, at: datetime) -> float:
        priority = (10 - goal.priority) * self.priority_weight
        urgency = 0.0
        if goal.deadline is not None:
            remaining = (goal.deadline - at).total_seconds()
            urgency = self.urgency_weight * (2.0 if remaining <= 0 else 1.0 / max(1.0, remaining))
        age_seconds = max(0.0, (at - goal.created_at).total_seconds())
        utility = goal.utility
        return (
            priority
            + urgency
            + utility.expected_value * self.expected_value_weight
            + utility.strategic_value * self.strategic_value_weight
            - utility.estimated_effort * self.effort_weight
            - utility.risk * self.risk_weight
            + (age_seconds / 86_400.0) * self.age_weight
        )


class GoalGraphError(ValueError):
    pass


class GoalManager:
    """Own lifecycle policy for the goals in one ``MindState``.

    The state itself remains embedded in ``AgentDefinition`` for migration
    compatibility. This service is deliberately stateless: recreating it for a
    cycle does not create a second source of truth beside persisted MindState.
    """

    def __init__(
        self, mind: MindState, scoring: GoalScoringPolicy | None = None
    ) -> None:
        self.mind = mind
        self.scoring = scoring or UtilityGoalScoringPolicy()

    def create(self, description: str, **fields: Any) -> Goal:
        goal = self.mind.add_goal(description, **fields)
        try:
            self.validate_graph()
        except GoalGraphError:
            self.mind.goals = [item for item in self.mind.goals if item.id != goal.id]
            if goal.parent_goal_id:
                parent = self.mind.goal(goal.parent_goal_id)
                parent.child_goal_ids = [item for item in parent.child_goal_ids if item != goal.id]
            raise
        self.refresh()
        return goal

    def validate_graph(self) -> None:
        goals = {goal.id: goal for goal in self.mind.goals}
        for goal in goals.values():
            for related in [*goal.dependency_goal_ids, *goal.child_goal_ids]:
                if related not in goals:
                    raise GoalGraphError(f"goal {goal.id!r} references missing goal {related!r}")
            if goal.parent_goal_id is not None and goal.parent_goal_id not in goals:
                raise GoalGraphError(
                    f"goal {goal.id!r} references missing parent {goal.parent_goal_id!r}"
                )
            if goal.id in goal.dependency_goal_ids:
                raise GoalGraphError(f"goal {goal.id!r} depends on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(goal_id: str) -> None:
            if goal_id in visiting:
                raise GoalGraphError(f"goal dependency cycle includes {goal_id!r}")
            if goal_id in visited:
                return
            visiting.add(goal_id)
            for dependency_id in goals[goal_id].dependency_goal_ids:
                visit(dependency_id)
            visiting.remove(goal_id)
            visited.add(goal_id)

        for goal_id in goals:
            visit(goal_id)

    def dependencies_complete(self, goal: Goal) -> bool:
        try:
            return all(
                self.mind.goal(dependency_id).status is GoalStatus.DONE
                for dependency_id in goal.dependency_goal_ids
            )
        except KeyError:
            return False

    def refresh(self, *, at: datetime | None = None) -> None:
        at = at or now_utc()
        for goal in self.mind.goals:
            if goal.status in TERMINAL_GOAL_STATUSES:
                continue
            if goal.deadline is not None and at >= goal.deadline and not goal.recurring:
                goal.status = GoalStatus.FAILED
                goal.blocked_reason = "deadline"
                goal.last_error = "deadline passed before the goal completed"
                goal.updated_at = at
                continue
            due = goal.next_attempt_at is None or at >= goal.next_attempt_at
            attempts_left = goal.recurring or goal.attempts < goal.attempt_limit
            dependencies_complete = self.dependencies_complete(goal)
            if not dependencies_complete or not due or not attempts_left:
                # A goal exhausted by failures is failed, while dependency and
                # cadence waits are blocked and can later become runnable.
                goal.status = (
                    GoalStatus.FAILED
                    if not attempts_left and not goal.recurring
                    else GoalStatus.BLOCKED
                )
                goal.blocked_reason = (
                    "attempts"
                    if not attempts_left
                    else "dependencies"
                    if not dependencies_complete
                    else "schedule"
                )
            elif goal.status is GoalStatus.BLOCKED and goal.blocked_reason not in {
                "dependencies",
                "schedule",
                "retry",
            }:
                continue
            elif goal.status is not GoalStatus.ACTIVE:
                goal.status = GoalStatus.RUNNABLE
                goal.blocked_reason = None

    def runnable_goals(self, *, at: datetime | None = None) -> list[Goal]:
        at = at or now_utc()
        self.refresh(at=at)
        runnable = [
            goal
            for goal in self.mind.goals
            if goal.status in {GoalStatus.RUNNABLE, GoalStatus.ACTIVE} and goal.is_open
        ]
        return sorted(
            runnable,
            key=lambda goal: (-self.scoring.score(goal, at=at), goal.created_at, goal.id),
        )

    def next_goal(self, *, at: datetime | None = None) -> Goal | None:
        return next(iter(self.runnable_goals(at=at)), None)

    def condition_met(
        self, condition: GoalCondition, context: GoalEvaluationContext
    ) -> bool:
        if condition.kind is GoalConditionKind.BELIEF_EQUALS:
            belief = self.mind.belief(condition.key)
            return belief is not None and belief.statement == str(condition.value)
        if condition.kind is GoalConditionKind.ARTIFACT_EXISTS:
            path = Path(condition.path)
            if not path.is_absolute():
                if context.artifact_root is None:
                    return False
                path = context.artifact_root / path
            return path.exists()
        if condition.kind is GoalConditionKind.TOOL_RESULT:
            return context.tool_results.get(condition.key) == condition.value
        if condition.kind is GoalConditionKind.CHILD_GOALS_COMPLETE:
            goal = self.mind.goal(condition.key)
            return bool(goal.child_goal_ids) and all(
                self.mind.goal(child_id).status is GoalStatus.DONE
                for child_id in goal.child_goal_ids
            )
        if condition.kind is GoalConditionKind.VALIDATOR_PASSES:
            return context.validator_results.get(condition.key) == bool(condition.value)
        if condition.kind is GoalConditionKind.HUMAN_APPROVAL:
            return condition.key in context.human_approvals
        return False

    def evaluate(
        self, goal: Goal, context: GoalEvaluationContext | None = None
    ) -> GoalStatus | None:
        context = context or GoalEvaluationContext()
        if goal.failure_conditions and any(
            self.condition_met(condition, context) for condition in goal.failure_conditions
        ):
            goal.status = GoalStatus.FAILED
            goal.updated_at = now_utc()
            return goal.status
        if goal.success_conditions and all(
            self.condition_met(condition, context) for condition in goal.success_conditions
        ):
            self.complete(goal)
            return goal.status
        return None

    def complete(self, goal: Goal, *, at: datetime | None = None) -> None:
        at = at or now_utc()
        goal.progress = 1.0
        goal.completed_at = at
        goal.updated_at = at
        if goal.recurring:
            if goal.cron:
                goal.next_attempt_at = cron.next_after(goal.cron, at)
            elif goal.interval_seconds:
                goal.next_attempt_at = at + timedelta(seconds=goal.interval_seconds)
            goal.status = GoalStatus.BLOCKED if goal.next_attempt_at else GoalStatus.RUNNABLE
        else:
            goal.status = GoalStatus.DONE
        self.refresh(at=at)

    def record_failure(self, goal: Goal, reason: str, *, at: datetime | None = None) -> None:
        at = at or now_utc()
        was_blocked = goal.status is GoalStatus.BLOCKED
        goal.attempts += 1
        goal.last_error = reason
        goal.updated_at = at
        if not goal.recurring and goal.attempts >= goal.attempt_limit:
            goal.status = GoalStatus.FAILED
            return
        delay = goal.retry_policy.backoff_seconds * (
            goal.retry_policy.backoff_multiplier ** max(0, goal.attempts - 1)
        )
        if delay > 0:
            goal.next_attempt_at = at + timedelta(seconds=delay)
            goal.status = GoalStatus.BLOCKED
            goal.blocked_reason = "retry"
        elif was_blocked:
            # A behavior can declare a step impossible and deliberately block
            # the goal. Recording its failure budget must not immediately
            # undo that semantic transition and make it runnable again.
            goal.status = GoalStatus.BLOCKED
        else:
            goal.status = GoalStatus.RUNNABLE

    def stalled_goals(
        self, older_than: timedelta, *, at: datetime | None = None
    ) -> list[Goal]:
        at = at or datetime.now(UTC)
        return [
            goal
            for goal in self.mind.goals
            if goal.status in {GoalStatus.ACTIVE, GoalStatus.RUNNABLE}
            and at - goal.updated_at >= older_than
        ]
