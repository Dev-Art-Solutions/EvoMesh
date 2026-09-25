"""Typed procedures: waits, delegation contracts, cancellation and pause
(closure plan v2 AC-10 to AC-12; T16-T24, T31-T34)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from evomesh.blackboard import Blackboard, WorldFact
from evomesh.contracts import now_utc
from evomesh.coordination import WorkItem
from evomesh.procedure_runtime import (
    ExecStatus,
    ProcedureExecutor,
    ProcedureRegistry,
    core_catalog,
)
from evomesh.procedures import validate_definition
from tests.procedure_fixtures import (
    BRANCHING,
    DELEGATED_INSPECTION,
    LOCAL_JSON_SNAPSHOT,
    evidence_wait,
    ref,
)
from tests.test_procedures import Host, promote, run, setup


class Clock:
    def __init__(self) -> None:
        self.now = now_utc()

    def __call__(self):  # type: ignore[no-untyped-def]
        return self.now


async def _start(
    registry: ProcedureRegistry,
    executor: ProcedureExecutor,
    kind: str,
    parameters: dict[str, Any],
    capabilities: set[str],
    occurrence: str = "g#0",
):
    match = registry.select(kind, parameters, capabilities)
    assert match.definition is not None and match.admission is not None, match.reasons
    return await executor.start(
        match.definition,
        match.admission,
        agent_id="agent-1",
        goal_id="g",
        occurrence_id=occurrence,
        parameters=parameters,
    )


# -- AC-10: branches and durable waits ------------------------------------------


def _merge(done_result: dict[str, Any]) -> dict[str, Any]:
    """read -> branch -> (project | straight) -> one merged completion."""
    definition = dict(BRANCHING)
    definition["steps"] = [
        BRANCHING["steps"][0],
        {**BRANCHING["steps"][1], "then": "project", "else": "done"},
        {
            "id": "project",
            "kind": "tool",
            "adapter": "core.json_project",
            "contract_version": 1,
            "arguments": {
                "value": ref("result", "read", "value"),
                "fields": {"literal": ["status"]},
            },
            "next": "done",
        },
        {"id": "done", "kind": "complete", "result": done_result},
    ]
    return definition


def test_merge_after_a_branch_cannot_use_a_one_sided_result() -> None:
    one_sided = _merge({"route": ref("result", "project", "value", "status")})
    both_sides = _merge({"route": ref("result", "read", "value", "status")})

    rejected = validate_definition(one_sided, core_catalog())
    accepted = validate_definition(both_sides, core_catalog())

    assert "UNAVAILABLE_RESULT" in rejected.codes()
    assert accepted.ok, accepted.issues


async def test_evidence_published_before_the_wait_begins_is_seen(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    host.blackboard = board = Blackboard()
    await promote(registry, evidence_wait())
    execution = await _start(registry, executor, "evidence_wait", {"key": "k"}, set())
    board.publish_fact(WorldFact(key="k", value="ready", source="peer"))

    outcome = await run(executor, execution.execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    assert outcome.result == {"value": "ready"}
    assert host.model_calls == []


async def test_evidence_from_before_this_execution_does_not_satisfy_the_wait(
    tmp_path: Path,
) -> None:
    _, registry, executor, host = await setup(tmp_path)
    host.blackboard = board = Blackboard()
    board.publish_fact(
        WorldFact(key="k", value="stale", source="peer", created_at=now_utc() - timedelta(hours=1))
    )
    await promote(registry, evidence_wait())
    execution = await _start(registry, executor, "evidence_wait", {"key": "k"}, set())

    outcome = await executor.advance(execution.execution_id, host)

    assert outcome.kind == "waiting" and outcome.code == "WAITING_EVIDENCE"


async def test_a_wait_rechecks_durable_state_without_any_event(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    host.blackboard = board = Blackboard()
    await promote(registry, evidence_wait())
    execution = await _start(registry, executor, "evidence_wait", {"key": "k"}, set())
    for _ in range(3):
        assert (await executor.advance(execution.execution_id, host)).kind == "waiting"
    # No notification of any kind: the next advance simply finds it.
    board.publish_fact(WorldFact(key="k", value="late", source="peer"))

    outcome = await run(executor, execution.execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    assert host.model_calls == [], "waiting never polls a model"


async def test_a_wait_times_out_explicitly(tmp_path: Path) -> None:
    repository, registry, _, host = await setup(tmp_path)
    clock = Clock()
    executor = ProcedureExecutor(repository, registry, clock=clock)
    host.blackboard = Blackboard()
    await promote(registry, evidence_wait(timeout=30))
    execution = await _start(registry, executor, "evidence_wait", {"key": "k"}, set())
    assert (await executor.advance(execution.execution_id, host)).kind == "waiting"
    clock.now += timedelta(seconds=31)

    outcome = await executor.advance(execution.execution_id, host)

    assert outcome.kind == "failed" and outcome.code == "AWAIT_TIMEOUT"


async def test_waiting_does_not_spend_step_attempts(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    host.blackboard = Blackboard()
    await promote(registry, evidence_wait())
    execution = await _start(registry, executor, "evidence_wait", {"key": "k"}, set())
    for _ in range(50):
        await executor.advance(execution.execution_id, host)

    _, current = await executor.load(execution.execution_id)

    assert current.status is ExecStatus.WAITING
    assert current.budget.step_attempts < current.budget.max_step_attempts


# -- AC-11: structured delegation -----------------------------------------------

DELEGATOR = {"work.delegate"}


async def _delegation(tmp_path: Path) -> tuple[ProcedureExecutor, Host, str, str]:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, DELEGATED_INSPECTION)
    host.caps = set(DELEGATOR)
    execution = await _start(
        registry, executor, "delegated_inspection", {"path": "doc.json"}, DELEGATOR
    )
    first = await executor.advance(execution.execution_id, host)
    assert first.kind == "advanced", first
    work_id = str((first.result or {})["work_item_id"])
    return executor, host, execution.execution_id, work_id


GOOD_CHILD = {"artifact_id": "doc.json", "keys": ["a"], "digest": "d" * 64}


async def test_one_child_is_created_and_the_parent_resumes_on_its_result(
    tmp_path: Path,
) -> None:
    executor, host, execution_id, work_id = await _delegation(tmp_path)
    assert host.delivered == [work_id]
    assert (await executor.advance(execution_id, host)).code == "WAITING_FOR_CHILD"

    assert await executor.settle_child(
        work_id, status="completed", executor_id="helper", result=GOOD_CHILD
    )
    outcome = await run(executor, execution_id, host)

    assert outcome is not None and outcome.kind == "completed"
    assert outcome.result == {"keys": ["a"], "digest": "d" * 64}
    assert len(host.routed) == 1 and host.model_calls == []
    routed = host.routed[0]
    assert routed.causation_chain == ["agent-1"] and routed.delegation_depth == 1


async def test_a_lost_delivery_is_redelivered_not_duplicated(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, DELEGATED_INSPECTION)
    host.caps = set(DELEGATOR)
    execution = await _start(
        registry, executor, "delegated_inspection", {"path": "doc.json"}, DELEGATOR
    )
    calls = {"n": 0}
    original = host.deliver

    async def flaky(work: WorkItem, requester_id: str, assignee_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("bus down")
        await original(work, requester_id, assignee_id)

    host.deliver = flaky  # type: ignore[method-assign]
    try:
        await executor.advance(execution.execution_id, host)
    except ConnectionError:
        pass  # the process "crashed" after the child was committed

    await executor.advance(execution.execution_id, host)

    assert len(host.routed) == 1, "the child was created once"
    assert len(host.delivered) == 1, "and delivered once, after the failure"


async def test_a_result_from_the_wrong_executor_is_refused(tmp_path: Path) -> None:
    executor, host, execution_id, work_id = await _delegation(tmp_path)

    assert not await executor.settle_child(
        work_id, status="completed", executor_id="impostor", result=GOOD_CHILD
    )
    assert (await executor.advance(execution_id, host)).code == "WAITING_FOR_CHILD"


async def test_a_second_settlement_is_ignored(tmp_path: Path) -> None:
    executor, _, _, work_id = await _delegation(tmp_path)
    assert await executor.settle_child(
        work_id, status="completed", executor_id="helper", result=GOOD_CHILD
    )
    assert not await executor.settle_child(
        work_id, status="failed", executor_id="helper", result="late"
    )


async def test_a_failed_or_cancelled_child_is_not_success(tmp_path: Path) -> None:
    for status in ("failed", "cancelled"):
        executor, host, execution_id, work_id = await _delegation(tmp_path / status)
        await executor.settle_child(work_id, status=status, executor_id="helper")

        outcome = await run(executor, execution_id, host)

        assert outcome is not None and outcome.kind == "failed"
        assert outcome.code == "CHILD_NOT_COMPLETED"


async def test_a_child_result_outside_its_contract_fails_the_parent(tmp_path: Path) -> None:
    executor, host, execution_id, work_id = await _delegation(tmp_path)
    await executor.settle_child(
        work_id, status="completed", executor_id="helper", result={"keys": "not a list"}
    )

    outcome = await run(executor, execution_id, host)

    assert outcome is not None and outcome.code == "CHILD_RESULT_INVALID"


async def test_no_eligible_peer_fails_without_a_child(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, DELEGATED_INSPECTION)
    host.caps = set(DELEGATOR)
    host.assignee = None
    execution = await _start(
        registry, executor, "delegated_inspection", {"path": "doc.json"}, DELEGATOR
    )

    outcome = await executor.advance(execution.execution_id, host)

    assert outcome.kind == "failed" and outcome.code == "NO_ELIGIBLE_PEER"
    assert host.delivered == []


async def test_delegation_depth_is_bounded(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, DELEGATED_INSPECTION)
    host.caps = set(DELEGATOR)
    match = registry.select("delegated_inspection", {"path": "doc.json"}, DELEGATOR)
    assert match.definition is not None and match.admission is not None
    execution = await executor.start(
        match.definition,
        match.admission,
        agent_id="agent-1",
        goal_id="g",
        occurrence_id="g#0",
        parameters={"path": "doc.json"},
        work={"causation_chain": ["a", "b", "c"], "delegation_depth": 3},
    )

    outcome = await executor.advance(execution.execution_id, host)

    assert outcome.kind == "failed" and outcome.code == "DELEGATION_DEPTH_EXCEEDED"
    assert host.routed == []


# -- AC-12: pause, cancellation --------------------------------------------------


async def test_cancel_before_a_write_stops_new_operations(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await _start(
        registry,
        executor,
        "local_json_snapshot",
        {"source": "in.json", "destination": "out/snap.json"},
        {"artifact.read", "artifact.write"},
    )
    await executor.advance(execution.execution_id, host)  # the read

    await executor.request_cancel(execution.execution_id, "operator")
    outcome = await executor.advance(execution.execution_id, host)

    assert outcome.kind == "cancelled"
    assert not (host.root / "out" / "snap.json").exists()
    assert (await executor.advance(execution.execution_id, host)).kind == "cancelled"


async def test_cancel_while_waiting_on_a_child_leaves_the_child_accounted(
    tmp_path: Path,
) -> None:
    executor, host, execution_id, work_id = await _delegation(tmp_path)
    await executor.request_cancel(execution_id, "operator")

    outcome = await executor.advance(execution_id, host)
    # The child's late result is recorded, but it revives nothing.
    await executor.settle_child(
        work_id, status="completed", executor_id="helper", result=GOOD_CHILD
    )

    assert outcome.kind == "cancelled"
    assert (await executor.advance(execution_id, host)).kind == "cancelled"


async def test_pause_holds_and_resume_continues_from_the_same_step(tmp_path: Path) -> None:
    _, registry, executor, host = await setup(tmp_path)
    await promote(registry, LOCAL_JSON_SNAPSHOT)
    (host.root / "in.json").write_text('{"a": 1}', encoding="utf-8")
    execution = await _start(
        registry,
        executor,
        "local_json_snapshot",
        {"source": "in.json", "destination": "out/snap.json"},
        {"artifact.read", "artifact.write"},
    )
    await executor.advance(execution.execution_id, host)
    await executor.set_paused(execution.execution_id, True)

    held = await executor.advance(execution.execution_id, host)
    await executor.set_paused(execution.execution_id, False)
    outcome = await run(executor, execution.execution_id, host)

    assert held.kind == "waiting" and held.code == "PAUSED"
    assert outcome is not None and outcome.kind == "completed"
    _, current = await executor.load(execution.execution_id)
    assert current.completed_steps.count("read_source") == 1, "the graph did not restart"
