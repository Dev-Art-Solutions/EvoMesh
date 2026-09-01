"""Utility helpers for the EvoMesh package.

This module provides small, self-contained geometric helpers that operate on
plain vertex/face arrays so they can be reused across the package without
pulling in heavy dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence

Vector = Sequence[float]
Face = Sequence[int]


def compute_area(vertices: Sequence[Sequence[float]], faces: Sequence[Sequence[int]]) -> float:
    """Return the total surface area of a triangle mesh.

    Args:
        vertices: An (N, 3) array-like of vertex coordinates.
        faces: A (T, 3) array-like of triangle vertex indices.

    Returns:
        The summed area of all triangular faces.
    """
    total = 0.0
    for f in faces:
        i, j, k = f[0], f[1], f[2]
        ax = vertices[j][0] - vertices[i][0]
        ay = vertices[j][1] - vertices[i][1]
        az = vertices[j][2] - vertices[i][2]
        bx = vertices[k][0] - vertices[i][0]
        by = vertices[k][1] - vertices[i][1]
        bz = vertices[k][2] - vertices[i][2]
        cross_x = ay * bz - az * by
        cross_y = az * bx - ax * bz
        cross_z = ax * by - ay * bx
        total += 0.5 * (cross_x ** 2 + cross_y ** 2 + cross_z ** 2) ** 0.5
    return total


def compute_centroid(
    vertices: Sequence[Sequence[float]], faces: Sequence[Sequence[int]]
) -> tuple[float, float, float]:
    """Return the area-weighted centroid of a triangle mesh.

    Args:
        vertices: An (N, 3) array-like of vertex coordinates.
        faces: A (T, 3) array-like of triangle vertex indices.

    Returns:
        A 3-tuple of centroid coordinates.
    """
    cx = cy = cz = area = 0.0
    for f in faces:
        i, j, k = f[0], f[1], f[2]
        ax = vertices[j][0] - vertices[i][0]
        ay = vertices[j][1] - vertices[i][1]
        az = vertices[j][2] - vertices[i][2]
        bx = vertices[k][0] - vertices[i][0]
        by = vertices[k][1] - vertices[i][1]
        bz = vertices[k][2] - vertices[i][2]
        cross_x = ay * bz - az * by
        cross_y = az * bx - ax * bz
        cross_z = ax * by - ay * bx
        tri_area = 0.5 * (cross_x ** 2 + cross_y ** 2 + cross_z ** 2) ** 0.5
        cx += (vertices[i][0] + vertices[j][0] + vertices[k][0]) / 3.0 * tri_area
        cy += (vertices[i][1] + vertices[j][1] + vertices[k][1]) / 3.0 * tri_area
        cz += (vertices[i][2] + vertices[j][2] + vertices[k][2]) / 3.0 * tri_area
        area += tri_area
    if area == 0.0:
        return 0.0, 0.0, 0.0
    return cx / area, cy / area, cz / area
