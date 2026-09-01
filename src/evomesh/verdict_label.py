"""Render a validation verdict as a short human label."""

from __future__ import annotations

VERDICT_LABELS: dict[str, str] = {
    "passed": "PASS",
    "failed": "FAIL",
    "pending": "PENDING",
    "unknown": "UNKNOWN",
}


def verdict_label(verdict: str | None) -> str:
    """Return a short human-readable label for a validation verdict.

    Unknown or missing verdicts fall back to a neutral PENDING label.
    """
    if verdict is None:
        return "PENDING"
    key = verdict.strip().lower()
    if not key:
        return "PENDING"
    return VERDICT_LABELS.get(key, key.upper())