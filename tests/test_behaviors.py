from evomesh.behaviors import _extract_rationale

RATIONALE_MARKER = "RATIONALE:"


def test_extract_rationale_pulls_only_the_marker_line():
    answer = f"{RATIONALE_MARKER} The model chose gold because of rising yields."
    assert _extract_rationale(answer) == "The model chose gold because of rising yields."
