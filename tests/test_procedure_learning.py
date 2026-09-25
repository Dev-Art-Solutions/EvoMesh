"""Conservative typed learning (closure plan v2 AC-14 to AC-17): traces from
real contract-backed operations, candidates only through an approved binding
map, manual promotion after a held-out replay, degradation, rollout and
migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from evomesh.config import HarnessSettings, ProcedureSettings
from evomesh.contracts import (
    AgentDefinition,
    AgentStatus,
    FilesystemGrant,
    Goal,
    GoalCondition,
    GoalConditionKind,
    GoalStatus,
)
from evomesh.environment import Environment
from evomesh.harness_queue import HarnessJob
from evomesh.harness_tools import ToolContext
from evomesh.models import ChatTurn, MockProvider, ToolCall
from evomesh.permissions import FilesystemPolicy
from evomesh.procedure_runtime import (
    AdmissionStatus,
    ProcedureRegistry,
    RegistryError,
    Selection,
    core_catalog,
)
from evomesh.procedure_traces import (
    BindingMap,
    OperationTrace,
    TraceRecorder,
    extract_candidate,
    replay,
    typed_harness_tools,
    validate_candidate,
)
from evomesh.storage import MIGRATIONS, SQLiteRepository
from tests.test_bdi import settings_for

COPY_MAP = BindingMap(
    goal_kind="copy_json",
    approved_by="operator:iliya",
    bindings={
        "0.path": {"param": "source"},
        "1.path": {"param": "destination"},
        "1.value": {"result": [0, "value"]},
    },
)


def _goal(index: int, **parameters: Any) -> Goal:
    return Goal(
        id=f"g{index}",
        description="copy",
        kind="copy_json",
        parameters=parameters or {"source": f"in{index}.json", "destination": f"out{index}.json"},
    )


async def _recorded(
    tmp_path: Path, count: int = 3, *, basis: tuple[str, ...] = ("artifact_exists:out",)
) -> tuple[TraceRecorder, list[OperationTrace]]:
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    policy = FilesystemPolicy(repository)
    root = tmp_path / "root"
    root.mkdir()
    await policy.grant(FilesystemGrant(agent_id="a", path=str(root), write=True))
    recorder = TraceRecorder(repository)
    traces: list[OperationTrace] = []
    for index in range(count):
        goal = _goal(index)
        (root / f"in{index}.json").write_text(json.dumps({"n": index}), encoding="utf-8")
        tools = {
            tool.name: tool
            for tool in typed_harness_tools(
                recorder, "a", lambda goal=goal: (goal.id, f"{goal.id}#0"), allow_write=True
            )
        }
        context = ToolContext(root=root, policy=policy, agent_id="a", allow_write=True)
        read = json.loads(await tools["json_read"].run(context, {"path": f"in{index}.json"}))
        await tools["json_write"].run(
            context, {"path": f"out{index}.json", "value": read["value"]}
        )
        traces += await recorder.finalize(goal, basis=list(basis), model_calls=3)
    return recorder, traces


# -- AC-14: traces from real operations ---------------------------------------------


async def test_a_trace_holds_the_actual_contract_operations(tmp_path: Path) -> None:
    _, traces = await _recorded(tmp_path, count=1)

    trace = traces[0]
    assert trace.eligible, trace.ineligible
    assert [(s.adapter, s.contract_version) for s in trace.steps] == [
        ("core.json_read", 1),
        ("core.json_write", 1),
    ]
    assert trace.steps[1].arguments == {"path": "out0.json", "value": {"n": 0}}
    assert trace.steps[1].result is not None and trace.steps[1].result["artifact_id"] == "out0.json"
    assert trace.model_calls == 3 and trace.completion_basis == ["artifact_exists:out"]


async def test_self_reported_completion_is_not_evidence(tmp_path: Path) -> None:
    _, traces = await _recorded(tmp_path, count=1, basis=())

    assert not traces[0].eligible
    assert "no authoritative completion evidence" in traces[0].ineligible


async def test_a_failed_operation_makes_the_trace_ineligible(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    policy = FilesystemPolicy(repository)
    root = tmp_path / "root"
    root.mkdir()
    await policy.grant(FilesystemGrant(agent_id="a", path=str(root)))
    recorder = TraceRecorder(repository)
    goal = _goal(0)
    (tool,) = typed_harness_tools(recorder, "a", lambda: (goal.id, "g0#0"), allow_write=False)
    context = ToolContext(root=root, policy=policy, agent_id="a")

    reply = await tool.run(context, {"path": "missing.json"})
    traces = await recorder.finalize(goal, basis=["x"], model_calls=1)

    assert reply.startswith("DENIED: NOT_FOUND")
    assert not traces[0].eligible


async def test_the_same_occurrence_is_recorded_once(tmp_path: Path) -> None:
    recorder, traces = await _recorded(tmp_path, count=1)
    again = traces[0].model_copy(update={"trace_id": "other"})

    stored = await recorder.repository.insert_procedure_trace(
        again.trace_id, again.goal_kind, again.occurrence_id, again.model_dump_json()
    )

    assert not stored
    assert len(await recorder.traces("copy_json")) == 1


# -- AC-15: conservative candidate extraction ----------------------------------------


async def test_three_verified_occurrences_and_an_approved_map_yield_a_candidate(
    tmp_path: Path,
) -> None:
    _, traces = await _recorded(tmp_path)

    report = extract_candidate(traces, COPY_MAP, core_catalog())

    assert report.ok, report.reasons
    assert report.definition is not None and not report.exact_scope
    kinds = [step["kind"] for step in report.definition["steps"]]
    assert kinds == ["tool", "tool", "validate", "complete"]
    assert report.definition["steps"][2]["check"] == "artifact_matches_source"
    assert report.definition["required_capabilities"] == ["artifact.read", "artifact.write"]
    assert report.expected_cost == {"model_calls_observed": 3.0, "model_calls_candidate": 0.0}


async def test_a_replayed_trace_does_not_count_twice(tmp_path: Path) -> None:
    _, traces = await _recorded(tmp_path, count=2)

    report = extract_candidate([*traces, traces[0]], COPY_MAP, core_catalog())

    assert report.code == "INSUFFICIENT_EVIDENCE"


async def test_equal_values_are_not_lineage(tmp_path: Path) -> None:
    _, traces = await _recorded(tmp_path)

    report = extract_candidate(traces, None, core_catalog())

    assert report.code == "NOT_COMPILABLE"
    assert any("no recorded provenance" in reason for reason in report.reasons)


async def test_a_map_that_disagrees_with_the_traces_is_refused(tmp_path: Path) -> None:
    _, traces = await _recorded(tmp_path)
    wrong = COPY_MAP.model_copy(
        update={"bindings": {**COPY_MAP.bindings, "1.path": {"param": "source"}}}
    )

    report = extract_candidate(traces, wrong, core_catalog())

    assert report.code == "NOT_COMPILABLE"
    assert any("disagrees" in reason for reason in report.reasons)


async def test_only_an_operator_can_approve_a_binding_map(tmp_path: Path) -> None:
    _, traces = await _recorded(tmp_path)

    report = extract_candidate(
        traces, COPY_MAP.model_copy(update={"approved_by": "agent:copier"}), core_catalog()
    )

    assert report.code == "NOT_COMPILABLE"


async def test_a_constant_unbound_value_becomes_an_exact_scope_literal(tmp_path: Path) -> None:
    _, traces = await _recorded(tmp_path)
    partial = COPY_MAP.model_copy(
        update={"bindings": {"0.path": {"param": "source"}, "1.value": {"result": [0, "value"]}}}
    )
    for trace in traces:  # every occurrence wrote to the same place
        trace.steps[1].arguments = {**(trace.steps[1].arguments or {}), "path": "same.json"}

    report = extract_candidate(traces, partial, core_catalog())

    assert report.ok and report.exact_scope
    assert report.restrictions == ["1.path is fixed to the observed value"]


# -- AC-14 to AC-16 through the live mesh --------------------------------------------


def _copy_turns(source: str, destination: str, value: dict[str, Any]) -> list[ChatTurn]:
    return [
        ChatTurn(tool_calls=[ToolCall(name="json_read", arguments={"path": source})]),
        ChatTurn(
            tool_calls=[
                ToolCall(name="json_write", arguments={"path": destination, "value": value})
            ]
        ),
        ChatTurn(text=f"Copied {source} to {destination}."),
    ]


async def _copier(
    tmp_path: Path, provider: MockProvider, procedures: ProcedureSettings | None = None
) -> tuple[Environment, AgentDefinition, Path]:
    settings = settings_for(tmp_path)
    settings.harness = HarnessSettings(enabled=True, allow_write=True)
    if procedures is not None:
        settings.procedures = procedures
    environment = Environment(settings, {"ollama": provider})
    await environment.start()
    work = tmp_path / "work"
    work.mkdir()
    agent = AgentDefinition(
        name="Copier",
        purpose="Copies JSON records",
        status=AgentStatus.ACTIVE,
        capabilities=["artifact.read", "artifact.write"],
        harness_root=str(work),
    )
    await environment.register_agent(agent)
    await environment.grant_access(
        FilesystemGrant(agent_id=agent.id, path=str(work), read=True, write=True)
    )
    await environment.start_agent(agent.id, start_delay=3600)
    return environment, agent, work


def _copy_goal(agent: AgentDefinition, index: int) -> Goal:
    destination = f"out/copy{index}.json"
    return agent.mind.add_goal(
        f"Copy record {index}",
        kind="copy_json",
        parameters={"source": f"in{index}.json", "destination": destination},
        success_conditions=[
            GoalCondition(kind=GoalConditionKind.ARTIFACT_EXISTS, path=destination)
        ],
    )


async def _legacy_occurrence(
    environment: Environment, agent: AgentDefinition, work: Path, index: int
) -> Goal:
    """One model-directed occurrence: the agent's harness job, steered by the
    scripted model, copies the record with the contract-backed tools."""
    value = {"record": index, "tags": ["x"] * index}
    (work / f"in{index}.json").write_text(json.dumps(value), encoding="utf-8")
    goal = _copy_goal(agent, index)
    agent.mind.commit(goal.id, [f"copy in{index}.json"], plan="model")
    provider = environment.providers["ollama"]
    assert isinstance(provider, MockProvider)
    provider.turns = _copy_turns(f"in{index}.json", f"out/copy{index}.json", value)
    job = HarnessJob(
        number=100 + index, objective="copy the record", root=work, agent_id=agent.id,
        allow_write=True, notify=False,
    )
    await environment._run_harness_job(job)  # pyright: ignore[reportPrivateUsage]
    await environment.cycle_agent(agent.name)  # its success condition now holds
    assert goal.status is GoalStatus.DONE, goal.last_error
    return goal


async def test_a_model_directed_workload_becomes_a_promoted_zero_call_procedure(
    tmp_path: Path,
) -> None:
    provider = MockProvider(turns=[ChatTurn(text="idle")])
    environment, agent, work = await _copier(tmp_path, provider)
    for index in range(3):
        await _legacy_occurrence(environment, agent, work, index)
    traces = await environment.procedure_learning.recorder.traces("copy_json")
    assert len(traces) == 3 and all(trace.eligible for trace in traces)
    assert all(trace.model_calls >= 3 for trace in traces), "the legacy cost was measured"

    # AC-15: compile, then register as a candidate. It cannot run yet.
    report = extract_candidate(traces, COPY_MAP, environment.procedures.registry.catalog)
    assert report.ok and report.definition is not None, report.reasons
    registry = environment.procedures.registry
    admission = await registry.register(report.definition, source="learned", owner="learning")
    key = admission.key
    assert admission.status is AdmissionStatus.CANDIDATE
    selection = registry.select(
        "copy_json", {"source": "a", "destination": "b"}, {"artifact.read", "artifact.write"}
    )
    assert selection.selection is Selection.NO_MATCH, "a candidate is never selected"
    digest = registry.definitions[key].digest()
    with pytest.raises(RegistryError):
        await registry.approve(key, actor="operator:iliya", digest=digest)

    # AC-16: a held-out replay in a sandbox, then a trusted approval.
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "held.json").write_text(json.dumps({"record": "held-out"}), encoding="utf-8")
    result = await replay(
        registry,
        environment.procedures.executor,
        environment.permissions,
        key,
        fixture=fixture,
        parameters={"source": "held.json", "destination": "copy.json"},
        training=traces,
    )
    assert result.passed() and result.held_out, result
    await validate_candidate(registry, key, [result])
    with pytest.raises(RegistryError):
        await registry.approve(key, actor="agent:copier", digest=digest)
    await registry.approve(key, actor="operator:iliya", digest=digest)

    # The fourth occurrence runs typed: same authoritative outcome, no model.
    (work / "in9.json").write_text(json.dumps({"record": 9, "fresh": True}), encoding="utf-8")
    goal = _copy_goal(agent, 9)
    calls_before = len(environment.cognition.metrics.records)
    for _ in range(8):
        await environment.cycle_agent(agent.name)
        if goal.status is GoalStatus.DONE:
            break

    assert goal.status is GoalStatus.DONE, goal.last_error
    assert json.loads((work / "out" / "copy9.json").read_text(encoding="utf-8")) == {
        "record": 9,
        "fresh": True,
    }
    assert len(environment.cognition.metrics.records) == calls_before, "0 calls, down from 3+"
    execution = await environment.procedures.executor.for_occurrence(f"{goal.id}#0")
    assert execution is not None and execution.path == "typed_learned"
    await environment.stop()


async def test_a_validation_regression_degrades_the_procedure(tmp_path: Path) -> None:
    from tests.procedure_fixtures import LOCAL_JSON_SNAPSHOT

    environment, agent, work = await _copier(tmp_path, MockProvider(turns=[ChatTurn(text="ok")]))
    registry = environment.procedures.registry
    admission = await registry.register(LOCAL_JSON_SNAPSHOT, source="template", owner="tests")
    key = admission.key
    await registry.approve(key, actor="operator:t", digest=registry.definitions[key].digest())
    (work / "source.json").write_text('{"a": 1}', encoding="utf-8")

    def tamper(point: str, operation_key: str) -> None:
        if point == "after_effect" and "write_snapshot" in operation_key:
            (work / "out" / "snap.json").write_text('{"a": 2}\n', encoding="utf-8")

    environment.procedures.executor.fault = tamper
    goal = agent.mind.add_goal(
        "Snapshot",
        kind="local_json_snapshot",
        parameters={"source": "source.json", "destination": "out/snap.json"},
    )
    for _ in range(6):
        await environment.cycle_agent(agent.name)
        if not goal.is_open:
            break

    assert goal.status is GoalStatus.FAILED
    assert registry.admissions[key].status is AdmissionStatus.DEGRADED
    selection = registry.select(
        "local_json_snapshot",
        {"source": "s", "destination": "d"},
        {"artifact.read", "artifact.write"},
    )
    assert selection.selection is Selection.NO_MATCH, "a degraded revision is not selected"
    await environment.stop()


# -- AC-17: rollout controls and migration -------------------------------------------


async def test_emergency_disable_stops_new_typed_runs_but_settles_open_ones(
    tmp_path: Path,
) -> None:
    from tests.procedure_fixtures import LOCAL_JSON_SNAPSHOT

    provider = MockProvider(turns=[ChatTurn(text="ok")])
    environment, agent, work = await _copier(tmp_path, provider)
    registry = environment.procedures.registry
    admission = await registry.register(LOCAL_JSON_SNAPSHOT, source="template", owner="tests")
    await registry.approve(
        admission.key, actor="operator:t", digest=registry.definitions[admission.key].digest()
    )
    (work / "source.json").write_text('{"a": 1}', encoding="utf-8")
    running = agent.mind.add_goal(
        "Snapshot",
        kind="local_json_snapshot",
        parameters={"source": "source.json", "destination": "out/snap.json"},
        priority=1,
    )
    await environment.cycle_agent(agent.name)  # the read, before the switch

    registry.enabled = False
    for _ in range(6):
        await environment.cycle_agent(agent.name)
        if not running.is_open:
            break
    fresh = registry.select(
        "local_json_snapshot",
        {"source": "source.json", "destination": "out/other.json"},
        {"artifact.read", "artifact.write"},
    )

    assert running.status is GoalStatus.DONE, "the open execution settled"
    assert (work / "out" / "snap.json").exists()
    assert fresh.selection is Selection.NO_MATCH, "nothing new is admitted"
    await environment.stop()


async def test_the_setting_disables_typed_admission(tmp_path: Path) -> None:
    environment, _, _ = await _copier(
        tmp_path, MockProvider(), procedures=ProcedureSettings(enabled=False)
    )

    assert environment.procedures.registry.enabled is False
    await environment.stop()


async def test_an_old_database_migrates_idempotently_and_keeps_its_data(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:  # a database from before typed procedures
        db.executescript(MIGRATIONS[0])
        agent = AgentDefinition(name="Veteran", purpose="Was here first")
        db.execute(
            "INSERT INTO agents(id, definition) VALUES (?, ?)",
            (agent.id, agent.model_dump_json()),
        )

    repository = SQLiteRepository(path)
    await repository.initialize()
    await SQLiteRepository(path).initialize()  # a restart migrates again, harmlessly

    with sqlite3.connect(path) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_version")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert versions == [1, 2]
    assert {"procedure_definitions", "procedure_executions", "procedure_traces"} <= tables
    assert [item.name for item in await repository.load_agents()] == ["Veteran"]


async def test_a_newer_or_corrupt_definition_is_quarantined_not_dropped(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    future = {"schema_version": 99, "procedure_id": "future", "revision": 1}
    await repository.insert_procedure_definition("future", 1, "d" * 64, json.dumps(future))
    registry = ProcedureRegistry(repository, core_catalog())

    await registry.load()

    assert registry.admissions["future@1"].status is AdmissionStatus.INVALID
    assert "future@1" not in registry.definitions
    assert len(await repository.load_procedure_definitions()) == 1, "kept for inspection"
