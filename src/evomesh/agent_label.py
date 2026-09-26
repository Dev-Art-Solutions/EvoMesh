"""Render an agent role as a short human label."""

from __future__ import annotations

_AGENT_LABELS: dict[str, str] = {
    "agent_architect": "Architect",
    "guardian": "Guardian",
    "evaluator": "Evaluator",
    "evolver": "Environment Evolver",
    "ideas": "Idea Scout",
    "trader": "Trader",
    "news-watcher": "NewsWatcher",
    "news-analyzer": "NewsAnalyzer",
}


def agent_label(role: str) -> str:
    """Return a short human label for the given agent role.

    Roles present in :data:`_AGENT_LABELS` are mapped to their label; any other
    role is returned verbatim, so a custom or unknown agent type (e.g.
    ``researcher``) keeps its own name instead of being rendered as the generic
    ``agent``.
    """
    return _AGENT_LABELS.get(role, role)