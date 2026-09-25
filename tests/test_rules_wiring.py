"""Rules as live runtime input and output, not an engine nothing configures."""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.behaviors import DEGRADED_KEY, INVESTIGATE, GuardianBehavior
from evomesh.cognition import CycleContext
from evomesh.config import Settings
from evomesh.contracts import AgentDefinition, AgentPhase, AgentRuntimeState, AgentStatus
from evomesh.environment import Environment
from evomesh.events import Event, EventType
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider
from evomesh.rules import RuleEngine, rule_from_config
from tests.test_agent_templates import VALID, write_template
from tests.test_bdi import ScriptedProvider, settings_for

WATCH_RULE = {
    "name": "react to a finished dependency",
    "when": [{"source": "event", "key": "goal_completed", "value": "dep-1"}],
    "then": [
        {"kind": "propose_goal", "key": "follow_up", "value": "Publish the report"},
        {"kind": "emit_event", "key": "report.queued", "payload": {"value": "dep-1"}},
        {"kind": "request_action", "key": "announce", "value": "queued: {belief:report.name}"},
    ],
}


def test_a_configured_rule_parses_and_a_malformed_one_is_refused() -> None:
    rule = rule_from_config(WATCH_RULE)
    assert rule.name == "react to a finished dependency"
    assert len(rule.then) == 3
    with pytest.raises(ValueError, match="malformed"):
        rule_from_config(
            {
                "name": "bad",
                "when": [{"source": "nowhere", "key": "x"}],
                "then": [{"kind": "assert_belief", "key": "y"}],
            }
        )
    with pytest.raises(ValueError, match="non-empty"):
        rule_from_config({"name": "empty", "when": [], "then": []})


async def test_a_bus_event_fires_an_agents_own_rule_without_a_model(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    environment = Environment(settings_for(tmp_path), {"ollama": provider})
    await environment.start()
    agent = AgentDefinition(
        name="Reporter", purpose="Report", status=AgentStatus.ACTIVE, rules=[WATCH_RULE]
    )
    agent.mind.revise([])
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    announced: list[str] = []
    original = environment.announce

    async def capture(text: str) -> None:
        announced.append(text)
        await original(text)

    environment.announce = capture  # type: ignore[method-assign]
    await environment.events.publish(
        Event(EventType.GOAL_COMPLETED, "elsewhere", agent_id=agent.id, goal_id="dep-1")
    )

    await environment.cycle_agent("Reporter")

    assert any(goal.description == "Publish the report" for goal in agent.mind.goals)
    rule_events = [e for e in environment.events.history if e.type is EventType.RULE_EVENT]
    assert rule_events and rule_events[0].payload["type"] == "report.queued"
    created = [e for e in environment.events.history if e.type is EventType.GOAL_CREATED]
    assert any(e.payload["description"] == "Publish the report" for e in created)
    assert any(text.startswith("queued:") for text in announced)

    # Consumed: the next cycle does not see the same event again.
    goals = len(agent.mind.goals)
    await environment.cycle_agent("Reporter")
    assert len(agent.mind.goals) == goals
    await environment.stop()


async def test_a_stopped_runtime_stops_collecting_events(tmp_path: Path) -> None:
    environment = Environment(settings_for(tmp_path), {"ollama": MockProvider()})
    await environment.start()
    agent = AgentDefinition(name="Quiet", purpose="Wait", status=AgentStatus.ACTIVE)
    await environment.register_agent(agent)
    await environment.start_agent(agent.id, start_delay=3600)
    runtime = environment.runtimes[agent.id]
    await environment.stop_agent(agent.id)

    await environment.events.publish(Event(EventType.GOAL_COMPLETED, "x", agent_id=agent.id))

    assert not runtime._pending_events  # pyright: ignore[reportPrivateUsage]
    await environment.stop()


async def test_guardian_investigates_only_when_degradation_is_newly_perceived(
    tmp_path: Path,
) -> None:
    definition = AgentDefinition(name="Guardian", purpose="Watch")
    definition.mind.add_goal("Watch the mesh", recurring=True)
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    down = AgentRuntimeState(agent_id="w", name="Worker", phase=AgentPhase.ERROR)
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"provider_health": (True, "ok"), "runtime_states": {"w": down}},
    )
    behavior = GuardianBehavior()
    assert isinstance(behavior.rule_engine(), RuleEngine)

    await behavior.cycle(context)
    await behavior.cycle(context)

    investigations = [
        goal for goal in definition.mind.goals if goal.description.startswith(INVESTIGATE)
    ]
    assert len(investigations) == 1
    assert investigations[0].description.endswith("agents not running: Worker")
    assert definition.mind.believes(DEGRADED_KEY, "agents not running: Worker")


async def test_a_template_carries_capabilities_and_rules_to_its_agent(tmp_path: Path) -> None:
    text = VALID.replace(
        "tools: [mt5_bridge]\n",
        "tools: [mt5_bridge]\n"
        "rules:\n"
        "  - name: flag\n"
        "    when: [{source: belief, key: equity.low, value: 'yes'}]\n"
        "    then: [{kind: request_action, key: announce, value: equity low}]\n",
    )
    write_template(tmp_path, "trader", text)
    settings = Settings(data_path=tmp_path / "data.db", generation_path=tmp_path / "generations")
    environment = Environment(settings, {"ollama": MockProvider()})
    await environment.start()

    definition = await environment.agent_templates.instantiate(environment, "trader")

    assert definition.rules[0]["name"] == "flag"
    assert definition.capabilities == ["tool.mt5_bridge", "harness"]
    assert environment.capabilities.candidates(["tool.mt5_bridge"])[0].id == definition.id
    await environment.stop()


async def test_a_template_with_a_malformed_rule_is_refused(tmp_path: Path) -> None:
    from evomesh.agent_templates import InvalidAgentTemplateError, parse_agent_template

    text = VALID.replace(
        "tools: [mt5_bridge]\n",
        "tools: [mt5_bridge]\nrules:\n  - name: broken\n    when: []\n    then: []\n",
    )
    with pytest.raises(InvalidAgentTemplateError):
        parse_agent_template(Path("agent-templates/trader/AGENT.md"), text)
