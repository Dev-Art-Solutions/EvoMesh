from evomesh.verdict_label import verdict_label


def test_verdict_label_renders_a_passed_verdict_as_pass():
    assert verdict_label("passed") == "PASS"
