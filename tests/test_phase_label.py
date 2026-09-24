from evomesh.phase_label import phase_label


def test_phase_label_renders_a_phase_as_a_readable_label():
    assert phase_label("thinking") == "Thinking"


def test_phase_label_renders_an_AgentPhase_enum_value():
    from evomesh.phase_label import AgentPhase

    assert phase_label(AgentPhase.IDLE) == "Idle"
