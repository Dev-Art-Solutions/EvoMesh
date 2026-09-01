from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def _normalize_nodes(nodes: Any) -> list[Any]:
    if nodes is None:
        return []
    if isinstance(nodes, dict):
        return list(nodes.values())
    if isinstance(nodes, Iterable):
        return list(nodes)
    return [nodes]


def _normalize_edges(edges: Any) -> list[tuple[Any, Any]]:
    if edges is None:
        return []
    if isinstance(edges, dict):
        normalized: list[tuple[Any, Any]] = []
        for src, targets in edges.items():
            for dst in _normalize_nodes(targets):
                normalized.append((src, dst))
        return normalized
    if isinstance(edges, Iterable):
        result: list[tuple[Any, Any]] = []
        for edge in edges:
            if isinstance(edge, (list, tuple)) and len(edge) == 2:
                result.append((edge[0], edge[1]))
            else:
                raise ValueError(f"Invalid edge definition: {edge!r}")
        return result
    raise ValueError(f"Cannot interpret edges as {type(edges)!r}")


def build_adjacency(nodes: Any, edges: Any) -> dict[Any, list[Any]]:
    adjacency: dict[Any, list[Any]] = defaultdict(list)
    for node in _normalize_nodes(nodes):
        adjacency.setdefault(node, [])
    for src, dst in _normalize_edges(edges):
        adjacency.setdefault(src, [])
        adjacency.setdefault(dst, [])
        adjacency[src].append(dst)
    return {node: list(targets) for node, targets in adjacency.items()}


def directed_pair_count(edges: Iterable[tuple[Any, Any]]) -> dict[tuple[Any, Any], int]:
    counts: dict[tuple[Any, Any], int] = defaultdict(int)
    for src, dst in edges:
        counts[(src, dst)] += 1
    return dict(counts)


def undirected_edges(edges: Iterable[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
    seen: set = set()
    result: list[tuple[Any, Any]] = []
    for src, dst in edges:
        key = frozenset((src, dst))
        if key in seen:
            continue
        seen.add(key)
        result.append((src, dst))
    return result


def _coerce_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _coerce_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_value(v) for v in value]
    return value


def merge_attributes(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {k: _coerce_value(v) for k, v in base.items()}
    if override:
        for key, value in override.items():
            merged[key] = _coerce_value(value)
    return merged
