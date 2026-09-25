"""A deliberately small, bounded forward-chaining rule engine."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
    STARTS_WITH = "starts_with"


class RuleEffectKind(StrEnum):
    ASSERT_BELIEF = "assert_belief"
    PROPOSE_GOAL = "propose_goal"
    EMIT_EVENT = "emit_event"
    REQUEST_ACTION = "request_action"


# The event every revised belief key becomes for one cycle, so a rule can
# react to a belief *changing* rather than to it merely holding.
BELIEF_CHANGED_EVENT = "belief_changed"

# `{belief:<key>}` inside a proposed goal's description or an action's value
# is replaced by that belief's statement when the rule fires.
BELIEF_PLACEHOLDER = re.compile(r"\{belief:([^{}]+)\}")


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

    def with_rules(self, extra: Sequence[Rule]) -> RuleEngine:
        """This engine plus ``extra`` (an agent's own configured rules)."""
        return RuleEngine((*self.rules, *extra), max_firings=self.max_firings)

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
        if test.operator is RuleOperator.STARTS_WITH:
            return any(str(value).startswith(str(test.value)) for value in candidates)
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
            description = _render(
                str(effect.payload.get("description") or effect.value), mind
            ).strip()
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
            result.requested_actions.append(
                RuleEffect(
                    effect.kind,
                    effect.key,
                    _render(effect.value, mind) if isinstance(effect.value, str) else effect.value,
                    dict(effect.payload),
                )
            )


def _render(text: str, mind: MindState) -> str:
    def belief(match: re.Match[str]) -> str:
        found = mind.belief(match.group(1).strip())
        return found.statement if found is not None else ""

    return BELIEF_PLACEHOLDER.sub(belief, text)


def rule_from_config(raw: Mapping[str, Any]) -> Rule:
    """One rule from its YAML/JSON shape::

        name: degraded provider
        when:
          - {source: belief, key: provider.failures, operator: greater_or_equal, value: 3}
        then:
          - {kind: propose_goal, key: investigate, value: "Investigate {belief:provider.status}"}

    Raises ``ValueError`` on anything malformed, so a bad rule is refused
    where it is declared instead of silently never firing.
    """
    name = str(raw.get("name") or "").strip()
    when = raw.get("when")
    then = raw.get("then")
    if not name:
        raise ValueError("a rule needs a name")
    if not isinstance(when, list) or not when or not isinstance(then, list) or not then:
        raise ValueError(f"rule {name!r} needs a non-empty 'when' list and 'then' list")
    try:
        tests = tuple(
            RuleTest(
                source=RuleSource(str(item["source"])),
                key=str(item["key"]),
                operator=RuleOperator(str(item.get("operator") or RuleOperator.EQUALS.value)),
                value=item.get("value", True),
            )
            for item in when
        )
        effects = tuple(
            RuleEffect(
                kind=RuleEffectKind(str(item["kind"])),
                key=str(item.get("key") or ""),
                value=item.get("value", True),
                payload=dict(item.get("payload") or {}),
            )
            for item in then
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"rule {name!r} is malformed: {exc}") from exc
    return Rule(name=name, when=tests, then=effects)


def rules_from_config(raw: Sequence[Mapping[str, Any]]) -> tuple[Rule, ...]:
    return tuple(rule_from_config(item) for item in raw)
