"""Human-friendly formatting helpers for EvoMesh."""

from __future__ import annotations

import math
from datetime import datetime

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_MINUTES = 60
_HOURS = 60
_DAYS = 24
_WEEKS = 7


def _safe_float(value) -> float:
    """Coerce ``value`` to a finite float that round-trips safely.

    Raises :class:`ValueError` for ``None`` / non-numeric input, NaN,
    infinities, and out-of-range values (e.g. an ``int`` larger than the
    largest representable ``float``) so they never render as ``"inf"`` /
    ``"nan"`` and never raise an uncaught ``OverflowError`` mid-format.
    """
    if not isinstance(value, (int, float)):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{value!r} is too large to render") from exc
    if not math.isfinite(number):
        raise ValueError(f"{value!r} is not a finite number")
    return number


def humanize_size(num_bytes: int) -> str:
    """Format a byte count using binary (IEC) units."""
    if num_bytes < 0:
        raise ValueError("num_bytes must be non-negative")
    value = _safe_float(num_bytes)
    unit = _UNITS[0]
    while value >= 1024 and unit != _UNITS[-1]:
        value /= 1024
        unit = _UNITS[_UNITS.index(unit) + 1]
    if unit == _UNITS[0]:
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def humanize_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact human string."""
    if seconds is None:
        raise TypeError("seconds must be a number, not None")
    seconds = max(0, _safe_float(seconds))
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < _DAYS * _HOURS * _MINUTES * _WEEKS:
        return f"{seconds // (_DAYS * _HOURS)} days"
    parts = []
    remaining = int(seconds)
    weeks, remaining = divmod(remaining, _DAYS * _HOURS * _MINUTES)
    days, remaining = divmod(remaining, _DAYS * _HOURS)
    hours, remaining = divmod(remaining, _HOURS)
    minutes, secs = divmod(remaining, _MINUTES)
    if weeks:
        parts.append(f"{weeks}w")
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def humanize_bytes(num_bytes: int) -> str:
    """Alias for :func:`humanize_size`, the byte-count formatter."""
    return humanize_size(num_bytes)


def humanize_timestamp(ts: float) -> str:
    """Format an epoch timestamp as a friendly relative description."""
    now = datetime.now().timestamp()
    delta = now - ts
    if delta < 0:
        return "in the future"
    return humanize_duration(delta) + " ago"
