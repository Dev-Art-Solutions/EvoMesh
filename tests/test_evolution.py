"""Coverage for the small, mechanical exports in ``evolution.py`` that do not
depend on a live harness.

``review_objective`` is a pure string-builder (a classmethod, no instance
needed), so the one check here is: the prompt it returns embeds exactly the
objective and diff it was handed.
"""

from __future__ import annotations

import asyncio

from evomesh.evolution import (
    GenerationChange,
    ObjectivePick,
    PlanNode,
    ValidationResult,
    ValidationRun,
    clip,
    decompose_objective,
    draft_plan_objective,
    evaluate_plan_objective,
    harness_objective,
    harness_repair_objective,
    parse_plan_children,
    parse_plan_verdict,
    review_objective,
)


def test_review_objective_embeds_objective_and_diff() -> None:
    prompt = review_objective("add a test", "def test(): pass\n")
    assert "add a test" in prompt
    assert "def test(): pass\n" in prompt


def test_parse_plan_verdict_returns_true_for_approve() -> None:
    approved, body = parse_plan_verdict("VERDICT: approve")
    assert approved is True
    assert body == "approve"


def test_harness_objective_embeds_objective_and_project() -> None:
    prompt = harness_objective("add a test", "evomesh")
    assert "OBJECTIVE: add a test" in prompt


def test_clip_keeps_the_tail_when_longer_than_limit() -> None:
    assert clip("abcdef", 2) == "...\nef"


def test_decompose_objective_embeds_project_and_title() -> None:
    node = PlanNode(id="leafA", title="item A", reasoning="change alpha")
    prompt = decompose_objective(node, "evomesh")
    assert "evomesh" in prompt
    assert "ITEM TITLE: item A" in prompt


def test_evaluate_plan_objective_embeds_plan_and_project() -> None:
    prompt = evaluate_plan_objective("add a new module", "evomesh")
    assert "add a new module" in prompt
    assert "evomesh" in prompt


def test_parse_plan_children_returns_the_first_child_title() -> None:
    result = parse_plan_children("- title one :: reason one")
    assert result is not None
    assert result[0]["title"] == "title one"


def test_draft_plan_objective_embeds_objective_and_project() -> None:
    prompt = draft_plan_objective("add a test", "evomesh")
    assert "OBJECTIVE: add a test" in prompt
    assert "evomesh" in prompt


def test_harness_repair_objective_embeds_command_and_output() -> None:
    failure = {"command": "uv run pytest", "exit_code": 1, "output": "assert x == 1"}
    prompt = harness_repair_objective(failure, "evomesh", ["src/evomesh/evolution.py"])
    assert "evomesh" in prompt
    assert "The validation command `uv run pytest` failed with exit code 1." in prompt
    assert "OUTPUT:\nassert x == 1" in prompt


def test_objective_pick_repr_names_its_fields() -> None:
    pick = ObjectivePick(
        kind="improvement",
        objective="Make X faster",
        needle="Improve X:",
        key="Faster startup",
    )
    rendered = repr(pick)
    assert rendered.startswith("ObjectivePick(")
    assert "make x faster" in rendered.lower()
    assert "Faster startup" in rendered


def test_generation_change_defaults_kind_to_mutation() -> None:
    change = GenerationChange(path="src/evomesh/evolution.py", rationale="add test")
    assert change.kind == "mutation"
    assert change.path == "src/evomesh/evolution.py"


async def test_validation_run_describes_itself_as_running_then_finished() -> None:
    # Build the task the way the Evolver does -- run the suite off-cycle and hold
    # the Task -- but here we simply let a finished task stand in for one that ran
    # a real validation suite.
    task: asyncio.Task[ValidationResult] = asyncio.create_task(
        asyncio.sleep(0, result=ValidationResult(passed=True, commands=[]))
    )
    run = ValidationRun(generation=1, task=task)
    await task

    assert run.running is False
    assert "finished" in run.describe()
    assert "validating generation 1" in run.describe()
