"""Typed procedures through the normal runtime (closure plan T10, CG2): a
goal submitted to a running agent selects the shipped definition, runs it
under that agent's own grants, and completes only on validator evidence."""

from __future__ import annotations

import asyncio
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
from evomesh.coordination import DELEGATED_GOAL_KIND
from evomesh.environment import Environment
from evomesh.goal_manager import GoalManager
from evomesh.models import MockProvider
from evomesh.procedure_runtime import AdmissionStatus
from tests.procedure_fixtures import DELEGATED_INSPECTION, JSON_INSPECTION
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


# -- AC-11 through the live runtime: parent -> peer -> parent -------------------


async def _delegating_mesh(
    tmp_path: Path, *, coordinator_reads: bool = True, start_inspector: bool = True
) -> tuple[Environment, AgentDefinition, AgentDefinition, MockProvider, Path]:
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=False)
    provider = MockProvider()
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    registry = environment.procedures.registry
    for raw in (DELEGATED_INSPECTION, JSON_INSPECTION):
        admission = await registry.register(raw, source="template", owner="tests")
        digest = registry.definitions[admission.key].digest()
        await registry.approve(admission.key, actor="operator:test", digest=digest)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "doc.json").write_text(json.dumps({"b": 2, "a": 1}), encoding="utf-8")
    coordinator = AgentDefinition(
        name="Coordinator",
        purpose="Hands out inspections",
        status=AgentStatus.ACTIVE,
        capabilities=["work.delegate"],
        harness_root=str(shared),
    )
    inspector = AgentDefinition(
        name="Inspector",
        purpose="Inspects documents",
        status=AgentStatus.ACTIVE,
        capabilities=["artifact.read"],
        harness_root=str(shared),
    )
    for agent, reads in ((coordinator, coordinator_reads), (inspector, True)):
        await environment.register_agent(agent)
        if reads:
            await environment.grant_access(
                FilesystemGrant(agent_id=agent.id, path=str(shared), read=True)
            )
    await environment.start_agent(coordinator.id, start_delay=3600)
    if start_inspector:
        await environment.start_agent(inspector.id, start_delay=3600)
    return environment, coordinator, inspector, provider, shared


