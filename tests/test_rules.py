from __future__ import annotations

from pathlib import Path

from evomesh.bdi import BDIBehavior, PlanLibrary, PlanRecipe, ReflectiveBehavior, StepResult
from evomesh.cognition import CycleContext
from evomesh.contracts import (
    AgentDefinition,
    Belief,
    GoalCondition,
    GoalConditionKind,
    GoalStatus,
    Intention,
    MindState,
    PlanStep,
)
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider
from evomesh.rules import (
    Rule,
    RuleEffect,
    RuleEffectKind,
    RuleEngine,
    RuleOperator,
    RuleSource,
    RuleTest,
)


def degraded_rule() -> Rule:
    return Rule(
        name="degraded provider",
        when=(
            RuleTest(
                source=RuleSource.BELIEF,
                key="provider.failure_count",
                operator=RuleOperator.GREATER_OR_EQUAL,
                value=3,
            ),
        ),
        then=(
            RuleEffect(
                kind=RuleEffectKind.ASSERT_BELIEF,
                key="mesh.degraded",
                value="true",
            ),
            RuleEffect(
                kind=RuleEffectKind.PROPOSE_GOAL,
                key="investigate_provider",
                value="Investigate provider",
            ),
        ),
    )


def test_rule_engine_derives_beliefs_and_deduplicates_proposed_goals() -> None:
    mind = MindState(beliefs=[Belief(key="provider.failure_count", statement="3")])
    engine = RuleEngine((degraded_rule(),))

    first = engine.fire(mind)
    second = engine.fire(mind)

    assert first.fired == ["degraded provider"]
    assert mind.believes("mesh.degraded", "true")
    assert len(first.proposed_goals) == 1
    assert second.proposed_goals == []
    assert len(mind.goals) == 1


def test_rule_engine_caps_firings() -> None:
    rules = tuple(
        Rule(
            name=f"rule-{index}",
            when=(),
            then=(
                RuleEffect(
                    kind=RuleEffectKind.ASSERT_BELIEF,
                    key=f"derived.{index}",
                    value="yes",
                ),
            ),
        )
        for index in range(4)
    )

    result = RuleEngine(rules, max_firings=2).fire(MindState())

    assert len(result.fired) == 2
    assert result.capped


class ZeroModelScenario(BDIBehavior):
    async def perceive(self, context: CycleContext) -> list[Belief]:
        return [Belief(key="request.ready", statement="yes", source="test")]

    def rule_engine(self) -> RuleEngine:
        return RuleEngine(
            (
                Rule(
                    name="turn request into work",
                    when=(
                        RuleTest(
                            source=RuleSource.BELIEF,
                            key="request.ready",
                            value="yes",
                        ),
                    ),
                    then=(
                        RuleEffect(
                            kind=RuleEffectKind.PROPOSE_GOAL,
                            key="process_request",
                            value="Process the request",
                            payload={
                                "success_conditions": [
                                    GoalCondition(
                                        kind=GoalConditionKind.BELIEF_EQUALS,
                                        key="request.processed",
                                        value="yes",
                                    )
                                ]
                            },
                        ),
                    ),
                ),
            )
        )

    def library(self) -> PlanLibrary:
        return PlanLibrary(
            (
                PlanRecipe(
                    name="process-known-request",
                    goal_kind="process_request",
                    steps=("mark the request processed",),
                    action="deterministic",
                ),
            )
        )

    async def execute(
        self, context: CycleContext, intention: Intention, step: PlanStep
    ) -> StepResult:
        context.definition.mind.revise(
            [Belief(key="request.processed", statement="yes", source="plan")]
        )
        return StepResult(summary="processed deterministically", achieved=True)


async def test_percept_rule_goal_known_plan_and_predicate_need_zero_model_calls(
    tmp_path: Path,
) -> None:
    provider = MockProvider(["a model call would make this test fail"])
    definition = AgentDefinition(name="Determinist", purpose="Known work")
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=provider,
        memory=memory,
        budget=MemoryBudget(),
    )

    outcome = await ZeroModelScenario().cycle(context)

    assert outcome.goal_done
    assert outcome.summary == "processed deterministically"
    assert definition.mind.goals[0].status is GoalStatus.DONE
    assert definition.mind.plan_statistics["process-known-request"].selected == 1
    assert definition.mind.plan_statistics["process-known-request"].succeeded == 1
    assert provider.calls == []


async def test_known_deterministic_work_uses_two_fewer_calls_than_novel_work(
    tmp_path: Path,
) -> None:
    novel_provider = MockProvider(
        [
            "1. inspect the request",
            "RESULT: inspected\nFACT: NONE\nSTATUS: done",
        ]
    )
    novel_definition = AgentDefinition(name="Novel", purpose="Novel work")
    novel_definition.mind.add_goal("Handle an unfamiliar request")
    novel_memory = AgentMemory(tmp_path / "novel", novel_definition)
    await novel_memory.ensure()
    novel_context = CycleContext(
        definition=novel_definition,
        provider=novel_provider,
        memory=novel_memory,
        budget=MemoryBudget(),
    )

    known_provider = MockProvider(["must not be used"])
    known_definition = AgentDefinition(name="Known", purpose="Known work")
    known_memory = AgentMemory(tmp_path / "known", known_definition)
    await known_memory.ensure()
    known_context = CycleContext(
        definition=known_definition,
        provider=known_provider,
        memory=known_memory,
        budget=MemoryBudget(),
    )

    await ReflectiveBehavior().cycle(novel_context)
    await ZeroModelScenario().cycle(known_context)

    assert len(novel_provider.calls) == 2  # one plan call and one execution call
    assert known_provider.calls == []
