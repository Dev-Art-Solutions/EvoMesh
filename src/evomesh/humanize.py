"""Human-friendly formatting helpers for EvoMesh.

This module provides small, dependency-free helpers that turn raw
machine values into short strings suitable for logs and human reports.
"""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact human label.

    Non-negative whole-second units are shown from largest to smallest,
    dropping leading zero-width units.

    Examples
    --------
    >>> format_duration(0)
    '0s'
    >>> format_duration(65)
    '1m 5s'
    >>> format_duration(3661)
    '1h 1m 1s'

    Raises
    ------
    ValueError
        If ``seconds`` is negative.
    """
    if seconds < 0:
        raise ValueError("duration must be non-negative")

    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)