"""Core mesh utilities for EvoMesh.

This module provides small helpers for representing and evolving triangle
meshes. The functions intentionally avoid mutating caller data in place so
that evolutionary populations remain predictable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Vertex:
    """A single 3D vertex."""

    x: float
    y: float
    z: float

    def copy(self) -> Vertex:
        """Return an independent copy of this vertex."""
        return Vertex(self.x, self.y, self.z)


@dataclass
class TriangleMesh:
    """A minimal triangle mesh built from vertices and indices."""

    vertices: list[Vertex] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)

    def vertex_count(self) -> int:
        """Return the number of vertices in the mesh."""
        return len(self.vertices)

    def validate_indices(self) -> bool:
        """Return True when every index references a valid vertex."""
        count = self.vertex_count()
        return all(0 <= idx < count for idx in self.indices)

    def clone(self) -> TriangleMesh:
        """Return an independent deep copy of this mesh."""
        return TriangleMesh(
            vertices=[v.copy() for v in self.vertices],
            indices=list(self.indices),
        )