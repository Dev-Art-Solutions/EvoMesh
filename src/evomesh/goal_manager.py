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


# A stalled goal pauses this long (doubling per stall, capped) before it
# runs again, so help can arrive and the same inference is not repeated.
STALL_COOLDOWN_SECONDS = 300.0
STALL_COOLDOWN_MAX_SECONDS = 3600.0
# A one-shot goal that stalls this many times has failed.
STALL_LIMIT = 3


@dataclass(frozen=True)
class GoalTransition:
    goal: Goal
    before: GoalStatus
    after: GoalStatus


class GoalGraphError(ValueError):
    pass


class IllegalGoalTransition(ValueError):
    pass


@dataclass(frozen=True)
class PreemptionPolicy:
    enabled: bool = True
    minimum_score_delta: float = 50.0
    non_preemptible_goal_kinds: frozenset[str] = frozenset()
    deadline_override_seconds: float = 300.0
    override_parameter: str = "preempt_override"


LEGAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.PENDING: frozenset(
        {GoalStatus.RUNNABLE, GoalStatus.BLOCKED, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.RUNNABLE: frozenset(
        {
            GoalStatus.ACTIVE,
            GoalStatus.BLOCKED,
            GoalStatus.STALLED,
            GoalStatus.DONE,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.ACTIVE: frozenset(
        {
            GoalStatus.RUNNABLE,
            GoalStatus.BLOCKED,
            GoalStatus.STALLED,
            GoalStatus.DONE,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.BLOCKED: frozenset(
        {GoalStatus.RUNNABLE, GoalStatus.STALLED, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.STALLED: frozenset(
        {GoalStatus.RUNNABLE, GoalStatus.BLOCKED, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.DONE: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}


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

    def transition(
        self,
        goal: Goal,
        status: GoalStatus,
        *,
        reason: str | None = None,
        at: datetime | None = None,
        human_override: bool = False,
    ) -> bool:
        """Apply one legal lifecycle transition and keep its metadata coherent."""
        if goal.status is status:
            if status in {GoalStatus.BLOCKED, GoalStatus.STALLED} and reason is not None:
                goal.blocked_reason = reason
                goal.updated_at = at or now_utc()
            return False
        if not human_override and status not in LEGAL_TRANSITIONS.get(
            goal.status, frozenset()
        ):
            raise IllegalGoalTransition(
                f"illegal goal transition {goal.status.value} -> {status.value} for {goal.id}"
            )
        moment = at or now_utc()
        goal.status = status
        goal.updated_at = moment
        goal.blocked_reason = reason if status in {GoalStatus.BLOCKED, GoalStatus.STALLED} else None
        if status is GoalStatus.DONE:
            goal.progress = 1.0
            goal.completed_at = moment
        return True

    def score(self, goal: Goal, *, at: datetime | None = None) -> float:
        return self.scoring.score(goal, at=at or now_utc())

    def should_preempt(
        self,
        current: Goal,
        candidate: Goal,
        *,
        policy: PreemptionPolicy | None = None,
        at: datetime | None = None,
    ) -> bool:
        policy = policy or PreemptionPolicy()
        at = at or now_utc()
        if current.id == candidate.id or not policy.enabled:
            return False
        if current.status in TERMINAL_GOAL_STATUSES | {
            GoalStatus.BLOCKED,
            GoalStatus.STALLED,
        }:
            return True
        explicit_override = bool(candidate.parameters.get(policy.override_parameter))
        emergency = candidate.kind in {"human_override", "emergency"}
        deadline_override = (
            candidate.deadline is not None
            and (candidate.deadline - at).total_seconds() <= policy.deadline_override_seconds
        )
        if explicit_override or emergency or deadline_override:
            return True
        if current.kind in policy.non_preemptible_goal_kinds:
            return False
        return self.score(candidate, at=at) - self.score(current, at=at) >= max(
            0.0, policy.minimum_score_delta
        )

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

    def refresh(self, *, at: datetime | None = None) -> list[GoalTransition]:
        """Apply time, dependency and budget policy to every goal, and return
        the transitions it made -- the one record callers dispatch events
        from, instead of inferring them around the call (B-007)."""
        at = at or now_utc()
        before = {goal.id: goal.status for goal in self.mind.goals}
        self._refresh(at)
        return [
            GoalTransition(goal, before[goal.id], goal.status)
            for goal in self.mind.goals
            if goal.id in before and before[goal.id] is not goal.status
        ]

    def _refresh(self, at: datetime) -> None:
        for goal in self.mind.goals:
            if goal.status in TERMINAL_GOAL_STATUSES:
                continue
            if goal.deadline is not None and at >= goal.deadline and not goal.recurring:
                self.transition(goal, GoalStatus.FAILED, at=at)
                goal.blocked_reason = "deadline"
                goal.last_error = "deadline passed before the goal completed"
                goal.updated_at = at
                continue
            if goal.status is GoalStatus.STALLED:
                # A stall is a pause for help or a changed world, not an end:
                # found in review, a stalled goal was never selected again, so
                # an agent's standing goal would have died on its first stall.
                if goal.next_attempt_at is None or at >= goal.next_attempt_at:
                    self.transition(goal, GoalStatus.RUNNABLE, at=at)
                continue
            due = goal.next_attempt_at is None or at >= goal.next_attempt_at
            attempts_left = goal.recurring or goal.attempts < goal.attempt_limit
            dependencies: list[Goal] = []
            missing_dependency = False
            for dependency_id in goal.dependency_goal_ids:
                try:
                    dependencies.append(self.mind.goal(dependency_id))
                except KeyError:
                    missing_dependency = True
            failed_dependency = next(
                (
                    dependency
                    for dependency in dependencies
                    if dependency.status in {GoalStatus.FAILED, GoalStatus.CANCELLED}
                ),
                None,
            )
            if failed_dependency is not None:
                self.transition(goal, GoalStatus.FAILED, at=at)
                goal.last_error = f"dependency {failed_dependency.id} did not complete"
                goal.blocked_reason = "dependency_failed"
                continue
            if missing_dependency:
                self.transition(
                    goal, GoalStatus.BLOCKED, reason="missing_dependency", at=at
                )
                continue
            dependencies_complete = all(
                dependency.status is GoalStatus.DONE for dependency in dependencies
            )
            if not dependencies_complete or not due or not attempts_left:
                # A goal exhausted by failures is failed, while dependency and
                # cadence waits are blocked and can later become runnable.
                status = (
                    GoalStatus.FAILED
                    if not attempts_left and not goal.recurring
                    else GoalStatus.BLOCKED
                )
                reason = (
                    "attempts"
                    if not attempts_left
                    else "dependencies"
                    if not dependencies_complete
                    else "schedule"
                )
                self.transition(goal, status, reason=reason, at=at)
            elif goal.status is GoalStatus.BLOCKED and goal.blocked_reason not in {
                "dependencies",
                "schedule",
                "retry",
            }:
                continue
            elif goal.status is not GoalStatus.ACTIVE:
                self.transition(goal, GoalStatus.RUNNABLE, at=at)

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
            self.transition(goal, GoalStatus.FAILED)
            return goal.status
        if goal.success_conditions and all(
            self.condition_met(condition, context) for condition in goal.success_conditions
        ):
            self.complete(goal)
            return goal.status
        return None

    def complete(
        self,
        goal: Goal,
        *,
        at: datetime | None = None,
        human_override: bool = False,
    ) -> None:
        at = at or now_utc()
        if goal.status is GoalStatus.PENDING:
            self.refresh(at=at)
        if goal.recurring:
            if goal.cron:
                goal.next_attempt_at = cron.next_after(goal.cron, at)
            elif goal.interval_seconds:
                goal.next_attempt_at = at + timedelta(seconds=goal.interval_seconds)
            self.transition(
                goal,
                GoalStatus.BLOCKED if goal.next_attempt_at else GoalStatus.RUNNABLE,
                reason="schedule" if goal.next_attempt_at else None,
                at=at,
            )
        else:
            self.transition(
                goal, GoalStatus.DONE, at=at, human_override=human_override
            )
        self.refresh(at=at)

    def record_failure(self, goal: Goal, reason: str, *, at: datetime | None = None) -> None:
        at = at or now_utc()
        was_blocked = goal.status is GoalStatus.BLOCKED
        goal.attempts += 1
        goal.last_error = reason
        goal.updated_at = at
        if not goal.recurring and goal.attempts >= goal.attempt_limit:
            self.transition(goal, GoalStatus.FAILED, at=at)
            return
        delay = goal.retry_policy.backoff_seconds * (
            goal.retry_policy.backoff_multiplier ** max(0, goal.attempts - 1)
        )
        if delay > 0:
            goal.next_attempt_at = at + timedelta(seconds=delay)
            self.transition(goal, GoalStatus.BLOCKED, reason="retry", at=at)
        elif was_blocked:
            # A behavior can declare a step impossible and deliberately block
            # the goal. Recording its failure budget must not immediately
            # undo that semantic transition and make it runnable again.
            if goal.status is not GoalStatus.BLOCKED:
                self.transition(goal, GoalStatus.BLOCKED, reason=goal.blocked_reason, at=at)
        else:
            self.transition(goal, GoalStatus.RUNNABLE, at=at)

    def activate(self, goal: Goal, *, at: datetime | None = None) -> None:
        moment = at or now_utc()
        if goal.status is GoalStatus.PENDING:
            self.transition(goal, GoalStatus.RUNNABLE, at=moment)
        self.transition(goal, GoalStatus.ACTIVE, at=moment)

    def block(self, goal: Goal, reason: str, *, at: datetime | None = None) -> None:
        self.transition(goal, GoalStatus.BLOCKED, reason=reason, at=at)

    def mark_stalled(self, goal: Goal, reason: str, *, at: datetime | None = None) -> None:
        """Pause a stalled goal, then let it run again; a one-shot goal that
        stalls ``STALL_LIMIT`` times fails instead of repeating the same work."""
        at = at or now_utc()
        goal.stalls += 1
        goal.last_error = reason
        if not goal.recurring and goal.stalls >= STALL_LIMIT:
            self.transition(goal, GoalStatus.FAILED, at=at)
            goal.blocked_reason = "stalled"
            return
        cooldown = min(
            STALL_COOLDOWN_MAX_SECONDS, STALL_COOLDOWN_SECONDS * 2 ** (goal.stalls - 1)
        )
        goal.next_attempt_at = at + timedelta(seconds=cooldown)
        self.transition(goal, GoalStatus.STALLED, reason=reason, at=at)

    def cancel(self, goal: Goal, reason: str = "cancelled", *, at: datetime | None = None) -> None:
        goal.recurring = False
        self.transition(goal, GoalStatus.CANCELLED, reason=reason, at=at)

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
