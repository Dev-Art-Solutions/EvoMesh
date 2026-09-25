"""Typed procedures: contract, admission and direct execution (closure plan
v2, tests T01-T09, T13-T15, T29-T30, T35, T38)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from evomesh.contracts import FilesystemGrant
from evomesh.coordination import WorkItem
from evomesh.harness_tools import ToolContext
from evomesh.permissions import FilesystemPolicy
from evomesh.procedure_runtime import (
    AdmissionStatus,
    ExecStatus,
    ProcedureExecutor,
    ProcedureRegistry,
    RegistryError,
    Selection,
    core_catalog,
)
from evomesh.procedures import (
    BindingError,
    PredicateError,
    evaluate,
    resolve,
    validate_definition,
)
from evomesh.storage import SQLiteRepository
from tests.procedure_fixtures import BRANCHING, LOCAL_JSON_SNAPSHOT, ref, snapshot

CAPS = {"artifact.read", "artifact.write"}


class Host:
    """A per-test mesh stand-in: real filesystem policy, real tool context."""

    def __init__(self, root: Path, policy: FilesystemPolicy, agent_id: str = "agent-1") -> None:
        self.root = root
        self.policy = policy
        self.agent_id = agent_id
        self.caps = set(CAPS)
        self.blackboard = None
        self.model_calls: list[str] = []
        self.replies: list[str] = []
        self.routed: list[WorkItem] = []
        self.delivered: list[str] = []
        self.assignee: str | None = "helper"

    def capabilities(self, agent_id: str) -> set[str]:
        return self.caps

    def tool_context(self, agent_id: str) -> ToolContext:
        return ToolContext(root=self.root, policy=self.policy, agent_id=agent_id, allow_write=True)

    async def think(self, agent_id: str, prompt: str, **kwargs: Any) -> str:
        self.model_calls.append(prompt)
        return self.replies.pop(0)

    async def route(self, work: WorkItem, requester_id: str) -> tuple[str | None, str]:
        self.routed.append(work)
        return self.assignee, "" if self.assignee else "no eligible peer"

    async def deliver(self, work: WorkItem, requester_id: str, assignee_id: str) -> None:
        self.delivered.append(work.id)


async def setup(
    tmp_path: Path, *, read: bool = True, write: bool = True
) -> tuple[SQLiteRepository, ProcedureRegistry, ProcedureExecutor, Host]:
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    policy = FilesystemPolicy(repository)
    root = tmp_path / "playground"
    root.mkdir()
    if read or write:
        await policy.grant(
            FilesystemGrant(agent_id="agent-1", path=str(root), read=read, write=write)
        )
    registry = ProcedureRegistry(repository, core_catalog())
    await registry.load()
    return repository, registry, ProcedureExecutor(repository, registry), Host(root, policy)


async def promote(registry: ProcedureRegistry, raw: dict[str, Any]) -> str:
    admission = await registry.register(raw, source="template", owner="tests")
    definition = registry.definitions[admission.key]
    await registry.approve(admission.key, actor="operator:test", digest=definition.digest())
    return admission.key


async def run(executor: ProcedureExecutor, execution_id: str, host: Host, limit: int = 20):
    outcome = None
    for _ in range(limit):
        outcome = await executor.advance(execution_id, host)
        if outcome.kind in {"completed", "failed", "cancelled", "needs_reconciliation"}:
            return outcome
    return outcome


# -- T01-T09: format, validation, admission -------------------------------------


async def test_t01_valid_definition_round_trips_through_the_registry(tmp_path: Path) -> None:
    repository, registry, _, _ = await setup(tmp_path)
    key = await promote(registry, LOCAL_JSON_SNAPSHOT)
    digest = registry.definitions[key].digest()

    again = ProcedureRegistry(repository, core_catalog())
    await again.load()

    assert again.definitions[key].digest() == digest
    assert again.admissions[key].status is AdmissionStatus.PROMOTED


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"schema_version": 2}, "UNSUPPORTED_SCHEMA_VERSION"),
        ({"surprise": True}, "UNKNOWN_FIELD"),
        ({"status": "promoted"}, "UNKNOWN_FIELD"),  # T08: imported admission
    ],
)
def test_t02_t08_unknown_versions_fields_and_admission_are_refused(
    change: dict[str, Any], code: str
) -> None:
    report = validate_definition(snapshot(**change), core_catalog())
    assert not report.ok and code in report.codes()


def test_t02_unknown_step_kind_and_operator() -> None:
    raw = snapshot()
    raw["steps"][0] = {"id": "x", "kind": "eval", "next": "done"}
    assert "UNKNOWN_STEP_KIND" in validate_definition(raw, core_catalog()).codes()
    branch = json.loads(json.dumps(BRANCHING))
    branch["steps"][1]["predicate"]["op"] = "regex"
    assert "UNSUPPORTED_OPERATOR" in validate_definition(branch, core_catalog()).codes()


def test_t03_graph_defects_are_refused() -> None:
    duplicate = snapshot()
    duplicate["steps"][1]["id"] = "read_source"
    assert "DUPLICATE_STEP_ID" in validate_definition(duplicate, core_catalog()).codes()
    dangling = snapshot()
    dangling["steps"][0]["next"] = "nowhere"
    assert "DANGLING_EDGE" in validate_definition(dangling, core_catalog()).codes()
    cycle = snapshot()
    cycle["steps"][2]["next"] = "read_source"
    assert "CYCLE" in validate_definition(cycle, core_catalog()).codes()
    unreachable = snapshot()
    unreachable["steps"].append({"id": "orphan", "kind": "complete", "result": {}})
    assert "UNREACHABLE_STEP" in validate_definition(unreachable, core_catalog()).codes()


def test_t04_a_result_from_only_one_branch_cannot_be_used_after_the_merge() -> None:
    raw = json.loads(json.dumps(BRANCHING))
    raw["steps"][1]["then"] = "extra"
    raw["steps"].insert(
        2,
        {
            "id": "extra",
            "kind": "tool",
            "adapter": "core.json_read",
            "contract_version": 1,
            "arguments": {"path": ref("goal", "parameters", "source")},
            "next": "merged",
        },
    )
    raw["steps"][1]["else"] = "merged"
    raw["steps"] = [step for step in raw["steps"] if step["id"] not in {"healthy", "unhealthy"}]
    raw["steps"].append(
        {"id": "merged", "kind": "complete", "result": {"route": ref("result", "extra", "path")}}
    )
    assert "UNAVAILABLE_RESULT" in validate_definition(raw, core_catalog()).codes()


def test_t05_bindings_preserve_types_and_refuse_missing_values() -> None:
    scopes = {"goal": {"parameters": {"flag": False, "count": 0, "empty": None}}}
    assert resolve(ref("goal", "parameters", "flag"), scopes) is False
    assert resolve(ref("goal", "parameters", "empty"), scopes) is None
    with pytest.raises(BindingError) as missing:
        resolve(ref("goal", "parameters", "absent"), scopes)
    assert missing.value.code == "MISSING_BINDING"
    assert evaluate({"op": "eq", "left": ref("goal", "parameters", "flag"), "right": False}, scopes)
    with pytest.raises(PredicateError):  # "false" is not false; bool is not int
        evaluate({"op": "eq", "left": ref("goal", "parameters", "flag"), "right": "false"}, scopes)
    with pytest.raises(PredicateError):
        evaluate({"op": "eq", "left": ref("goal", "parameters", "count"), "right": False}, scopes)
    assert evaluate({"op": "exists", "value": ref("goal", "parameters", "empty")}, scopes) is True


def test_t06_unknown_predicates_never_pass() -> None:
    scopes: dict[str, Any] = {"result": {}}
    missing = {"op": "eq", "left": ref("result", "x", "status"), "right": "ok"}
    assert evaluate(missing, scopes) is None
    assert evaluate({"op": "not", "item": missing}, scopes) is None
    assert (
        evaluate({"op": "all", "items": [missing, {"op": "eq", "left": 1, "right": 1}]}, scopes)
        is None
    )
    raw = json.loads(json.dumps(BRANCHING))
    raw["steps"][1]["predicate"] = {"op": "all", "items": []}
    assert "EMPTY_COMPOSITE" in validate_definition(raw, core_catalog()).codes()


def test_t07_template_looking_text_is_data() -> None:
    scopes = {"goal": {"parameters": {"x": "{{__import__('os')}}"}}}
    assert resolve({"literal": {"ref": "not a ref"}}, scopes) == {"ref": "not a ref"}
    assert resolve("{{goal.parameters.x}}", scopes) == "{{goal.parameters.x}}"


async def test_t08_only_a_trusted_approval_promotes(tmp_path: Path) -> None:
    _, registry, _, _ = await setup(tmp_path)
    admission = await registry.register(snapshot(), source="learned")
    assert admission.status is AdmissionStatus.CANDIDATE
    with pytest.raises(RegistryError):
        await registry.approve(admission.key, actor="operator:x", digest=admission.digest)
    await registry.mark_validated(admission.key, "replayed on fixture")
    with pytest.raises(RegistryError, match="UNTRUSTED"):
        await registry.approve(admission.key, actor="model", digest=admission.digest)
    with pytest.raises(RegistryError, match="DIGEST"):
        await registry.approve(admission.key, actor="operator:x", digest="0" * 64)
    promoted = await registry.approve(admission.key, actor="operator:x", digest=admission.digest)
    assert promoted.status is AdmissionStatus.PROMOTED


async def test_t09_a_revision_is_immutable_and_executions_stay_pinned(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    key = await promote(registry, LOCAL_JSON_SNAPSHOT)
    match = registry.select(
        "local_json_snapshot", {"source": "a.json", "destination": "b.json"}, CAPS
    )
    assert match.definition is not None and match.admission is not None
    execution = await executor.start(
        match.definition,
        match.admission,
        agent_id="agent-1",
        goal_id="g",
        occurrence_id="g#0",
        parameters={"source": "a.json", "destination": "b.json"},
    )
    changed = snapshot(name="A different body at the same revision")
    with pytest.raises(RegistryError, match="REVISION_CONFLICT"):
        await registry.register(changed)
    assert execution.digest == registry.definitions[key].digest()


# -- selection --------------------------------------------------------------------


async def test_selection_explains_every_refusal(tmp_path: Path) -> None:
    _, registry, _, _ = await setup(tmp_path)
    assert registry.select("local_json_snapshot", {}, CAPS).selection is Selection.NO_MATCH
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    bad = registry.select("local_json_snapshot", {"source": 3}, CAPS)
    assert bad.selection is Selection.INCOMPATIBLE_PROCEDURE and "parameters" in bad.reasons[0]
    weak = registry.select(
        "local_json_snapshot", {"source": "a", "destination": "b"}, {"artifact.read"}
    )
    assert weak.selection is Selection.PERMISSION_DENIED
    ok = registry.select("local_json_snapshot", {"source": "a", "destination": "b"}, CAPS)
    assert ok.selection is Selection.MATCH


# -- W1 direct execution ------------------------------------------------------------


async def start_w1(
    registry: ProcedureRegistry, executor: ProcedureExecutor, occurrence: str = "g#0"
):
    match = registry.select(
        "local_json_snapshot", {"source": "in.json", "destination": "out/snap.json"}, CAPS
    )
    assert match.definition is not None and match.admission is not None
    return await executor.start(
        match.definition,
        match.admission,
        agent_id="agent-1",
        goal_id="g",
        occurrence_id=occurrence,
        parameters={"source": "in.json", "destination": "out/snap.json"},
    )


async def test_w1_runs_real_adapters_with_zero_model_calls(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"b": 2, "a": [1, true, null]}', encoding="utf-8")
    execution = await start_w1(registry, executor)

    outcome = await run(executor, execution.execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    written = (host.root / "out" / "snap.json").read_text(encoding="utf-8")
    assert json.loads(written) == {"a": [1, True, None], "b": 2}
    assert host.model_calls == []
    _, done = await executor.load(execution.execution_id)
    assert any(
        item.get("check") == "artifact_matches_source" and item["passed"] for item in done.evidence
    )
    assert done.budget.model_calls == 0


async def test_t13_a_stale_destination_is_a_conflict_not_evidence(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    (host.root / "out").mkdir()
    (host.root / "out" / "snap.json").write_text('{"a":1}\n', encoding="utf-8")  # same content
    execution = await start_w1(registry, executor)

    outcome = await run(executor, execution.execution_id, host)

    assert (
        outcome is not None and outcome.kind == "failed" and outcome.code == "DESTINATION_CONFLICT"
    )


async def test_t29_t30_the_direct_path_keeps_the_agents_own_policy(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path, write=False)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)

    outcome = await run(executor, execution.execution_id, host)

    assert outcome is not None and outcome.kind == "failed" and outcome.code == "PERMISSION_DENIED"
    assert not (host.root / "out" / "snap.json").exists()


async def test_t30_a_capability_lost_mid_run_blocks_the_next_dispatch(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)
    first = await executor.advance(execution.execution_id, host)
    assert first.kind == "advanced"
    host.caps.discard("artifact.write")

    second = await executor.advance(execution.execution_id, host)

    assert second.kind == "failed" and second.code == "PERMISSION_DENIED"


async def test_t14_a_schema_invalid_source_stops_before_any_write(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text("[1, 2]", encoding="utf-8")  # not an object
    execution = await start_w1(registry, executor)

    outcome = await run(executor, execution.execution_id, host)

    assert outcome is not None and outcome.kind == "failed" and outcome.code == "INVALID_JSON"
    assert not (host.root / "out").exists()


async def test_t35_concurrent_advances_dispatch_once(tmp_path: Path) -> None:
    repository, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)
    await executor.advance(execution.execution_id, host)  # read done
    other = ProcedureExecutor(repository, registry)  # a second advancer

    results = await asyncio.gather(
        executor.advance(execution.execution_id, host), other.advance(execution.execution_id, host)
    )

    kinds = sorted(result.kind for result in results)
    assert kinds.count("advanced") == 1
    receipts = list((host.root / ".evomesh-receipts").glob("*.json"))
    assert len(receipts) == 1


async def test_t38_same_operation_with_changed_arguments_is_a_conflict(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await start_w1(registry, executor)
    await executor.advance(execution.execution_id, host)
    # Crash right after claiming the write, then the source changes.
    executor.fault = lambda point, key: (
        (_ for _ in ()).throw(SystemExit()) if point == "after_claim" else None
    )
    with pytest.raises(SystemExit):
        await executor.advance(execution.execution_id, host)
    version, current = await executor.load(execution.execution_id)
    current.results["read_source"]["value"] = {"a": 2}
    await executor._commit(version, current)  # pyright: ignore[reportPrivateUsage]
    fresh = ProcedureExecutor(executor.repository, registry)
    outcome = await run(fresh, execution.execution_id, host)
    assert outcome is not None and outcome.kind == "failed"


async def test_branch_takes_explicit_edges_and_unknown_fails(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, BRANCHING)
    routes = {}
    for index, body in enumerate(['{"status": "healthy"}', '{"status": "down"}', '{"other": 1}']):
        (host.root / f"s{index}.json").write_text(body, encoding="utf-8")
        match = registry.select("branch_fixture", {"source": f"s{index}.json"}, CAPS)
        assert match.definition is not None and match.admission is not None
        execution = await executor.start(
            match.definition,
            match.admission,
            agent_id="agent-1",
            goal_id="g",
            occurrence_id=f"g#{index}",
            parameters={"source": f"s{index}.json"},
        )
        outcome = await run(executor, execution.execution_id, host)
        assert outcome is not None
        routes[index] = (outcome.kind, outcome.code, outcome.result)
    assert routes[0] == ("completed", "", {"route": "healthy"})
    assert routes[1] == ("completed", "", {"route": "unhealthy"})
    assert routes[2][0] == "failed" and routes[2][1] == "PREDICATE_UNRESOLVED"
    assert host.model_calls == []


async def test_executions_and_status_survive_a_second_open(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    first = await start_w1(registry, executor)
    second = await start_w1(registry, executor)
    assert first.execution_id == second.execution_id, "resume before reselect"
    cancelled = await executor.request_cancel(first.execution_id, "operator")
    assert cancelled.status is ExecStatus.CANCELLED
