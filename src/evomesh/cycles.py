"""Cycle detection for the agent dependency graph."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = ["cycle_agents", "explain_not_running"]


def cycle_agents(dependencies: Mapping[str, Iterable[str]]) -> set[str]:
    """Return every agent that participates in a dependency cycle."""
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: set[str] = set()
    counter = 0

    def strongconnect(node: str) -> None:
        nonlocal counter
        index[node] = lowlink[node] = counter
        counter += 1
        stack.append(node)
        on_stack[node] = True
        for neighbour in dependencies.get(node, ()):
            if neighbour not in index:
                strongconnect(neighbour)
                lowlink[node] = min(lowlink[node], lowlink[neighbour])
            elif on_stack.get(neighbour):
                lowlink[node] = min(lowlink[node], index[neighbour])
        if lowlink[node] == index[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack[member] = False
                component.append(member)
                if member == node:
                    break
            if len(component) > 1 or component[0] in dependencies.get(component[0], ()):
                result.update(component)

    for node in dependencies:
        if node not in index:
            strongconnect(node)
    return result


def explain_not_running(agent: str, dependencies: Mapping[str, Iterable[str]]) -> str:
    """Explain why an agent is not running, naming dependency cycles explicitly."""
    if agent in cycle_agents(dependencies):
        return f"agent {agent!r} is blocked by a dependency cycle"
    return f"agent {agent!r} is not running"