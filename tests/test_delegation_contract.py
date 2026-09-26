"""Delegated work through the live runtime (closure audit 9739188, R01 and
R02): the child really runs inside the WorkItem's allocation, deadline and
success contract, a replayed DELEGATE runs nothing twice, and a delegated
file is the requester's file -- not whatever the child's root has under the
same name."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from evomesh.config import HarnessSettings
from evomesh.contracts import AgentDefinition, AgentStatus, FilesystemGrant, Goal, GoalStatus
from evomesh.coordination import (
    DELEGATED_GOAL_KIND,
    Performative,
    WorkItem,
    semantic_message,
)
from evomesh.environment import Environment
from evomesh.models import MockProvider
from evomesh.procedure_runtime import CORE_OUTPUTS, ExecStatus, occurrence_id
from evomesh.procedures import OutputContract
from tests.procedure_fixtures import DELEGATED_INSPECTION, JSON_INSPECTION, ref
from tests.test_bdi import settings_for

INSPECTION_SCHEMA = next(
    item.schema for item in CORE_OUTPUTS if item.schema_id == "authorized_json_inspection_v1"
)


def _parent(
    procedure_id: str,
    work_kind: str,
    *,
    model_calls: int = 0,
    attempts: int = 1,
    deadline: float = 600,
    inputs: dict[str, Any] | None = None,
    success_contract: str = "authorized_json_inspection_v1",
) -> dict[str, Any]:
    raw = copy.deepcopy(DELEGATED_INSPECTION)
    raw["procedure_id"] = raw["goal_kind"] = procedure_id
    hand_off = raw["steps"][0]
    hand_off["work_kind"] = work_kind
    hand_off["success_contract"] = success_contract
    hand_off["budget"] = {
        "max_model_calls": model_calls,
        "max_attempts": attempts,
        "deadline_seconds": deadline,
    }
    if inputs is not None:
        hand_off["inputs"] = inputs
        raw["parameter_schema"]["properties"] = {
            name: {"type": "string", "maxLength": 500} for name in inputs
        }
        raw["parameter_schema"]["required"] = sorted(inputs)
    return raw


def _cognitive_child() -> dict[str, Any]:
    """Read, then one schema-bound model call: the call the allocation governs."""
    raw = copy.deepcopy(JSON_INSPECTION)
    raw["procedure_id"] = raw["goal_kind"] = "cognitive_inspection"
    raw["output_schema"] = INSPECTION_SCHEMA
    raw["steps"] = [
        {**raw["steps"][0], "next": "think"},
        {
            "id": "think",
            "kind": "cognitive",
            "service": "synthesize_evidence",
            "reason": "synthesis_required",
            "instruction": "Describe the document.",
            "inputs": {"document": ref("result", "read", "value")},
            "output_schema": "authorized_json_inspection_v1",
            "max_model_calls": 1,
            "repair_calls": 1,
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {
                "artifact_id": ref("result", "think", "artifact_id"),
                "keys": ref("result", "think", "keys"),
                "digest": ref("result", "think", "digest"),
            },
        },
    ]
    return raw


def _verified_copy(*, fixed_destination: str | None = None) -> dict[str, Any]:
    """Read a document, write it to the task's destination, and prove the copy
    with the runtime's own validator -- the evidence a contract can require."""
    raw = copy.deepcopy(JSON_INSPECTION)
    raw["procedure_id"] = raw["goal_kind"] = "verified_copy"
    raw["required_capabilities"] = ["artifact.read", "artifact.write"]
    raw["parameter_schema"] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "maxLength": 500},
            "destination": {"type": "string", "maxLength": 500},
        },
        "required": ["path", "destination"],
        "additionalProperties": False,
    }
    destination = (
        {"literal": fixed_destination}
        if fixed_destination is not None
        else ref("goal", "parameters", "destination")
    )
    raw["steps"] = [
        {**raw["steps"][0], "next": "write"},
        {
            "id": "write",
            "kind": "tool",
            "adapter": "core.json_write",
            "contract_version": 1,
            "arguments": {"path": destination, "value": ref("result", "read", "value")},
            "next": "check",
        },
        {
            "id": "check",
            "kind": "validate",
            "check": "artifact_matches_source",
            "arguments": {
                "artifact_id": ref("result", "write", "artifact_id"),
                "source_step": {"literal": "read"},
            },
            "next": "done",
        },
        {
            "id": "done",
            "kind": "complete",
            "result": {
                "artifact_id": ref("result", "write", "artifact_id"),
                "keys": ref("result", "read", "keys"),
                "digest": ref("result", "read", "source_digest"),
            },
        },
    ]
    return raw


async def _promote(environment: Environment, *raws: dict[str, Any]) -> None:
    registry = environment.procedures.registry
    for raw in raws:
        admission = await registry.register(raw, source="template", owner="tests")
        digest = registry.definitions[admission.key].digest()
        await registry.approve(admission.key, actor="operator:test", digest=digest)


