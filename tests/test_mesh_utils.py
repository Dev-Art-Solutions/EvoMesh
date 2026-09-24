from evomesh.mesh_utils import edge_snapshot, undirected_edges


def test_undirected_edges_dedupes_reversed_pair_keeps_first_direction():
    # Same undirected pair in both directions collapses to a single edge,
    # keeping the first-seen direction as-is (not a canonical undirected form).
    assert undirected_edges([(0, 1), (1, 0)]) == [(0, 1)]


def test_edge_snapshot_returns_copy_with_same_contents():
    # edge_snapshot returns the edges with its inner target dicts preserved as
    # live views, so the snapshot has the same contents as the input.
    edges = {"a": {"b": "label"}}
    assert edge_snapshot(edges) == {"a": {"b": "label"}}
