"""The live tree's own suite is run before an objective is picked.

Found live 2026-09-25: generation 1463 landed a test that could never pass on
this Windows host; every later candidate failed validation on it, and the
evolver kept writing one more small test on top of a red suite. A red suite is
now the objective, and "write ONE small test" is no longer a fallback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evomesh.behaviors import EvolverBehavior
from evomesh.cognition import CycleContext
from evomesh.contracts import AgentDefinition
from evomesh.evolution import (
    BASELINE_FILE,
    BASELINE_NEEDLE,
    MAX_TARGET_ATTEMPTS,
    PICK_BASELINE,
    BaselineResult,
    CandidateWorkspace,
    EnvironmentEvolver,
    parse_baseline,
)
from evomesh.memory import AgentMemory, MemoryBudget
from evomesh.models import MockProvider
from evomesh.storage import SQLiteRepository

RED = BaselineResult(
    key="abc:123",
    passed=False,
    failures=("tests/test_processes.py::test_group",),
    output="FileNotFoundError: /tmp/evomesh_pg_child.pid",
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    package = root / "src" / "evomesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Package."""\n\nfrom evomesh.busy import helper\n', encoding="utf-8"
    )
    (package / "busy.py").write_text(
        '"""Does the real work."""\n\ndef helper():\n    pass\n', encoding="utf-8"
    )
    (root / "tests").mkdir()
    return root


async def _context(tmp_path: Path, project: Path) -> tuple[CycleContext, EnvironmentEvolver]:
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    evolver = EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"), repository, MockProvider()
    )
    definition = AgentDefinition(name="Environment Evolver", purpose="Evolve")
    definition.mind.add_goal(
        "Improve EvoMesh by one validated candidate generation at a time.", recurring=True
    )
    memory = AgentMemory(tmp_path / "workspace", definition)
    await memory.ensure()
    context = CycleContext(
        definition=definition,
        provider=MockProvider(),
        memory=memory,
        budget=MemoryBudget(),
        services={"evolver": evolver},
    )
    return context, evolver


def _baseline_returns(
    monkeypatch: pytest.MonkeyPatch, evolver: EnvironmentEvolver, result: BaselineResult | None
) -> None:
    async def baseline(timeout_seconds: float = 0) -> BaselineResult | None:
        return result

    monkeypatch.setattr(evolver, "baseline", baseline)


def test_a_green_run_passes_and_a_red_one_names_its_failures() -> None:
    assert parse_baseline("k", 0, "774 passed").passed

    red = parse_baseline(
        "k",
        1,
        "FAILED tests/test_a.py::test_x - boom\nERROR tests/test_b.py::test_y\n"
        "FAILED tests/test_a.py::test_x - boom\n2 failed, 770 passed\n",
    )

    assert not red.passed
    assert red.failures == ("tests/test_a.py::test_x", "tests/test_b.py::test_y")
    assert parse_baseline("k", 2, "1 passed, collection crashed").failures == (
        "pytest exited 2",
    )


def test_a_run_where_nothing_passed_is_no_verdict() -> None:
    """Found live: 790 PermissionErrors at tmp_path setup, read as a red suite."""
    broken = parse_baseline(
        "k", 1, "ERROR tests/test_a.py::test_x - PermissionError\n790 errors in 51.07s\n"
    )

    assert broken.blocked
    assert not broken.failures


async def test_a_red_suite_is_the_objective_before_any_improvement(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = project / "docs" / "evolution" / "improvements.md"
    backlog.parent.mkdir(parents=True)
    backlog.write_text("- [ ] Make helper useful\n    It does nothing.\n", encoding="utf-8")
    context, evolver = await _context(tmp_path, project)
    _baseline_returns(monkeypatch, evolver, RED)

    await EvolverBehavior(baseline_tests=True).cycle(context)

    state = await evolver.pipeline_state()
    assert state["pick"] == PICK_BASELINE
    assert state["objective"].startswith(BASELINE_NEEDLE)
    assert "tests/test_processes.py::test_group" in state["objective"]


async def test_nothing_opens_while_the_suite_is_still_running(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, evolver = await _context(tmp_path, project)
    _baseline_returns(monkeypatch, evolver, None)

    outcome = await EvolverBehavior(baseline_tests=True).cycle(context)

    assert "test suite" in outcome.summary
    assert evolver.workspace.supervisor.candidates() == []
    assert (await evolver.pipeline_state())["stage"] == "plan"


async def test_a_suite_the_mesh_cannot_fix_pauses_evolution(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, evolver = await _context(tmp_path, project)
    _baseline_returns(monkeypatch, evolver, RED)
    for number in range(1, MAX_TARGET_ATTEMPTS + 1):
        tried = tmp_path / "generations" / f"{number:06d}-candidate"
        tried.mkdir(parents=True)
        (tried / "MUTATION_OBJECTIVE.md").write_text(RED.objective(), encoding="utf-8")

    outcome = await EvolverBehavior(baseline_tests=True).cycle(context)

    assert "paused" in outcome.summary
    assert (await evolver.pipeline_state())["stage"] == "plan"


async def test_with_the_test_backlog_off_an_untested_export_is_not_an_objective(
    tmp_path: Path, project: Path
) -> None:
    context, evolver = await _context(tmp_path, project)

    outcome = await EvolverBehavior(test_backlog=False).cycle(context)

    assert "nothing substantive" in outcome.summary
    assert evolver.workspace.supervisor.candidates() == []


async def test_the_verdict_is_reused_until_the_tree_changes(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, evolver = await _context(tmp_path, project)
    keys = iter(["one", "one", "one", "two"])
    runs: list[str] = []

    async def key() -> str:
        return next(keys)

    async def run(tree: str) -> BaselineResult:
        runs.append(tree)
        return BaselineResult(key=tree, passed=True)

    monkeypatch.setattr(evolver, "baseline_key", key)
    monkeypatch.setattr(evolver, "_run_baseline", run)

    assert await evolver.baseline() is None  # started
    await asyncio.sleep(0.01)
    assert (await evolver.baseline()) == BaselineResult(key="one", passed=True)
    assert (project / BASELINE_FILE).is_file()
    assert (await evolver.baseline()) == BaselineResult(key="one", passed=True)
    assert await evolver.baseline() is None  # new tree, new run
    await asyncio.sleep(0.01)
    assert runs == ["one", "two"]


async def test_a_generation_decided_outside_the_pipeline_frees_it(
    tmp_path: Path, project: Path
) -> None:
    """Found live 2026-09-26: the mesh was stopped with the pipeline at
    `report` for a generation that was then discarded, and every cycle after
    the restart failed with 'Generation N is not a known candidate'."""
    context, evolver = await _context(tmp_path, project)
    generation = await evolver.create_candidate("objective")
    evolver.workspace.supervisor.discard(generation.number)
    await evolver.set_pipeline_state(
        {"stage": "report", "generation": generation.number, "objective": "objective"}
    )

    outcome = await EvolverBehavior(test_backlog=False, scout_when_idle=False).cycle(context)

    assert outcome.error is None, outcome.error
    assert "already discarded" in outcome.summary
    assert (await evolver.pipeline_state())["stage"] == "plan"
