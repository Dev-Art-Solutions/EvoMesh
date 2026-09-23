from evomesh._agent_ids import is_valid


def test_is_valid_accepts_a_hierarchical_identifier() -> None:
    assert is_valid("root.child.grandchild") is True
