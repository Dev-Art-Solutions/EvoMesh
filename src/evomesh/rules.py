"""A deliberately small, bounded forward-chaining rule engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from evomesh.contracts import Belief, Goal, MindState
from evomesh.goal_manager import GoalManager


class RuleSource(StrEnum):
    BELIEF = "belief"
    EVENT = "event"
    GOAL = "goal"


class RuleOperator(StrEnum):
    EQUALS = "equals"
    EXISTS = "exists"
    GREATER_OR_EQUAL = "greater_or_equal"


class RuleEffectKind(StrEnum):
    ASSERT_BELIEF = "assert_belief"
    PROPOSE_GOAL = "propose_goal"
    EMIT_EVENT = "emit_event"
    REQUEST_ACTION = "request_action"


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleTest:
    source: RuleSource
    key: str
    operator: RuleOperator = RuleOperator.EQUALS
    value: Any = True


@dataclass(frozen=True)
class RuleEffect:
    kind: RuleEffectKind
    key: str = ""
    value: Any = True
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    name: str
    when: tuple[RuleTest, ...]
    then: tuple[RuleEffect, ...]


@dataclass
class RuleResult:
    fired: list[str] = field(default_factory=list)
    derived_beliefs: list[Belief] = field(default_factory=list)
    proposed_goals: list[Goal] = field(default_factory=list)
    events: list[RuntimeEvent] = field(default_factory=list)
    requested_actions: list[RuleEffect] = field(default_factory=list)
    capped: bool = False


class RuleEngine:
    """Run matching rules to a fixed point, at most once each per cycle."""

    def __init__(self, rules: tuple[Rule, ...] = (), *, max_firings: int = 32) -> None:
        if max_firings < 1:
            raise ValueError("max_firings must be positive")
        self.rules = rules
        self.max_firings = max_firings

    def fire(
        self, mind: MindState, events: tuple[RuntimeEvent, ...] = ()
    ) -> RuleResult:
        result = RuleResult(events=list(events))
        fired: set[str] = set()
        while len(fired) < self.max_firings:
            matched = next(
                (
                    rule
                    for rule in self.rules
                    if rule.name not in fired and self._matches(rule, mind, result.events)
                ),
                None,
            )
            if matched is None:
                break
            fired.add(matched.name)
            result.fired.append(matched.name)
            for effect in matched.then:
                self._apply(effect, mind, result)
        result.capped = len(fired) >= self.max_firings and any(
            rule.name not in fired and self._matches(rule, mind, result.events)
            for rule in self.rules
        )
        return result

    def _matches(
        self, rule: Rule, mind: MindState, events: list[RuntimeEvent]
    ) -> bool:
        return all(self._test(test, mind, events) for test in rule.when)

    def _test(
        self, test: RuleTest, mind: MindState, events: list[RuntimeEvent]
    ) -> bool:
        candidates: list[Any]
        if test.source is RuleSource.BELIEF:
            belief = mind.belief(test.key)
            candidates = [] if belief is None else [belief.statement]
        elif test.source is RuleSource.EVENT:
            candidates = [
                event.payload.get("value", True)
                for event in events
                if event.type == test.key
            ]
        else:
            candidates = [
                goal.status.value
                for goal in mind.goals
                if goal.id == test.key or goal.kind == test.key
            ]
        if test.operator is RuleOperator.EXISTS:
            return bool(candidates) is bool(test.value)
        if test.operator is RuleOperator.GREATER_OR_EQUAL:
            return any(self._number(value) >= self._number(test.value) for value in candidates)
        return any(value == test.value or str(value) == str(test.value) for value in candidates)

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")

    def _apply(self, effect: RuleEffect, mind: MindState, result: RuleResult) -> None:
        if effect.kind is RuleEffectKind.ASSERT_BELIEF:
            belief = Belief(key=effect.key, statement=str(effect.value), source="rule")
            change = mind.revise([belief])
            if change:
                result.derived_beliefs.append(mind.belief(effect.key) or belief)
            return
        if effect.kind is RuleEffectKind.PROPOSE_GOAL:
            description = str(effect.payload.get("description") or effect.value).strip()
            kind = str(effect.payload.get("kind") or effect.key or "goal")
            duplicate = next(
                (
                    goal
                    for goal in mind.goals
                    if goal.kind == kind and goal.description == description and goal.is_open
                ),
                None,
            )
            if duplicate is None and description:
                fields = {
                    key: value
                    for key, value in effect.payload.items()
                    if key
                    in {
                        "priority",
                        "parameters",
                        "dependency_goal_ids",
                        "success_conditions",
                        "failure_conditions",
                        "deadline",
                        "owner_agent_id",
                    }
                }
                result.proposed_goals.append(
                    GoalManager(mind).create(description, kind=kind, **fields)
                )
            return
        if effect.kind is RuleEffectKind.EMIT_EVENT:
            result.events.append(RuntimeEvent(effect.key, dict(effect.payload)))
            return
        if effect.kind is RuleEffectKind.REQUEST_ACTION:
            result.requested_actions.append(effect)
