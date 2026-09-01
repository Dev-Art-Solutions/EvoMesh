"""Human-friendly formatting helpers for EvoMesh."""

from __future__ import annotations

from datetime import datetime

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_MINUTES = 60
_HOURS = 60
_DAYS = 24
_WEEKS = 7


def humanize_size(num_bytes: int) -> str:
    """Format a byte count using binary (IEC) units."""
    if num_bytes < 0:
        raise ValueError("num_bytes must be non-negative")
    value = float(num_bytes)
    unit = _UNITS[0]
    while value >= 1024 and unit != _UNITS[-1]:
        value /= 1024
        unit = _UNITS[_UNITS.index(unit) + 1]
    if unit == _UNITS[0]:
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def humanize_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact human string."""
    seconds = max(0, float(seconds))
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
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
    return " ".join(parts[:3])


def humanize_timestamp(ts: float) -> str:
    """Format an epoch timestamp as a friendly relative description."""
    now = datetime.now().timestamp()
    delta = now - ts
    if delta < 0:
        return "in the future"
    return humanize_duration(delta) + " ago"
