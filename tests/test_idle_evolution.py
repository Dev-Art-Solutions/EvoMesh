"""An empty backlog is IDLE, not busywork (closure plan v2 AC-18).

With no eligible, evidenced improvement the Evolver opens no candidate and
calls no model; a real failure still becomes the objective; a speculative
proposal stays a proposal. Exploration (a scout, the dead-module backlog)
happens only when a human turns ``evolution.scout_when_idle`` on."""

from __future__ import annotations

from pathlib import Path

import pytest

from evomesh.behaviors import EvolverBehavior
from evomesh.config import Settings
from evomesh.evolution import PICK_BASELINE
from evomesh.improvements import ImprovementStatus
from evomesh.models import MockProvider
from tests.test_baseline import RED, _baseline_returns, _context
from tests.test_improvement_control import _control_context, control


@pytest.fixture
def idle_project(tmp_path: Path) -> Path:
    """Nothing evidenced to do -- but a dead module an idle explorer would
    happily 'wire or delete'."""
    root = tmp_path / "project"
    package = root / "src" / "evomesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Package."""\n\nfrom evomesh.busy import helper\n', encoding="utf-8"
    )
    (package / "busy.py").write_text('"""Work."""\n\ndef helper():\n    pass\n', encoding="utf-8")
    (package / "orphan.py").write_text('"""Imported by nothing."""\n', encoding="utf-8")
    (root / "tests").mkdir()
    return root


def _calls(context) -> int:  # type: ignore[no-untyped-def]
    provider = context.provider
    assert isinstance(provider, MockProvider)
    evolver = context.services["evolver"]
    return len(provider.calls) + len(evolver.provider.calls)


def test_idle_exploration_is_off_by_default() -> None:
    assert Settings().evolution.scout_when_idle is False


async def test_an_empty_backlog_is_idle_with_no_model_call(
    tmp_path: Path, idle_project: Path
) -> None:
    context, evolver = await _context(tmp_path, idle_project)
    assert evolver.backlog_target(0) is not None, "there is dead code to be tempted by"

    outcome = await EvolverBehavior(test_backlog=False, scout_when_idle=False).cycle(context)

    assert "nothing substantive" in outcome.summary
    assert evolver.workspace.supervisor.candidates() == []
    assert _calls(context) == 0


async def test_an_empty_ranked_backlog_is_idle_too(tmp_path: Path, idle_project: Path) -> None:
    context, evolver = await _context(tmp_path, idle_project)
    plane, _ = control()
    _control_context(context, plane)

    outcome = await EvolverBehavior(test_backlog=False, scout_when_idle=False).cycle(context)

    assert "nothing substantive" in outcome.summary
    assert evolver.workspace.supervisor.candidates() == []
    assert plane.backlog.items == {}, "no fallback was adopted as an improvement"
    assert _calls(context) == 0


async def test_a_real_failure_still_becomes_the_objective(
    tmp_path: Path, idle_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, evolver = await _context(tmp_path, idle_project)
    _baseline_returns(monkeypatch, evolver, RED)

    await EvolverBehavior(
        baseline_tests=True, test_backlog=False, scout_when_idle=False
    ).cycle(context)

    assert (await evolver.pipeline_state())["pick"] == PICK_BASELINE


async def test_a_speculative_proposal_is_not_activated(
    tmp_path: Path, idle_project: Path
) -> None:
    context, evolver = await _context(tmp_path, idle_project)
    plane, _ = control()
    _control_context(context, plane)
    proposal = await plane.propose_discovery(
        "rename helper() in src/evomesh/busy.py for style", generation=1, job=1
    )
    assert proposal is not None and proposal.status is ImprovementStatus.TRIAGED

    outcome = await EvolverBehavior(test_backlog=False, scout_when_idle=False).cycle(context)

    assert "nothing substantive" in outcome.summary
    assert proposal.status is ImprovementStatus.TRIAGED
    assert evolver.workspace.supervisor.candidates() == []


async def test_turning_exploration_on_brings_the_dead_module_back(
    tmp_path: Path, idle_project: Path
) -> None:
    context, evolver = await _context(tmp_path, idle_project)

    await EvolverBehavior(test_backlog=False, scout_when_idle=True).cycle(context)

    state = await evolver.pipeline_state()
    assert "orphan" in str(state.get("objective", "")), state
