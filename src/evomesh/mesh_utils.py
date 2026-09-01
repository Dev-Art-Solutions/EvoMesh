"""Utility functions for basic mesh operations in EvoMesh."""

from __future__ import annotations

from collections.abc import Iterable


def vertex_count(vertices: Iterable) -> int:
    """Return the number of vertices in an iterable of vertex positions."""
    return sum(1 for _ in vertices)


def edge_count(faces: Iterable) -> int:
    """Return the total number of edges across all triangular faces."""
    return sum(3 for _ in faces)


def normalize_vertices(
    vertices: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Return vertices translated so their centroid sits at the origin."""
    if not vertices:
        return []
    n = len(vertices)
    mean_x = sum(v[0] for v in vertices) / n
    mean_y = sum(v[1] for v in vertices) / n
    mean_z = sum(v[2] for v in vertices) / n
    return [(x - mean_x, y - mean_y, z - mean_z) for x, y, z in vertices]