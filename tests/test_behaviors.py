from evomesh.behaviors import _extract_rationale, default_behaviors

RATIONALE_MARKER = "RATIONALE:"


def test_extract_rationale_pulls_only_the_marker_line():
    answer = f"{RATIONALE_MARKER} The model chose gold because of rising yields."
    assert _extract_rationale(answer) == "The model chose gold because of rising yields."


def test_default_behaviors_returns_the_four_system_agents():
    behaviors = default_behaviors()
    assert set(behaviors) == {"architect", "guardian", "evaluator", "evolver"}
    assert all(value is not None for value in behaviors.values())