async def _pair(
    tmp_path: Path,
    *,
    provider: MockProvider | None = None,
    coordinator_root: Path | None = None,
    inspector_root: Path | None = None,
    coordinator_grants: tuple[tuple[Path, bool], ...] | None = None,
    inspector_grants: tuple[tuple[Path, bool], ...] | None = None,
) -> tuple[Environment, AgentDefinition, AgentDefinition, MockProvider, Path]:
    """A coordinator and an inspector, each with its own root and grants
    (path, write). Defaults: both under one shared, readable directory."""
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    provider = provider or MockProvider()
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    (shared / "doc.json").write_text(json.dumps({"b": 2, "a": 1}), encoding="utf-8")
    coordinator = AgentDefinition(
        name="Coordinator",
        purpose="Hands out inspections",
        status=AgentStatus.ACTIVE,
        capabilities=["work.delegate"],
        harness_root=str(coordinator_root or shared),
    )
    inspector = AgentDefinition(
        name="Inspector",
        purpose="Inspects documents",
        status=AgentStatus.ACTIVE,
        capabilities=["artifact.read", "artifact.write"],
        harness_root=str(inspector_root or shared),
    )
    for agent, grants in (
        (coordinator, coordinator_grants or ((shared, False),)),
        (inspector, inspector_grants or ((shared, False),)),
    ):
        await environment.register_agent(agent)
        for path, write in grants:
            await environment.grant_access(
                FilesystemGrant(agent_id=agent.id, path=str(path), read=True, write=write)
            )
    await environment.start_agent(coordinator.id, start_delay=3600)
    await environment.start_agent(inspector.id, start_delay=3600)
    return environment, coordinator, inspector, provider, shared


async def _until(condition, within: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


def _children(inspector: AgentDefinition) -> list[Goal]:
    return [goal for goal in inspector.mind.goals if goal.kind == DELEGATED_GOAL_KIND]


async def _delegate_one(
    environment: Environment, coordinator: AgentDefinition, inspector: AgentDefinition, goal: Goal
) -> Goal:
    await environment.cycle_agent(coordinator.name)
    assert await _until(lambda: bool(_children(inspector))), goal.last_error
    return _children(inspector)[0]


async def _run_child(environment: Environment, inspector: AgentDefinition, child: Goal) -> None:
    for _ in range(6):
        await environment.cycle_agent(inspector.name)
        if not child.is_open:
            return


async def _settled(environment: Environment, work_id: str) -> bool:
    for _ in range(100):
        row = await environment.repository.load_procedure_operation(f"delegate:{work_id}")
        if row is not None and json.loads(row[1])["state"] != "waiting":
            return True
        await asyncio.sleep(0.02)
    return False


async def _run_parent(environment: Environment, coordinator: AgentDefinition, goal: Goal) -> None:
    for _ in range(6):
        await environment.cycle_agent(coordinator.name)
        if not goal.is_open:
            return


# -- R01: the WorkItem's allocation is what the child actually runs under ---------


async def test_r01a_a_child_allocated_no_model_call_makes_none(tmp_path: Path) -> None:
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path)
    await _promote(
        environment,
        _cognitive_child(),
        _parent("hand_off_cognitive", "cognitive_inspection", model_calls=0),
    )
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_cognitive", parameters={"path": "doc.json"}
    )
    calls_before = len(provider.calls)

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert "BUDGET_EXHAUSTED" in (child.last_error or "")
    assert len(provider.calls) == calls_before, "the admitted cognitive step never reached a model"
    execution = await environment.procedures.executor.for_occurrence(occurrence_id(child))
    assert execution is not None and execution.budget.max_model_calls == 0
    assert "think" not in execution.completed_steps
    await environment.stop()


async def test_r01b_an_expired_deadline_starts_no_operation(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(
        environment,
        _parent("hand_off_quick", "json_inspection", deadline=0.05),
        JSON_INSPECTION,
    )
    goal = coordinator.mind.add_goal(
        "Inspect doc.json quickly", kind="hand_off_quick", parameters={"path": "doc.json"}
    )

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await asyncio.sleep(0.1)
    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert "DEADLINE_EXCEEDED" in (child.last_error or "")
    execution = await environment.procedures.executor.for_occurrence(occurrence_id(child))
    assert execution is not None
    assert await environment.procedures.executor.operations(execution.execution_id) == []
    await environment.stop()


async def test_r01b_a_running_child_ends_no_later_than_its_parent(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    # The step would allow an hour; the parent's own execution only ten minutes.
    await _promote(
        environment, _parent("hand_off_long", "json_inspection", deadline=3600), JSON_INSPECTION
    )
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_long", parameters={"path": "doc.json"}
    )

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)

    executor = environment.procedures.executor
    parent = await executor.for_occurrence(occurrence_id(goal))
    running = await executor.for_occurrence(occurrence_id(child))
    assert parent is not None and running is not None
    assert running.deadline_at <= parent.deadline_at
    await environment.stop()


