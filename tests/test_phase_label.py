from evomesh.phase_label import phase_label


def test_phase_label_renders_a_phase_as_a_readable_label():
    assert phase_label("thinking") == "Thinking"
