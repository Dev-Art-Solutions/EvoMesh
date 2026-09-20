from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from evomesh.node import Node

from .mesh_utils import directed_pair_count


@dataclass
class Mesh:
    """A directed, labelled graph of agents and their connections.

    Vertices are :class:`evomesh.node.Node` objects, the package's core node
    abstraction for the EvoMesh graph.
    """

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, dict[str, str]] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        self.nodes[node.node_id] = node
        self.edges.setdefault(node.node_id, {})

    def add_edge(self, source: str, target: str, label: str = "") -> None:
        if source not in self.nodes or target not in self.nodes:
            raise KeyError(f"edge references unknown node: {source!r} or {target!r}")
        self.edges.setdefault(source, {})[target] = label

    def neighbours(self, node_id: str) -> list[str]:
        return sorted(self.edges.get(node_id, {}))

    def out_degree(self, node_id: str) -> int:
        return len(self.edges.get(node_id, {}))

    def in_degree(self, node_id: str) -> int:
        return sum(1 for outs in self.edges.values() if node_id in outs)

    def iter_nodes(self) -> Iterable[Node]:
        return self.nodes.values()

    def iter_edges(self) -> Iterable[tuple[str, str, str]]:
        for source, targets in self.edges.items():
            for target, label in targets.items():
                yield source, target, label

    def edge_count(self) -> int:
        edges = (
            (source, target)
            for source, targets in self.edges.items()
            for target in targets
        )
        return len(directed_pair_count(edges))