async def test_r01c_a_replacement_does_not_reset_the_allocation(tmp_path: Path) -> None:
    provider = MockProvider(["not json at all"] * 10)
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path, provider=provider)
    await _promote(
        environment,
        _cognitive_child(),
        _parent("hand_off_once", "cognitive_inspection", model_calls=1, attempts=2),
    )
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_once", parameters={"path": "doc.json"}
    )
    calls_before = len(provider.calls)

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)
    assert len(provider.calls) - calls_before == 1, "one call allowed, one call made"

    # Replan: a second execution for the same occurrence, as the reasoner
    # would start it. It inherits the spent call and makes no new one.
    service = environment.procedures
    replacement, _ = await service.begin(child, inspector)
    assert replacement is not None
    outcome = await service.advance(replacement.execution_id)
    for _ in range(4):
        if outcome.kind in {"failed", "completed"}:
            break
        outcome = await service.advance(replacement.execution_id)
    assert outcome.kind == "failed" and outcome.code == "BUDGET_EXHAUSTED"
    # The WorkItem allowed two attempts; a third is not an attempt at all.
    third, refusal = await service.begin(child, inspector)
    assert third is None and refusal is not None
    assert "attempt" in "; ".join(refusal.reasons)
    assert len(provider.calls) - calls_before == 1, "provider invocations, not counters"
    await environment.stop()


async def test_r01d_a_replayed_delegate_runs_nothing_twice(tmp_path: Path) -> None:
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )
    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)
    assert child.status is GoalStatus.DONE, child.last_error
    work_id = str(child.parameters["work_item_id"])
    assert await _settled(environment, work_id)
    row = await environment.repository.load_procedure_operation(f"delegate:{work_id}")
    assert row is not None
    payload = json.loads(row[1])["arguments"]["work"]
    executions_before = len(await environment.repository.list_procedure_executions())
    calls_before = len(provider.calls)

    for _ in range(2):
        await environment.bus.send(
            semantic_message(
                Performative.DELEGATE,
                sender_id=coordinator.id,
                recipient_id=inspector.id,
                task_id=work_id,
                goal_id=goal.id,
                payload=payload,
                content="replayed",
            )
        )
    await asyncio.sleep(0.2)
    for _ in range(3):
        await environment.cycle_agent(inspector.name)

    assert len(_children(inspector)) == 1, "no second goal for the same WorkItem"
    assert len(await environment.repository.list_procedure_executions()) == executions_before
    assert len(provider.calls) == calls_before
    await environment.stop()


async def test_r01d_a_replay_after_the_goal_was_pruned_is_still_refused(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )
    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)
    work_id = str(child.parameters["work_item_id"])
    row = await environment.repository.load_procedure_operation(f"delegate:{work_id}")
    assert row is not None
    payload = json.loads(row[1])["arguments"]["work"]
    inspector.mind.goals = [item for item in inspector.mind.goals if item.id != child.id]

    await environment.bus.send(
        semantic_message(
            Performative.DELEGATE,
            sender_id=coordinator.id,
            recipient_id=inspector.id,
            task_id=work_id,
            payload=payload,
        )
    )
    await asyncio.sleep(0.2)

    assert _children(inspector) == [], "the durable ledger, not the goal list, decides"
    await environment.stop()


async def test_r01e_schema_valid_output_without_the_required_evidence_fails(
    tmp_path: Path,
) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    environment.procedures.registry.catalog.add_output(
        OutputContract(
            "verified_inspection_v1",
            INSPECTION_SCHEMA,
            required_evidence=("artifact_matches_source",),
        )
    )
    await _promote(
        environment,
        _parent("hand_off_verified", "json_inspection", success_contract="verified_inspection_v1"),
        JSON_INSPECTION,
    )
    goal = coordinator.mind.add_goal(
        "Inspect doc.json, with proof", kind="hand_off_verified", parameters={"path": "doc.json"}
    )

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)
    assert child.status is GoalStatus.DONE, "the child's graph itself completed"
    assert await _settled(environment, str(child.parameters["work_item_id"]))
    await _run_parent(environment, coordinator, goal)

    assert goal.status is GoalStatus.FAILED
    assert "CHILD_CONTRACT_UNSATISFIED" in (goal.last_error or "")
    await environment.stop()


