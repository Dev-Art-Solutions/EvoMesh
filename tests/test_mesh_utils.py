from evomesh.mesh_utils import undirected_edges


def test_undirected_edges_dedupes_reversed_pair_keeps_first_direction():
    # Same undirected pair in both directions collapses to a single edge,
    # keeping the first-seen direction as-is (not a canonical undirected form).
    assert undirected_edges([(0, 1), (1, 0)]) == [(0, 1)]
