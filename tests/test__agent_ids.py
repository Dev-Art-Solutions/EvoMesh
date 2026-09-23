from evomesh._agent_ids import is_valid, make_id


def test_is_valid_accepts_a_hierarchical_identifier() -> None:
    assert is_valid("root.child.grandchild") is True


def test_make_id_joins_parts_with_dots() -> None:
    assert make_id("root", "child") == "root.child"
