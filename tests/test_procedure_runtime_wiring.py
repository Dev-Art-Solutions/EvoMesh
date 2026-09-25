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
    FilesystemGrant,
    GoalCondition,
    GoalConditionKind,
    GoalStatus,
)
from evomesh.environment import Environment
from evomesh.models import MockProvider
from evomesh.procedure_runtime import AdmissionStatus
from tests.test_bdi import settings_for

SHIPPED = Path(__file__).resolve().parents[1] / "procedures"


async def _mesh(
    tmp_path: Path, *, grant_write: bool = True
) -> tuple[Environment, AgentDefinition, MockProvider, Path]:
    shutil.copytree(SHIPPED, tmp_path / "procedures")
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    provider = MockProvider()
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    work = tmp_path / "work"
    work.mkdir()
    (work / "source.json").write_text(json.dumps({"rows": [1, 2, 3]}), encoding="utf-8")
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
