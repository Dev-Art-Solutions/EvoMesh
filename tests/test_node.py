from evomesh.node import Node


def test_connect_makes_two_nodes_is_connected_to_each_other():
     a = Node("a")
     b = Node("b")
     a.connect(b)
     assert a.is_connected(b)
     assert b.is_connected(a)