async def test_r01e_the_required_evidence_from_the_child_satisfies_it(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    environment, coordinator, inspector, _, _ = await _pair(
        tmp_path,
        coordinator_grants=((shared, True),),
        inspector_grants=((shared, True),),
    )
    environment.procedures.registry.catalog.add_output(
        OutputContract(
            "verified_inspection_v1",
            INSPECTION_SCHEMA,
            required_evidence=("artifact_matches_source",),
        )
    )
    inputs = {
        "path": ref("goal", "parameters", "path"),
        "destination": ref("goal", "parameters", "destination"),
    }
    await _promote(
        environment,
        _parent(
            "hand_off_copy",
            "verified_copy",
            inputs=inputs,
            success_contract="verified_inspection_v1",
        ),
        _verified_copy(),
    )
    goal = coordinator.mind.add_goal(
        "Copy doc.json, with proof",
        kind="hand_off_copy",
        parameters={"path": "doc.json", "destination": "out/copy.json"},
    )

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)
    assert child.status is GoalStatus.DONE, child.last_error
    assert await _settled(environment, str(child.parameters["work_item_id"]))
    await _run_parent(environment, coordinator, goal)

    assert goal.status is GoalStatus.DONE, goal.last_error
    assert json.loads((shared / "out" / "copy.json").read_text(encoding="utf-8")) == {
        "a": 1,
        "b": 2,
    }
    await environment.stop()


# -- R02: one resource identity, resolved in the requester's scope ---------------


def _two_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    public, private = workspace / "public", workspace / "private"
    public.mkdir(parents=True)
    private.mkdir(parents=True)
    (public / "doc.json").write_text(json.dumps({"public": 1}), encoding="utf-8")
    (private / "doc.json").write_text(json.dumps({"secret": 1}), encoding="utf-8")
    return workspace, public, private


async def test_r02a_same_name_in_another_root_is_not_the_same_resource(tmp_path: Path) -> None:
    workspace, public, private = _two_roots(tmp_path)
    environment, coordinator, inspector, _, _ = await _pair(
        tmp_path,
        coordinator_root=public,
        inspector_root=private,
        coordinator_grants=((public, False),),
        # The recipient may read both files, but its root is the private one.
        inspector_grants=((workspace, False),),
    )
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )

    await environment.cycle_agent(coordinator.name)
    await asyncio.sleep(0.1)

    assert goal.status is GoalStatus.FAILED
    assert "NO_ELIGIBLE_PEER" in (goal.last_error or "")
    assert "outside its root" in (goal.last_error or "")
    assert _children(inspector) == [], "the private doc.json was never handed out"
    await environment.stop()


async def test_r02a_the_child_reads_the_requesters_file_not_its_own(tmp_path: Path) -> None:
    workspace, public, _ = _two_roots(tmp_path)
    environment, coordinator, inspector, _, _ = await _pair(
        tmp_path,
        coordinator_root=public,
        # A root that holds both: a relative "doc.json" would be its own.
        inspector_root=workspace,
        coordinator_grants=((public, False),),
        inspector_grants=((workspace, False),),
    )
    (workspace / "doc.json").write_text(json.dumps({"inspector_own": 1}), encoding="utf-8")
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)
    assert await _settled(environment, str(child.parameters["work_item_id"]))
    await _run_parent(environment, coordinator, goal)

    assert goal.status is GoalStatus.DONE, goal.last_error
    execution = await environment.procedures.executor.for_occurrence(occurrence_id(goal))
    assert execution is not None and execution.output is not None
    assert execution.output["keys"] == ["public"], "the file the requester was authorized for"
    await environment.stop()


async def test_r02b_a_nested_path_is_checked_like_a_top_level_one(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "secret").mkdir()
    (shared / "secret" / "doc.json").write_text('{"secret": 1}', encoding="utf-8")
    environment, coordinator, inspector, _, _ = await _pair(
        tmp_path,
        coordinator_grants=((shared / "doc.json", False),),
        inspector_grants=((shared, False),),
    )
    inputs = {
        "path": ref("goal", "parameters", "path"),
        "options": {"literal": {"extra": [{"source_path": "secret/doc.json"}]}},
    }
    raw = _parent("hand_off_nested", "json_inspection")
    raw["steps"][0]["inputs"] = inputs
    await _promote(environment, raw, JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_nested", parameters={"path": "doc.json"}
    )

    await environment.cycle_agent(coordinator.name)
    await asyncio.sleep(0.1)

    assert goal.status is GoalStatus.FAILED
    assert "may not read secret/doc.json" in (goal.last_error or "")
    assert _children(inspector) == []
    await environment.stop()


async def test_r02b_a_declared_resource_under_an_innocent_name_is_checked(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "secret.json").write_text('{"secret": 1}', encoding="utf-8")
    environment, coordinator, inspector, _, _ = await _pair(
        tmp_path,
        coordinator_grants=((shared / "doc.json", False),),
        inspector_grants=((shared, False),),
    )
    # The child's contract binds "document" into json_read's path: that
    # binding, not the parameter's name, makes it a resource.
    child = copy.deepcopy(JSON_INSPECTION)
    child["procedure_id"] = child["goal_kind"] = "document_inspection"
    child["parameter_schema"]["properties"] = {"document": {"type": "string", "maxLength": 500}}
    child["parameter_schema"]["required"] = ["document"]
    child["steps"][0]["arguments"] = {"path": ref("goal", "parameters", "document")}
    parent = _parent(
        "hand_off_document",
        "document_inspection",
        inputs={"document": ref("goal", "parameters", "document")},
    )
    await _promote(environment, parent, child)
    goal = coordinator.mind.add_goal(
        "Inspect it", kind="hand_off_document", parameters={"document": "secret.json"}
    )

    await environment.cycle_agent(coordinator.name)
    await asyncio.sleep(0.1)

    assert goal.status is GoalStatus.FAILED
    assert "may not read secret.json" in (goal.last_error or "")
    assert _children(inspector) == []
    await environment.stop()


