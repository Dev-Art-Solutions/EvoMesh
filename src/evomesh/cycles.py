"""Cycle detection for the agent dependency graph.

The mesh builds a graph whose nodes are agent definitions and whose edges go
from an agent to every agent it ``depends_on``.  A mesh that depends on itself,
directly or transitively, cannot be started, so :func:`cycle_agents` is run
against that graph before any agent is instantiated.

This module is wired into :class:`evomesh.cognition.Cogniser.run` -- the loop every
agent runs when the mesh starts -- so that a circular dependency in the agent
graph fails the whole cycle rather than leaving an agent spinning in an infinite
reasoning loop, without the caller having to know a cycle checker exists.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["CycleDetector", "CycleResult", "cycle_agents", "explain_not_running"]


@dataclass
class CycleResult:
    """The outcome of running cycle detection against one agent graph."""

    has_cycle: bool = False
    cycle: list[str] = field(default_factory=list)
    message: str = ""


class CycleDetector:
    """A depth-first search that reports the first reference cycle it finds."""

    def __init__(self) -> None:
        # _edges[agent] is the list of agents ``agent`` depends on.
        self._edges: dict[str, list[str]] = {}

    def add_agent(self, agent_id: str, depends_on: str | None = None) -> None:
        self._edges.setdefault(agent_id, [])
        if depends_on is None:
            return
        self._edges.setdefault(depends_on, [])
        if agent_id not in self._edges[depends_on]:
            self._edges[depends_on].append(agent_id)

    def find_cycle(self) -> CycleResult:
        cycle = self._detect()
        if cycle is None:
            return CycleResult(
                has_cycle=False,
                cycle=[],
                message="No reference cycle in the agent dependency graph.",
            )
        path = " -> ".join(cycle)
        return CycleResult(
            has_cycle=True,
            cycle=cycle,
            message=f"Agent cycle detected ({path}); the mesh cannot start.",
        )

    def _detect(self) -> list[str] | None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node: WHITE for node in self._edges}
        for root in sorted(self._edges):
            if color[root] != WHITE:
                continue
            stack: list[tuple[str, Any]] = [(root, iter(self._edges[root]))]
            color[root] = GRAY
            on_path = [root]
            while stack:
                node, neighbours = stack[-1]
                progressed = False
                for nxt in neighbours:
                    if color[nxt] == GRAY:
                        return on_path[on_path.index(nxt):] + [nxt]
                    if color[nxt] != WHITE:
                        continue
                    color[nxt] = GRAY
                    stack.append((nxt, iter(self._edges[nxt])))
                    on_path.append(nxt)
                    progressed = True
                    break
                if not progressed:
                    color[node] = BLACK
                    stack.pop()
                    on_path.pop()
        return None


def cycle_agents(graph: Mapping[str, Iterable[str]]) -> CycleResult:
    """Return the first reference cycle, if any, in an agent dependency graph.

    ``graph`` maps each agent id to the agents it ``depends_on``.  A
    self-dependency (an agent that depends on itself) is a one-node cycle.
    """

    detector = CycleDetector()
    for agent_id, depends_on in graph.items():
        if depends_on is None:
            detector.add_agent(agent_id)
            continue
        for dep in depends_on:
            detector.add_agent(agent_id, dep)
    return detector.find_cycle()


def explain_not_running(
    agent: str, dependencies: Mapping[str, Iterable[str]] | None = None
) -> str:
    """Explain why an agent is not running, naming dependency cycles explicitly."""
    if dependencies is None:
        return f"agent {agent!r} is not running"
    cycle = cycle_agents(dependencies).cycle
    if cycle:
        path = " -> ".join(cycle)
        return f"agent {agent!r} is blocked by a dependency cycle ({path})"
    return f"agent {agent!r} is not running"