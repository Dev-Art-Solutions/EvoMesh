from evomesh.agent_label import agent_label


def test_agent_label_maps_a_known_role_to_its_short_label():
    assert agent_label("trader") == "Trader"