async def test_r02c_a_revocation_after_assignment_stops_the_child(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, shared = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )
    child = await _delegate_one(environment, coordinator, inspector, goal)

    await environment.revoke_access(coordinator.id, str(shared))
    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert "authority is gone" in (child.last_error or "")
    execution = await environment.procedures.executor.for_occurrence(occurrence_id(child))
    assert execution is not None
    assert await environment.procedures.executor.operations(execution.execution_id) == []
    await environment.stop()


async def test_r02d_a_write_needs_a_destination_the_requester_may_write(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    environment, coordinator, inspector, _, _ = await _pair(
        tmp_path,
        coordinator_grants=((shared, False),),  # read only
        inspector_grants=((shared, True),),
    )
    inputs = {
        "path": ref("goal", "parameters", "path"),
        "destination": ref("goal", "parameters", "destination"),
    }
    await _promote(
        environment, _parent("hand_off_copy", "verified_copy", inputs=inputs), _verified_copy()
    )
    goal = coordinator.mind.add_goal(
        "Copy doc.json",
        kind="hand_off_copy",
        parameters={"path": "doc.json", "destination": "out/copy.json"},
    )

    await environment.cycle_agent(coordinator.name)
    await asyncio.sleep(0.1)

    assert goal.status is GoalStatus.FAILED
    assert "may not write out/copy.json" in (goal.last_error or "")
    assert not (shared / "out").exists()
    await environment.stop()


async def test_r02d_the_child_cannot_write_where_the_task_did_not_say(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    environment, coordinator, inspector, _, _ = await _pair(
        tmp_path,
        coordinator_grants=((shared, True),),
        # The child's own grant is broad enough to write anywhere here.
        inspector_grants=((shared, True),),
    )
    inputs = {
        "path": ref("goal", "parameters", "path"),
        "destination": ref("goal", "parameters", "destination"),
    }
    await _promote(
        environment,
        _parent("hand_off_copy", "verified_copy", inputs=inputs),
        _verified_copy(fixed_destination="elsewhere.json"),
    )
    goal = coordinator.mind.add_goal(
        "Copy doc.json",
        kind="hand_off_copy",
        parameters={"path": "doc.json", "destination": "out/copy.json"},
    )

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert "grants no write" in (child.last_error or "")
    assert not (shared / "elsewhere.json").exists()
    execution = await environment.procedures.executor.for_occurrence(occurrence_id(child))
    assert execution is not None and execution.status is ExecStatus.FAILED
    await environment.stop()


# -- R01-F (review 899778e): a bounded WorkItem never escapes to legacy work -----


def _harness_jobs_for(environment: Environment, agent: AgentDefinition) -> list[object]:
    return [job for job in environment.harness.queue.jobs.values() if job.agent_id == agent.id]


async def test_r01f1_a_positive_allocation_without_a_typed_child_is_refused(
    tmp_path: Path,
) -> None:
    provider = MockProvider(["1. Read doc.json\n2. Report its keys"] * 10)
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path, provider=provider)
    # The inspector has no procedure for this work kind: NO_MATCH.
    await _promote(environment, _parent("hand_off_one", "json_inspection", model_calls=1))
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_one", parameters={"path": "doc.json"}
    )
    calls_before = len(provider.calls)

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert "unsupported_execution" in (child.last_error or "")
    assert len(provider.calls) == calls_before, "no legacy planning, no model-backed step"
    assert _harness_jobs_for(environment, inspector) == [], "no unscoped harness work"
    await environment.stop()


async def test_r01f2_an_incompatible_procedure_does_not_erase_the_contract(
    tmp_path: Path,
) -> None:
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path)
    other = copy.deepcopy(JSON_INSPECTION)
    other["parameter_schema"]["properties"] = {"document": {"type": "string", "maxLength": 500}}
    other["parameter_schema"]["required"] = ["document"]
    other["steps"][0]["arguments"] = {"path": ref("goal", "parameters", "document")}
    await _promote(environment, _parent("hand_off_one", "json_inspection", model_calls=1), other)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_one", parameters={"path": "doc.json"}
    )
    calls_before = len(provider.calls)

    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert "unsupported_execution" in (child.last_error or "")
    assert len(provider.calls) == calls_before
    await environment.stop()


