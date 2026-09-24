"""Coverage for the small, mechanical exports in ``evolution.py`` that do not
depend on a live harness.

``review_objective`` is a pure string-builder (a classmethod, no instance
needed), so the one check here is: the prompt it returns embeds exactly the
objective and diff it was handed.
"""

from __future__ import annotations

from evomesh.evolution import review_objective


def test_review_objective_embeds_objective_and_diff() -> None:
    prompt = review_objective("add a test", "def test(): pass\n")
    assert "add a test" in prompt
    assert "def test(): pass\n" in prompt
