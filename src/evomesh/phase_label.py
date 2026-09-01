"""Render an agent phase as a short human label."""

from __future__ import annotations

from enum import StrEnum


class AgentPhase(StrEnum):
    """Well-known agent phases in EvoMesh."""

    IDLE = "idle"
    PLANNING = "planning"
    THINKING = "thinking"
    ACTING = "acting"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    AWAITING_HUMAN = "awaiting_human"
    DONE = "done"

    def label(self) -> str:
        """Return a short human-readable label for this phase."""
        return _LABELS[self.value]


_LABELS: dict[str, str] = {
    "idle": "Idle",
    "planning": "Planning",
    "thinking": "Thinking",
    "acting": "Acting",
    "validating": "Validating",
    "repairing": "Repairing",
    "awaiting_human": "Awaiting human",
    "done": "Done",
}


def phase_label(phase: AgentPhase | str) -> str:
    """Render any agent phase as a short human label."""
    text = phase.value if isinstance(phase, AgentPhase) else str(phase).strip()
    try:
        return _LABELS[text]
    except KeyError:
        return text.replace("_", " ").strip().capitalize() or "Unknown"