async def test_r01f2_disabling_typed_execution_does_not_erase_the_contract(
    tmp_path: Path,
) -> None:
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path)
    await _promote(
        environment, _parent("hand_off_one", "json_inspection", model_calls=1), JSON_INSPECTION
    )
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_one", parameters={"path": "doc.json"}
    )
    child = await _delegate_one(environment, coordinator, inspector, goal)
    environment.procedures.registry.enabled = False  # the operator's emergency switch
    calls_before = len(provider.calls)

    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert "unsupported_execution" in (child.last_error or "")
    assert len(provider.calls) == calls_before
    assert _harness_jobs_for(environment, inspector) == []
    await environment.stop()


async def test_r01f3_an_expired_deadline_starts_no_fallback_either(tmp_path: Path) -> None:
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path)
    await _promote(
        environment, _parent("hand_off_quick", "json_inspection", model_calls=1, deadline=0.05)
    )
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off_quick", parameters={"path": "doc.json"}
    )
    child = await _delegate_one(environment, coordinator, inspector, goal)
    await asyncio.sleep(0.1)
    calls_before = len(provider.calls)

    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.FAILED
    assert len(provider.calls) == calls_before
    await environment.stop()


async def test_r01f5_ordinary_and_unbounded_legacy_work_still_falls_through(
    tmp_path: Path,
) -> None:
    provider = MockProvider(["1. Think about it"] * 10)
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path, provider=provider)
    # A delegation made the old way: no allocation, deadline or resources.
    legacy = WorkItem(
        parent_goal_id="g",
        requester_agent_id=coordinator.id,
        type="task",
        objective="Summarize the weather",
        required_capabilities=["artifact.read"],
    )
    legacy.assign(inspector.id)
    environment.blackboard.publish_work(legacy)
    await environment.bus.send(
        semantic_message(
            Performative.DELEGATE,
            sender_id=coordinator.id,
            recipient_id=inspector.id,
            task_id=legacy.id,
            payload=legacy.model_dump(mode="json"),
        )
    )
    assert await _until(lambda: bool(_children(inspector)))
    calls_before = len(provider.calls)

    for _ in range(3):
        await environment.cycle_agent(inspector.name)

    assert _children(inspector)[0].status is not GoalStatus.FAILED
    assert len(provider.calls) > calls_before, "legacy reasoning still serves it"
    await environment.stop()


# -- R01-G (review 899778e): accepting work is atomic with its goal -------------


async def _delegate_payload(
    environment: Environment, coordinator: AgentDefinition
) -> tuple[str, dict[str, Any]]:
    await environment.cycle_agent(coordinator.name)
    repository = environment.repository
    records = [
        json.loads(payload)
        for _, execution in await repository.list_procedure_executions()
        for _, payload in await repository.list_procedure_operations(
            json.loads(execution)["execution_id"]
        )
    ]
    delegated = [record for record in records if record["kind"] == "delegate"]
    assert delegated, "the parent delegated"
    work = delegated[0]["arguments"]["work"]
    return str(work["id"]), work


async def test_r01g1_a_failed_admission_leaves_nothing_that_blocks_recovery(
    tmp_path: Path,
) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    repository = environment.repository
    # The agent's row cannot be written: the crash between the two writes.
    trigger = (
        "CREATE TRIGGER fail_inspector BEFORE UPDATE ON agents "
        f"WHEN NEW.id = '{inspector.id}' BEGIN SELECT RAISE(ABORT, 'disk gone'); END"
    )
    await repository._write([(trigger, ())])  # pyright: ignore[reportPrivateUsage]
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )
    work_id, payload = await _delegate_payload(environment, coordinator)
    await asyncio.sleep(0.3)
    marker = f"delegation.accepted:{inspector.id}:{work_id}"
    assert await repository.load_state(marker) is None
    assert _children(inspector) == [], "no goal the database does not also hold"
    drop = "DROP TRIGGER fail_inspector"
    await repository._write([(drop, ())])  # pyright: ignore[reportPrivateUsage]
    await environment.stop()

    # Restart from the database, the fault gone, and the delivery replayed.
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    again = Environment(settings, {"ollama": MockProvider()})
    await again.start()
    restored = again.registry.get(inspector.id)
    await again.start_agent(restored.id, start_delay=3600)
    for _ in range(2):
        await again.bus.send(
            semantic_message(
                Performative.DELEGATE,
                sender_id=coordinator.id,
                recipient_id=restored.id,
                task_id=work_id,
                goal_id=goal.id,
                payload=payload,
            )
        )
    assert await _until(lambda: bool(_children(restored)))
    await asyncio.sleep(0.2)

    assert len(_children(restored)) == 1, "exactly one recoverable goal"
    await _run_child(again, restored, _children(restored)[0])
    assert _children(restored)[0].status is GoalStatus.DONE, _children(restored)[0].last_error
    await again.stop()


