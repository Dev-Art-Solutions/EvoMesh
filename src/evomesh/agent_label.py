"""Render an agent role as a short human label."""

from __future__ import annotations

_AGENT_LABELS: dict[str, str] = {
    "agent_architect": "Architect",
    "guardian": "Guardian",
    "evaluator": "Evaluator",
    "evolver": "Environment Evolver",
    "trader": "Trader",
}


def agent_label(role: str) -> str:
    """Return a short human label for the given agent role.

    Unknown roles fall back to the role string unchanged so callers can
    rely on a non-empty, always-valid label.
    """
    return _AGENT_LABELS.get(role, role)