from evomesh.phase_label import phase_label


def test_phase_label_renders_a_phase_as_a_readable_label():
    assert phase_label("thinking") == "Thinking"


def test_phase_label_renders_an_AgentPhase_enum_value():
    from evomesh.phase_label import AgentPhase

    assert phase_label(AgentPhase.IDLE) == "Idle"


def test_phase_label_preserves_trailing_uppercase_and_digits():
    # Unknown phases: only the first char is upper-cased; the rest is
    # left as-is, so "E2E" and "2D" are not mangled.
    assert phase_label("E2E") == "E2E"
    assert phase_label("2D") == "2D"


def test_phase_label_capitalizes_only_the_first_char():
    assert phase_label("planning_phase") == "Planning phase"


def test_phase_label_none_for_empty_whitespace_and_none():
    # Documented contract: empty, whitespace-only, or None input yields None.
    assert phase_label("") is None
    assert phase_label("   ") is None
    assert phase_label(None) is None


def test_phase_label_formats_non_empty_unknown_phase():
    # Empty/whitespace is the only None case; non-empty unknown phases are
    # still upper-cased, so this returns a formatted label, not None.
    assert phase_label("unknown_phase") == "Unknown phase"