async def test_r01g2_g3_repeated_admission_reuses_the_one_goal(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )
    work_id, payload = await _delegate_payload(environment, coordinator)
    await asyncio.gather(
        *(
            environment.bus.send(
                semantic_message(
                    Performative.DELEGATE,
                    sender_id=coordinator.id,
                    recipient_id=inspector.id,
                    task_id=work_id,
                    goal_id=goal.id,
                    payload=payload,
                )
            )
            for _ in range(3)
        )
    )
    await asyncio.sleep(0.3)

    assert len(_children(inspector)) == 1
    await _run_child(environment, inspector, _children(inspector)[0])
    executions = await environment.repository.list_procedure_executions()
    children = [row for row in executions if json.loads(row[1])["agent_id"] == inspector.id]
    assert len(children) == 1, "one logical execution"
    await environment.stop()


async def test_r01g5_admission_keeps_unrelated_agent_state(tmp_path: Path) -> None:
    from evomesh.contracts import Belief

    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    inspector.mind.revise([Belief(key="weather", statement="rain")])
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )

    await _delegate_one(environment, coordinator, inspector, goal)
    await asyncio.sleep(0.1)

    stored = next(
        agent for agent in await environment.repository.load_agents() if agent.id == inspector.id
    )
    assert stored.mind.belief("weather") is not None
    assert any(item.kind == DELEGATED_GOAL_KIND for item in stored.mind.goals)
    await environment.stop()


# -- R01-H (review 5b88f57): no goal is visible before its admission commits ------


class AdmissionGate:
    """Holds the recipient's admission transaction at a controlled point, so a
    cycle can be scheduled while it is undecided."""

    def __init__(self, repository: Any) -> None:
        self.real = repository.accept_work
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        repository.accept_work = self.held

    async def held(self, *arguments: Any, **keywords: Any) -> bool:
        self.entered.set()
        await self.release.wait()
        return await self.real(*arguments, **keywords)


async def _inspector_executions(environment: Environment, inspector: AgentDefinition) -> int:
    rows = await environment.repository.list_procedure_executions()
    return sum(1 for _, payload in rows if json.loads(payload)["agent_id"] == inspector.id)


async def _stored_children(environment: Environment, inspector: AgentDefinition) -> int:
    stored = next(
        agent for agent in await environment.repository.load_agents() if agent.id == inspector.id
    )
    return sum(1 for goal in stored.mind.goals if goal.kind == DELEGATED_GOAL_KIND)


async def test_r01h1_a_cycle_during_admission_runs_nothing_uncommitted(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    gate = AdmissionGate(environment.repository)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )

    await environment.cycle_agent(coordinator.name)  # delegate
    await asyncio.wait_for(gate.entered.wait(), 5)
    # The receiver's own cycle, scheduled while its admission is undecided.
    cycle = asyncio.create_task(environment.cycle_agent(inspector.name))
    await asyncio.sleep(0.3)

    assert _children(inspector) == [], "nothing uncommitted is visible"
    assert await _inspector_executions(environment, inspector) == 0, "nor run"
    gate.release.set()
    await asyncio.wait_for(cycle, 10)
    child = _children(inspector)[0]
    await _run_child(environment, inspector, child)

    assert child.status is GoalStatus.DONE, child.last_error
    assert await _inspector_executions(environment, inspector) == 1
    assert goal.is_open or goal.status is GoalStatus.DONE
    await environment.stop()


async def test_r01h2_a_replay_during_a_cycle_leaves_no_trace(tmp_path: Path) -> None:
    environment, coordinator, inspector, provider, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )
    child = await _delegate_one(environment, coordinator, inspector, goal)
    await _run_child(environment, inspector, child)
    work_id = str(child.parameters["work_item_id"])
    assert await _settled(environment, work_id)
    row = await environment.repository.load_procedure_operation(f"delegate:{work_id}")
    assert row is not None
    payload = json.loads(row[1])["arguments"]["work"]
    inspector.mind.goals = [item for item in inspector.mind.goals if item.id != child.id]
    executions = await _inspector_executions(environment, inspector)
    calls = len(provider.calls)
    gate = AdmissionGate(environment.repository)

    await environment.bus.send(
        semantic_message(
            Performative.DELEGATE,
            sender_id=coordinator.id,
            recipient_id=inspector.id,
            task_id=work_id,
            payload=payload,
        )
    )
    await asyncio.wait_for(gate.entered.wait(), 5)
    cycle = asyncio.create_task(environment.cycle_agent(inspector.name))
    await asyncio.sleep(0.3)
    gate.release.set()
    await asyncio.wait_for(cycle, 10)
    for _ in range(2):
        await environment.cycle_agent(inspector.name)

    assert _children(inspector) == [], "no goal, not even for a moment's work"
    assert await _inspector_executions(environment, inspector) == executions
    assert len(provider.calls) == calls
    await environment.stop()


