"""Render an agent phase as a short human label."""

from __future__ import annotations

from enum import StrEnum


class AgentPhase(StrEnum):
    """Well-known agent phases in EvoMesh.

    Mirrors ``AgentPhase`` in :mod:`evomesh.contracts` so this module can
    render whatever phase a live agent is currently in.
    """

    OFFLINE = "offline"
    STARTING = "starting"
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    AWAITING_HARNESS = "awaiting-harness"
    WAITING_HUMAN = "waiting-human"
    ERROR = "error"

    def label(self) -> str:
        """Return a short human-readable label for this phase."""
        return _LABELS[self.value]


_LABELS: dict[str, str] = {
    "offline": "Offline",
    "starting": "Starting",
    "idle": "Idle",
    "thinking": "Thinking",
    "acting": "Acting",
    "awaiting-harness": "Awaiting harness",
    "waiting-human": "Awaiting human",
    "error": "Error",
}


def phase_label(phase: AgentPhase | str) -> str:
    """Render any agent phase as a short human label."""
    text = phase.value if isinstance(phase, AgentPhase) else str(phase).strip()
    try:
        return _LABELS[text]
    except KeyError:
        return text.replace("_", " ").strip().capitalize() or "Unknown"