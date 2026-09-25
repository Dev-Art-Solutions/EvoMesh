"""Typed procedures through the normal runtime (closure plan T10, CG2): a
goal submitted to a running agent selects the shipped definition, runs it
under that agent's own grants, and completes only on validator evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from evomesh.config import HarnessSettings
from evomesh.contracts import (
    AgentDefinition,
    AgentStatus,
    Belief,
    FilesystemGrant,
    GoalCondition,
    GoalConditionKind,
    GoalStatus,
)
from evomesh.environment import Environment
from evomesh.goal_manager import GoalManager
from evomesh.models import MockProvider
from evomesh.procedure_runtime import AdmissionStatus
from tests.test_bdi import settings_for

SHIPPED = Path(__file__).resolve().parents[1] / "procedures"
REPORTS = {
    "before.json": [
        {"id": "F1", "text": "p95 latency 180 ms"},
        {"id": "F2", "text": "error rate 0.4%"},
    ],
    "after.json": [
        {"id": "F3", "text": "p95 latency 95 ms"},
        {"id": "F4", "text": "error rate 0.5%"},
    ],
}
GOOD = json.dumps({"summary": "Latency halved; errors roughly flat.", "evidence_ids": ["F1", "F3"]})


async def _mesh(
    tmp_path: Path, *, grant_write: bool = True, provider: MockProvider | None = None
) -> tuple[Environment, AgentDefinition, MockProvider, Path]:
    shutil.copytree(SHIPPED, tmp_path / "procedures")
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    provider = provider or MockProvider()
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    work = tmp_path / "work"
    work.mkdir()
    (work / "source.json").write_text(json.dumps({"rows": [1, 2, 3]}), encoding="utf-8")
    for name, findings in REPORTS.items():
        (work / name).write_text(json.dumps({"findings": findings}), encoding="utf-8")
    agent = AgentDefinition(
        name="Archivist",
        purpose="Keep validated snapshots",
        status=AgentStatus.ACTIVE,
        capabilities=["artifact.read", "artifact.write"],
        harness_root=str(work),
    )
    await environment.register_agent(agent)
    await environment.grant_access(
        FilesystemGrant(agent_id=agent.id, path=str(work), read=True, write=grant_write)
    )
    await environment.start_agent(agent.id, start_delay=3600)
    return environment, agent, provider, work


def _snapshot_goal(agent: AgentDefinition) -> str:
    goal = agent.mind.add_goal(
        "Snapshot source.json",
        kind="local_json_snapshot",
        parameters={"source": "source.json", "destination": "out/snapshot.json"},
        success_conditions=[
            GoalCondition(kind=GoalConditionKind.VALIDATOR_PASSES, key="artifact_matches_source")
        ],
    )
    return goal.id


async def test_shipped_definition_is_admitted_at_start(tmp_path: Path) -> None:
    environment, _, _, _ = await _mesh(tmp_path)
    admission = environment.procedures.registry.admissions["local_json_snapshot@1"]
    assert admission.status is AdmissionStatus.PROMOTED
    assert admission.approved_by == "operator:iliya"
    await environment.stop()


async def test_w1_runs_through_a_running_agent_with_zero_model_calls(tmp_path: Path) -> None:
    environment, agent, provider, work = await _mesh(tmp_path)
    goal_id = _snapshot_goal(agent)
    calls_before = len(provider.calls)

    for _ in range(8):
        await environment.cycle_agent(agent.name)
        if agent.mind.goal(goal_id).status is GoalStatus.DONE:
            break

    goal = agent.mind.goal(goal_id)
    assert goal.status is GoalStatus.DONE, goal.last_error
    written = json.loads((work / "out" / "snapshot.json").read_text(encoding="utf-8"))
    assert written == {"rows": [1, 2, 3]}
    assert len(provider.calls) == calls_before, "W1 needs no model call"
    executions = await environment.repository.list_procedure_executions()
    assert len(executions) == 1
    await environment.stop()


async def test_w1_without_write_grant_fails_the_goal_without_fallback(tmp_path: Path) -> None:
    environment, agent, provider, work = await _mesh(tmp_path, grant_write=False)
    goal_id = _snapshot_goal(agent)
    calls_before = len(provider.calls)

    for _ in range(8):
        await environment.cycle_agent(agent.name)
        if not agent.mind.goal(goal_id).is_open:
            break

    goal = agent.mind.goal(goal_id)
    assert goal.status is GoalStatus.FAILED
    assert "PERMISSION_DENIED" in (goal.last_error or "")
    assert not (work / "out" / "snapshot.json").exists()
    assert len(provider.calls) == calls_before, "a denied typed goal is not handed to the model"
    await environment.stop()


def _comparison_goal(agent: AgentDefinition) -> str:
    goal = agent.mind.add_goal(
        "Compare the two latency reports",
        kind="report_comparison",
        parameters={
            "first": "before.json",
            "second": "after.json",
            "destination": "out/comparison.json",
        },
        success_conditions=[
            GoalCondition(kind=GoalConditionKind.VALIDATOR_PASSES, key="artifact_matches_output")
        ],
    )
    return goal.id


async def _run(environment: Environment, agent: AgentDefinition, goal_id: str) -> None:
    for _ in range(10):
        await environment.cycle_agent(agent.name)
        if not agent.mind.goal(goal_id).is_open:
            return


async def test_w2_clean_path_makes_exactly_one_bounded_model_call(tmp_path: Path) -> None:
    provider = MockProvider([GOOD])
    environment, agent, provider, work = await _mesh(tmp_path, provider=provider)
    # Unrelated history the model must never see: only declared inputs go in.
    agent.mind.revise([Belief(key="history", statement="UNRELATED-HISTORY " * 400)])
    goal_id = _comparison_goal(agent)
    calls_before = len(provider.calls)

    await _run(environment, agent, goal_id)

    goal = agent.mind.goal(goal_id)
    assert goal.status is GoalStatus.DONE, goal.last_error
    calls = provider.calls[calls_before:]
    assert len(calls) == 1, "one semantic call, no planning or routing call"
    prompt = calls[0]["prompt"]
    assert "UNRELATED-HISTORY" not in prompt
    for findings in REPORTS.values():
        for finding in findings:
            assert finding["text"] in prompt, "mandatory input survives intact"
    assert calls[0]["format"]["required"] == ["summary", "evidence_ids"]
    published = json.loads((work / "out" / "comparison.json").read_text(encoding="utf-8"))
    assert published == json.loads(GOOD)
    execution = (await environment.repository.list_procedure_executions())[-1][1]
    assert '"model_generated":true' in execution.replace(" ", "")
    await environment.stop()


async def test_w2_fake_evidence_id_is_repaired_within_the_call_budget(tmp_path: Path) -> None:
    fake = json.dumps({"summary": "Errors fell.", "evidence_ids": ["F9"]})
    provider = MockProvider([fake, GOOD])
    environment, agent, provider, _ = await _mesh(tmp_path, provider=provider)
    goal_id = _comparison_goal(agent)
    calls_before = len(provider.calls)

    await _run(environment, agent, goal_id)

    assert agent.mind.goal(goal_id).status is GoalStatus.DONE
    calls = provider.calls[calls_before:]
    assert len(calls) == 2
    assert "F9" in calls[1]["prompt"], "the repair names what was rejected"
    await environment.stop()


async def test_w2_repeated_fake_evidence_fails_without_a_third_call(tmp_path: Path) -> None:
    fake = json.dumps({"summary": "Errors fell.", "evidence_ids": ["F9"]})
    provider = MockProvider([fake])
    environment, agent, provider, work = await _mesh(tmp_path, provider=provider)
    goal_id = _comparison_goal(agent)
    calls_before = len(provider.calls)

    await _run(environment, agent, goal_id)

    goal = agent.mind.goal(goal_id)
    assert goal.status is not GoalStatus.DONE
    assert len(provider.calls) - calls_before == 2, "max_model_calls + repair_calls, no more"
    assert not (work / "out" / "comparison.json").exists()
    await environment.stop()


async def test_w2_malformed_json_then_valid_reply(tmp_path: Path) -> None:
    provider = MockProvider(["Sure! {summary: oops", GOOD])
    environment, agent, provider, _ = await _mesh(tmp_path, provider=provider)
    goal_id = _comparison_goal(agent)

    await _run(environment, agent, goal_id)

    assert agent.mind.goal(goal_id).status is GoalStatus.DONE
    await environment.stop()


async def test_w2_a_tool_call_attempt_fails_the_step_at_once(tmp_path: Path) -> None:
    provider = MockProvider([json.dumps({"tool_calls": [{"name": "shell"}]})])
    environment, agent, provider, work = await _mesh(tmp_path, provider=provider)
    goal_id = _comparison_goal(agent)
    calls_before = len(provider.calls)

    await _run(environment, agent, goal_id)

    goal = agent.mind.goal(goal_id)
    assert goal.status is GoalStatus.FAILED
    assert "UNEXPECTED_TOOL_CALL" in (goal.last_error or "")
    assert len(provider.calls) - calls_before == 1, "no repair after a tool-call attempt"
    assert not (work / "out" / "comparison.json").exists()
    await environment.stop()


async def test_w2_oversized_mandatory_input_is_refused_not_truncated(tmp_path: Path) -> None:
    provider = MockProvider([GOOD])
    environment, agent, provider, work = await _mesh(tmp_path, provider=provider)
    huge = [{"id": f"F{index}", "text": "x" * 200} for index in range(400)]
    (work / "before.json").write_text(json.dumps({"findings": huge}), encoding="utf-8")
    goal_id = _comparison_goal(agent)
    calls_before = len(provider.calls)

    await _run(environment, agent, goal_id)

    goal = agent.mind.goal(goal_id)
    assert goal.status is GoalStatus.FAILED
    assert "CONTEXT_BUDGET_EXCEEDED" in (goal.last_error or "")
    assert len(provider.calls) == calls_before
    await environment.stop()


async def test_w1_recurring_occurrence_rereads_and_gets_a_new_operation(tmp_path: Path) -> None:
    environment, agent, _, work = await _mesh(tmp_path)
    goal = agent.mind.add_goal(
        "Snapshot source.json every hour",
        kind="local_json_snapshot",
        recurring=True,
        interval_seconds=3600,
        parameters={"source": "source.json", "destination": "out/snapshot.json"},
        success_conditions=[
            GoalCondition(kind=GoalConditionKind.VALIDATOR_PASSES, key="artifact_matches_source")
        ],
    )

    async def one_occurrence() -> None:
        start = goal.occurrence
        for _ in range(8):
            await environment.cycle_agent(agent.name)
            if goal.occurrence != start:
                return
        raise AssertionError(goal.last_error)

    await one_occurrence()
    assert goal.occurrence == 1
    (work / "source.json").write_text(json.dumps({"rows": [4]}), encoding="utf-8")
    goal.next_attempt_at = None  # the hour has passed
    GoalManager(agent.mind).refresh()
    await one_occurrence()

    written = json.loads((work / "out" / "snapshot.json").read_text(encoding="utf-8"))
    assert written == {"rows": [4]}, "the new occurrence read the new source"
    repository = environment.repository
    writes = [
        json.loads(payload)["operation_key"]
        for _, execution in await repository.list_procedure_executions()
        for _, payload in await repository.list_procedure_operations(
            json.loads(execution)["execution_id"]
        )
        if json.loads(payload)["step_id"] == "write_snapshot"
    ]
    assert len(writes) == 2 and len(set(writes)) == 2, "one write identity per occurrence"
    await environment.stop()


async def test_w1_foreign_file_at_the_destination_is_a_conflict(tmp_path: Path) -> None:
    environment, agent, _, work = await _mesh(tmp_path)
    (work / "out").mkdir()
    (work / "out" / "snapshot.json").write_text('{"rows": [1, 2, 3]}\n', encoding="utf-8")
    goal_id = _snapshot_goal(agent)

    await _run(environment, agent, goal_id)

    goal = agent.mind.goal(goal_id)
    assert goal.status is GoalStatus.FAILED
    assert "DESTINATION_CONFLICT" in (goal.last_error or "")
    assert (work / "out" / "snapshot.json").read_text(encoding="utf-8") == '{"rows": [1, 2, 3]}\n'
    await environment.stop()


async def test_graph_completion_is_not_goal_achievement(tmp_path: Path) -> None:
    environment, agent, _, _ = await _mesh(tmp_path)
    goal = agent.mind.add_goal(
        "Snapshot, and a human must also sign off",
        kind="local_json_snapshot",
        parameters={"source": "source.json", "destination": "out/snapshot.json"},
        success_conditions=[
            GoalCondition(kind=GoalConditionKind.VALIDATOR_PASSES, key="artifact_matches_source"),
            GoalCondition(kind=GoalConditionKind.HUMAN_APPROVAL, key="release"),
        ],
    )

    await _run(environment, agent, goal.id)

    assert goal.status is GoalStatus.FAILED
    assert goal.last_error == "POSTCONDITION_UNSATISFIED"
    await environment.stop()
