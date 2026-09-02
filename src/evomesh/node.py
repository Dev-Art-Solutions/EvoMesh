'''Core node abstraction for the EvoMesh graph.'''

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    '''A single vertex in the evolving mesh.'''

    node_id: str
    neighbors: list[str] = field(default_factory=list)

    def connect(self, other: Node) -> None:
        '''Add a bidirectional edge to another node.'''
        if other.node_id not in self.neighbors:
            self.neighbors.append(other.node_id)
        if self.node_id not in other.neighbors:
            other.neighbors.append(self.node_id)

    def is_connected(self, other: Node) -> bool:
        '''Return True if this node is directly linked to another.'''
        return other.node_id in self.neighbors