async def _until(condition, within: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_delegation_runs_parent_peer_parent_without_a_model(tmp_path: Path) -> None:
    environment, coordinator, inspector, provider, _ = await _delegating_mesh(tmp_path)
    goal = coordinator.mind.add_goal(
        "Get doc.json inspected", kind="delegated_inspection", parameters={"path": "doc.json"}
    )
    calls_before = len(provider.calls)

    await environment.cycle_agent(coordinator.name)  # delegate
    assert await _until(
        lambda: any(g.kind == DELEGATED_GOAL_KIND for g in inspector.mind.goals)
    ), "the selected peer accepted the structured work"
    child = next(g for g in inspector.mind.goals if g.kind == DELEGATED_GOAL_KIND)
    for _ in range(4):
        await environment.cycle_agent(inspector.name)
        if not child.is_open:
            break
    assert child.status is GoalStatus.DONE, child.last_error
    work_id = str(child.parameters["work_item_id"])

    async def settled() -> bool:
        row = await environment.repository.load_procedure_operation(f"delegate:{work_id}")
        return row is not None and json.loads(row[1])["state"] == "applied"

    for _ in range(100):
        if await settled():
            break
        await asyncio.sleep(0.02)
    assert await settled(), "the child's result settled on the parent's operation"
    for _ in range(4):
        await environment.cycle_agent(coordinator.name)
        if not goal.is_open:
            break

    assert goal.status is GoalStatus.DONE, goal.last_error
    execution = await environment.procedures.executor.for_occurrence(f"{goal.id}#0")
    assert execution is not None and execution.output is not None
    assert execution.output["keys"] == ["a", "b"]
    child_evidence = [item for item in execution.evidence if item.get("kind") == "child"]
    assert child_evidence and child_evidence[0]["executor"] == inspector.id
    assert len(provider.calls) == calls_before, "no model routing, planning or polling"
    await environment.stop()


async def test_delegation_cannot_launder_a_read_the_requester_lacks(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _delegating_mesh(
        tmp_path, coordinator_reads=False
    )
    goal = coordinator.mind.add_goal(
        "Get doc.json inspected", kind="delegated_inspection", parameters={"path": "doc.json"}
    )

    await environment.cycle_agent(coordinator.name)

    assert goal.status is GoalStatus.FAILED
    assert "NO_ELIGIBLE_PEER" in (goal.last_error or "")
    assert "may not read" in (goal.last_error or "")
    assert not environment.blackboard.work_items, "no child work was created"
    await environment.stop()


async def test_an_offline_peer_is_not_selected(tmp_path: Path) -> None:
    environment, coordinator, _, _, _ = await _delegating_mesh(tmp_path, start_inspector=False)
    goal = coordinator.mind.add_goal(
        "Get doc.json inspected", kind="delegated_inspection", parameters={"path": "doc.json"}
    )

    await environment.cycle_agent(coordinator.name)

    assert goal.status is GoalStatus.FAILED
    assert "NO_ELIGIBLE_PEER" in (goal.last_error or "")
    await environment.stop()


# -- AC-12 through the live runtime ----------------------------------------------


async def _one_execution(environment: Environment) -> dict:  # type: ignore[type-arg]
    rows = await environment.repository.list_procedure_executions()
    assert len(rows) == 1, f"{len(rows)} executions"
    return json.loads(rows[0][1])


async def test_a_cancelled_goal_cancels_its_execution_before_the_write(tmp_path: Path) -> None:
    environment, agent, _, work = await _mesh(tmp_path)
    goal_id = _snapshot_goal(agent)
    await environment.cycle_agent(agent.name)  # the read
    goal = agent.mind.goal(goal_id)
    GoalManager(agent.mind).transition(goal, GoalStatus.CANCELLED, human_override=True)

    await environment.cycle_agent(agent.name)

    assert (await _one_execution(environment))["status"] == "cancelled"
    assert not (work / "out" / "snapshot.json").exists()
    await environment.stop()


async def test_an_unrelated_belief_change_does_not_replan_or_rewrite(tmp_path: Path) -> None:
    environment, agent, provider, _ = await _mesh(tmp_path)
    goal_id = _snapshot_goal(agent)
    calls_before = len(provider.calls)
    await environment.cycle_agent(agent.name)  # the read
    agent.mind.revise([Belief(key="weather", statement="rain")])

    await _run(environment, agent, goal_id)

    assert agent.mind.goal(goal_id).status is GoalStatus.DONE
    execution = await _one_execution(environment)
    assert execution["completed_steps"].count("write_snapshot") == 1
    assert len(provider.calls) == calls_before
    await environment.stop()


async def test_preemption_keeps_the_execution_and_resumes_it(tmp_path: Path) -> None:
    environment, agent, _, work = await _mesh(tmp_path)
    goal_id = _snapshot_goal(agent)
    await environment.cycle_agent(agent.name)  # the read
    urgent = agent.mind.add_goal("Answer the operator now", priority=1)
    urgent.parameters["preempt"] = True

    outcome = await environment.cycle_agent(agent.name)
    held = await _one_execution(environment)
    GoalManager(agent.mind).transition(urgent, GoalStatus.DONE, human_override=True)
    await _run(environment, agent, goal_id)

    assert not outcome.step.startswith("typed"), "the urgent goal took the cycle"
    assert held["status"] == "running" and "write_snapshot" not in held["completed_steps"]
    assert agent.mind.goal(goal_id).status is GoalStatus.DONE
    execution = await _one_execution(environment)
    assert execution["completed_steps"].count("read_source") == 1, "no restart from the top"
    assert (work / "out" / "snapshot.json").exists()
    await environment.stop()


# -- shipped templates select the typed path (plan 17.3, 19) ----------------------


async def _spawned(tmp_path: Path, template: str, provider: MockProvider):  # type: ignore[no-untyped-def]
    shutil.copytree(SHIPPED, tmp_path / "procedures")
    templates = SHIPPED.parent / "agent-templates"
    shutil.copytree(templates / template, tmp_path / "agent-templates" / template)
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    await environment.agent_templates.load()
    agent = await environment.agent_templates.instantiate(environment, template)
    root = Path(agent.harness_root)
    return environment, agent, root


async def test_the_archivist_template_runs_w1_typed(tmp_path: Path) -> None:
    provider = MockProvider()
    environment, agent, root = await _spawned(tmp_path, "json-archivist", provider)
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    (root / "inbox" / "records.json").write_text('{"ids": [1, 2]}', encoding="utf-8")
    goal = agent.mind.goals[0]
    calls_before = len(provider.calls)

    for _ in range(8):
        await environment.cycle_agent(agent.name)
        if goal.occurrence == 1:
            break

    assert goal.occurrence == 1, goal.last_error
    snapshot = root / "archive" / "records.snapshot.json"
    assert json.loads(snapshot.read_text(encoding="utf-8")) == {"ids": [1, 2]}
    execution = await environment.procedures.executor.for_occurrence(f"{goal.id}#0")
    assert execution is not None and execution.path == "typed_authored"
    assert len(provider.calls) == calls_before
    await environment.stop()


async def test_the_analyst_template_runs_w2_with_one_call(tmp_path: Path) -> None:
    provider = MockProvider([GOOD])
    environment, agent, root = await _spawned(tmp_path, "report-analyst", provider)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "previous.json").write_text(
        json.dumps({"findings": REPORTS["before.json"]}), encoding="utf-8"
    )
    (root / "reports" / "current.json").write_text(
        json.dumps({"findings": REPORTS["after.json"]}), encoding="utf-8"
    )
    goal = agent.mind.goals[0]
    calls_before = len(provider.calls)

    for _ in range(10):
        await environment.cycle_agent(agent.name)
        if goal.occurrence == 1:
            break

    assert goal.occurrence == 1, goal.last_error
    assert len(provider.calls) - calls_before == 1
    written = json.loads((root / "reports" / "comparison.json").read_text(encoding="utf-8"))
    assert written["evidence_ids"] == ["F1", "F3"]
    await environment.stop()