async def test_r01h3_a_failed_admission_during_a_cycle_leaks_nothing(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    repository = environment.repository
    # Only a row carrying the delegated goal fails: the cycle's own saves pass.
    trigger = (
        "CREATE TRIGGER fail_inspector BEFORE UPDATE ON agents "
        f"WHEN NEW.id = '{inspector.id}' AND NEW.definition LIKE '%{DELEGATED_GOAL_KIND}%' "
        "BEGIN SELECT RAISE(ABORT, 'disk gone'); END"
    )
    gate = AdmissionGate(repository)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )

    work_id, payload = await _delegate_payload(environment, coordinator)
    await asyncio.wait_for(gate.entered.wait(), 5)
    await repository._write([(trigger, ())])  # pyright: ignore[reportPrivateUsage]
    cycle = asyncio.create_task(environment.cycle_agent(inspector.name))
    await asyncio.sleep(0.3)
    gate.release.set()
    await asyncio.wait_for(cycle, 10)
    await asyncio.sleep(0.2)

    assert _children(inspector) == []
    assert await _inspector_executions(environment, inspector) == 0
    marker = f"delegation.accepted:{inspector.id}:{work_id}"
    assert await repository.load_state(marker) is None
    await repository._write([("DROP TRIGGER fail_inspector", ())])  # pyright: ignore[reportPrivateUsage]
    assert await _stored_children(environment, inspector) == 0, "no save leaked it"
    await environment.stop()

    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    again = Environment(settings, {"ollama": MockProvider()})
    await again.start()
    restored = again.registry.get(inspector.id)
    await again.start_agent(restored.id, start_delay=3600)
    await again.bus.send(
        semantic_message(
            Performative.DELEGATE,
            sender_id=coordinator.id,
            recipient_id=restored.id,
            task_id=work_id,
            goal_id=goal.id,
            payload=payload,
        )
    )
    assert await _until(lambda: bool(_children(restored)))
    assert len(_children(restored)) == 1
    await again.stop()


async def test_r01h4_cancelling_admission_resolves_what_was_committed(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    repository = environment.repository
    gate = AdmissionGate(repository)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )

    work_id, payload = await _delegate_payload(environment, coordinator)
    await asyncio.wait_for(gate.entered.wait(), 5)
    # Shutdown cancels the admission mid-transaction, after saving the agent.
    await environment.stop_agent(inspector.id, persist_status=False)
    assert _children(inspector) == [], "no memory-only goal"
    assert await _stored_children(environment, inspector) == 0, "the stop saved no guess"
    gate.release.set()
    marker = f"delegation.accepted:{inspector.id}:{work_id}"
    assert await _until(lambda: bool(_children(inspector))), "the committed goal is installed"
    assert await repository.load_state(marker) is not None
    assert await _stored_children(environment, inspector) == 1, "committed with its goal"
    await environment.stop()

    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    again = Environment(settings, {"ollama": MockProvider()})
    await again.start()
    restored = again.registry.get(inspector.id)
    await again.start_agent(restored.id, start_delay=3600)
    await again.bus.send(
        semantic_message(
            Performative.DELEGATE,
            sender_id=coordinator.id,
            recipient_id=restored.id,
            task_id=work_id,
            goal_id=goal.id,
            payload=payload,
        )
    )
    await asyncio.sleep(0.3)
    assert len(_children(restored)) == 1, "the committed goal, not a second task"
    await _run_child(again, restored, _children(restored)[0])
    assert _children(restored)[0].status is GoalStatus.DONE
    await again.stop()


async def test_r01h_an_acceptance_whose_goal_was_lost_is_recovered_once(tmp_path: Path) -> None:
    environment, coordinator, inspector, _, _ = await _pair(tmp_path)
    await _promote(environment, _parent("hand_off", "json_inspection"), JSON_INSPECTION)
    goal = coordinator.mind.add_goal(
        "Inspect doc.json", kind="hand_off", parameters={"path": "doc.json"}
    )
    child = await _delegate_one(environment, coordinator, inspector, goal)
    work_id = str(child.parameters["work_item_id"])
    # Committed, then lost before the child ran: an overwrite by a stale save.
    inspector.mind.goals = [item for item in inspector.mind.goals if item.id != child.id]
    await environment.repository.save_agent(inspector)
    row = await environment.repository.load_procedure_operation(f"delegate:{work_id}")
    assert row is not None
    payload = json.loads(row[1])["arguments"]["work"]

    for _ in range(2):
        await environment.bus.send(
            semantic_message(
                Performative.DELEGATE,
                sender_id=coordinator.id,
                recipient_id=inspector.id,
                task_id=work_id,
                payload=payload,
            )
        )
    assert await _until(lambda: bool(_children(inspector)))
    await asyncio.sleep(0.3)

    assert len(_children(inspector)) == 1, "recovered once, not twice"
    await _run_child(environment, inspector, _children(inspector)[0])
    assert _children(inspector)[0].status is GoalStatus.DONE
    await environment.stop()
