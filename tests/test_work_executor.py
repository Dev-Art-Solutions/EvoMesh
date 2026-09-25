"""The WorkExecutor seam as behaviour, not outcome reporting (closure plan v2
AC-19): the coordinator submits through it, inspects through it, cancels
through it, and a restart recovers the handle from the stored backlog."""

from __future__ import annotations

from pathlib import Path

from evomesh.coordination import WorkItem, WorkStatus
from evomesh.evolution import CandidateWorkspace, GenerationExecutor
from evomesh.improvements import (
    ExecutionScope,
    ImprovementBacklog,
    ImprovementStatus,
    WorkState,
    work_handle,
)
from tests.fakes_executor import InlineExecutor
from tests.test_improvement_control import candidate, control


async def _begun(executor: InlineExecutor | GenerationExecutor, reference: str = "1"):  # type: ignore[no-untyped-def]
    plane, announced = control()
    await plane.sync([candidate("item:small")], {"item:small"})
    item = plane.choose()
    assert item is not None
    work = await plane.begin(
        item,
        objective="do it",
        route=lambda _: "coder",
        executor=executor,
        workspace="/isolated/ws",
        reference=reference,
    )
    assert work is not None
    return plane, item, work, announced


async def test_the_selected_executor_is_the_one_that_starts_the_work() -> None:
    executor = InlineExecutor()

    _, _, work, _ = await _begun(executor)

    assert executor.started == [
        (work.id, ExecutionScope(assignee="coder", workspace="/isolated/ws", reference="1"))
    ]
    assert work.assigned_agent_id == "coder" and work.status is WorkStatus.ACTIVE
    assert work.inputs["handle"]["executor"] == "inline"


async def test_a_substituted_executor_settles_through_the_same_plane() -> None:
    executor = InlineExecutor()
    plane, item, work, _ = await _begun(executor)

    await plane.settle({"inline": executor}, {"item:small"})
    assert work.status is WorkStatus.ACTIVE, "still pending"
    executor.finish(work.inputs["handle"]["ref"], WorkState.COMPLETED)
    await plane.settle({"inline": executor}, {"item:small"})

    assert work.status is WorkStatus.COMPLETED
    assert item.status is not ImprovementStatus.ACTIVE


async def test_an_unknown_state_is_never_read_as_success() -> None:
    executor = InlineExecutor()
    plane, _, work, _ = await _begun(executor)
    executor.states.clear()  # the executor lost track

    await plane.settle({"inline": executor}, {"item:small"})

    assert work.status is WorkStatus.ACTIVE


async def test_a_restart_recovers_the_handle_and_the_outcome() -> None:
    executor = InlineExecutor()
    plane, _, work, _ = await _begun(executor)
    stored = plane.backlog.dump()

    restored = ImprovementBacklog.load(stored)
    again, _ = control()
    again.backlog.items = restored.items
    again.backlog.work_items = restored.work_items
    reloaded = again.backlog.work_items[work.id]
    handle = work_handle(reloaded)
    assert handle is not None and handle["executor"] == "inline"
    executor.finish(handle["ref"], WorkState.FAILED)
    await again.settle({"inline": executor}, {"item:small"})

    assert reloaded.status in {WorkStatus.FAILED, WorkStatus.PENDING, WorkStatus.BLOCKED}
    assert reloaded.status is not WorkStatus.COMPLETED


async def test_cancel_goes_through_the_executor_and_is_not_success() -> None:
    executor = InlineExecutor()
    plane, item, work, _ = await _begun(executor)

    results = await plane.cancel(item.id, {"inline": executor}, "operator changed their mind")

    assert [result.state for result in results] == [WorkState.CANCELLED]
    assert executor.cancelled == [work.inputs["handle"]["ref"]]
    assert work.status is WorkStatus.CANCELLED
    assert item.status is ImprovementStatus.BLOCKED


async def test_the_generation_executor_binds_an_open_generation(tmp_path: Path) -> None:
    from evomesh.evolution import EnvironmentEvolver
    from evomesh.storage import SQLiteRepository
    from tests.test_cycles import git_project

    root = tmp_path / "project"
    (root / "src" / "evomesh").mkdir(parents=True)
    (root / "src" / "evomesh" / "__init__.py").write_text('"""P."""\n', encoding="utf-8")
    project = await git_project(root)
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(CandidateWorkspace(project, tmp_path / "generations"), repository)
    generation = await evolver.create_candidate("objective")
    executor = GenerationExecutor(evolver.workspace.supervisor)
    work = WorkItem(parent_goal_id="g", objective="x")

    handle = await executor.submit(
        work, ExecutionScope("evolver", str(generation.path), str(generation.number))
    )
    pending = executor.inspect(handle)
    cancelled = await executor.request_cancel(handle)

    assert handle["executor"] == "generation" and handle["assignee"] == "evolver"
    assert pending.state is WorkState.PENDING
    assert cancelled.state is WorkState.CANCELLED
    assert executor.inspect(handle).state is WorkState.FAILED, "a discarded run is not success"


async def test_a_work_item_from_before_the_seam_still_settles() -> None:
    legacy = WorkItem(parent_goal_id="g", objective="old", inputs={"generation": 7})

    handle = work_handle(legacy)

    assert handle == {"executor": "generation", "ref": "7"}
