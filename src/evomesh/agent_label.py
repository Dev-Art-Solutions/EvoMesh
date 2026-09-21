"""Render an agent role as a short human label."""

from __future__ import annotations

_AGENT_LABELS: dict[str, str] = {
    "agent_architect": "Architect",
    "guardian": "Guardian",
    "evaluator": "Evaluator",
    "evolver": "Environment Evolver",
    "trader": "Trader",
    "news-watcher": "NewsWatcher",
    "news-analyzer": "NewsAnalyzer",
}


def agent_label(role: str) -> str:
    """Return a short human label for the given agent role.

    Roles present in :data:`_AGENT_LABELS` are mapped to their label; any other
    role is treated as a plain ``agent`` rather than being returned verbatim, so
    every caller gets the canonical non-role label the shared contract in
    ``contracts.py`` documents.
    """
    return _AGENT_LABELS.get(role, "agent")