"""Coverage for the small, mechanical exports in ``evolution.py`` that do not
depend on a live harness.

``review_objective`` is a pure string-builder (a classmethod, no instance
needed), so the one check here is: the prompt it returns embeds exactly the
objective and diff it was handed.
"""

from __future__ import annotations

from evomesh.evolution import harness_objective, parse_plan_verdict, review_objective


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
