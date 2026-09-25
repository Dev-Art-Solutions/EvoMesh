from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from evomesh.contracts import (
    Belief,
    Goal,
    GoalCondition,
    GoalConditionKind,
    GoalRetryPolicy,
    GoalStatus,
    GoalUtility,
    MindState,
    now_utc,
)
from evomesh.goal_manager import (
    GoalEvaluationContext,
    GoalGraphError,
    GoalManager,
)


def test_legacy_description_only_goal_still_loads() -> None:
    goal = Goal.model_validate({"description": "keep compatibility"})

    assert goal.kind == "goal"
    assert goal.parameters == {}
    assert goal.success_conditions == []
    assert goal.status is GoalStatus.PENDING


def test_dependencies_block_then_unblock_automatically() -> None:
    mind = MindState()
    first = mind.add_goal("prepare input", priority=5)
    second = mind.add_goal("consume input", priority=1, dependency_goal_ids=[first.id])
    manager = GoalManager(mind)

    assert manager.next_goal() is first
    assert second.status is GoalStatus.BLOCKED

    first.status = GoalStatus.DONE
    assert manager.next_goal() is second
    assert second.status is GoalStatus.RUNNABLE


def test_dependency_cycles_are_rejected() -> None:
    mind = MindState()
    first = mind.add_goal("first")
    second = mind.add_goal("second", dependency_goal_ids=[first.id])
    first.dependency_goal_ids = [second.id]

    with pytest.raises(GoalGraphError, match="cycle"):
        GoalManager(mind).validate_graph()


def test_explicit_belief_predicate_completes_without_a_model() -> None:
    mind = MindState()
    goal = mind.add_goal(
        "observe readiness",
        success_conditions=[
            GoalCondition(
                kind=GoalConditionKind.BELIEF_EQUALS,
                key="provider.ready",
                value="yes",
            )
        ],
    )
    mind.revise([Belief(key="provider.ready", statement="yes")])

    verdict = GoalManager(mind).evaluate(goal)

    assert verdict is GoalStatus.DONE
    assert goal.progress == 1.0
    assert goal.completed_at is not None


def test_failure_predicate_wins_before_success_can_close_the_goal() -> None:
    mind = MindState(
        beliefs=[
            Belief(key="build.ready", statement="yes"),
            Belief(key="build.unsafe", statement="yes"),
        ]
    )
    goal = mind.add_goal(
        "promote build",
        success_conditions=[
            GoalCondition(
                kind=GoalConditionKind.BELIEF_EQUALS,
                key="build.ready",
                value="yes",
            )
        ],
        failure_conditions=[
            GoalCondition(
                kind=GoalConditionKind.BELIEF_EQUALS,
                key="build.unsafe",
                value="yes",
            )
        ],
    )

    assert GoalManager(mind).evaluate(goal) is GoalStatus.FAILED


def test_artifact_validator_and_approval_predicates_are_explicit(tmp_path: Path) -> None:
    artifact = tmp_path / "report.json"
    artifact.write_text("{}", encoding="utf-8")
    mind = MindState()
    goal = mind.add_goal(
        "publish reviewed report",
        success_conditions=[
            GoalCondition(kind=GoalConditionKind.ARTIFACT_EXISTS, path="report.json"),
            GoalCondition(kind=GoalConditionKind.VALIDATOR_PASSES, key="schema"),
            GoalCondition(kind=GoalConditionKind.HUMAN_APPROVAL, key="release"),
        ],
    )
    context = GoalEvaluationContext(
        artifact_root=tmp_path,
        validator_results={"schema": True},
        human_approvals=frozenset({"release"}),
    )

    assert GoalManager(mind).evaluate(goal, context) is GoalStatus.DONE


def test_parent_can_complete_from_child_goals() -> None:
    mind = MindState()
    parent = mind.add_goal("ship the feature")
    child = mind.add_goal("write tests", parent_goal_id=parent.id)
    parent.success_conditions = [
        GoalCondition(kind=GoalConditionKind.CHILD_GOALS_COMPLETE, key=parent.id)
    ]

    assert GoalManager(mind).evaluate(parent) is None
    child.status = GoalStatus.DONE
    assert GoalManager(mind).evaluate(parent) is GoalStatus.DONE


def test_retry_policy_backs_off_and_stops_at_its_bound() -> None:
    mind = MindState()
    goal = Goal(
        description="bounded retry",
        retry_policy=GoalRetryPolicy(
            max_attempts=2, backoff_seconds=10, backoff_multiplier=2
        ),
    )
    mind.goals.append(goal)
    manager = GoalManager(mind)
    at = now_utc()

    manager.record_failure(goal, "first", at=at)
    assert goal.status is GoalStatus.BLOCKED
    assert goal.next_attempt_at == at + timedelta(seconds=10)

    manager.record_failure(goal, "second", at=at)
    assert goal.status is GoalStatus.FAILED


def test_a_missed_deadline_fails_without_model_arbitration() -> None:
    mind = MindState()
    goal = mind.add_goal("time bounded", deadline=now_utc() - timedelta(seconds=1))

    assert GoalManager(mind).next_goal() is None
    assert goal.status is GoalStatus.FAILED
    assert goal.blocked_reason == "deadline"


def test_deterministic_utility_policy_prefers_value_then_stable_age() -> None:
    mind = MindState()
    ordinary = mind.add_goal("ordinary", priority=5)
    valuable = mind.add_goal("valuable", priority=5)
    valuable.utility = GoalUtility(expected_value=10)

    assert GoalManager(mind).next_goal() is valuable
    valuable.status = GoalStatus.DONE
    assert GoalManager(mind).next_goal() is ordinary
