"""A minimal 5-field cron expression matcher.

No dependency: this is deliberately not `croniter`. Rule 16 in CLAUDE.md caps
runtime dependencies at five, and computing "the next matching wall-clock
time" from `minute hour day month weekday` is small enough to own outright.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

FIELD_NAMES = ("minute", "hour", "day of month", "month", "day of week")
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_SEARCH_LIMIT_DAYS = 4 * 366 + 1


class InvalidCronError(ValueError):
    """Raised for a cron expression that cannot be parsed or never matches."""


def _parse_field(field: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) <= 0:
                raise InvalidCronError(f"bad step in {name} field: {field!r}")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            if not (start_text.isdigit() and end_text.isdigit()):
                raise InvalidCronError(f"bad range in {name} field: {field!r}")
            start, end = int(start_text), int(end_text)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise InvalidCronError(f"unrecognised {name} field: {field!r}")
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise InvalidCronError(f"{name} field {field!r} out of range {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


def parse(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a standard 5-field cron expression: minute hour day month weekday."""
    fields = expression.split()
    if len(fields) != 5:
        raise InvalidCronError(
            f"expected 5 fields ({' '.join(FIELD_NAMES)}), got {len(fields)}: {expression!r}"
        )
    minute, hour, day, month, weekday = (
        _parse_field(field, name, low, high)
        for field, name, (low, high) in zip(fields, FIELD_NAMES, _FIELD_RANGES, strict=True)
    )
    # 7 is Sunday, same as 0, in the day-of-week field.
    weekday = {value % 7 for value in weekday}
    return minute, hour, day, month, weekday


def _day_matches(
    day: datetime, day_set: set[int], weekday_set: set[int], *, dom_wild: bool, dow_wild: bool
) -> bool:
    dom_ok = day.day in day_set
    dow_ok = ((day.weekday() + 1) % 7) in weekday_set
    # Standard (if surprising) cron rule: when both fields are restricted they
    # are OR'd together, not AND'd; a wildcard field simply drops out.
    if dom_wild and dow_wild:
        return True
    if dom_wild:
        return dow_ok
    if dow_wild:
        return dom_ok
    return dom_ok or dow_ok


def next_after(expression: str, after: datetime) -> datetime:
    """The next moment the expression matches, strictly later than `after`."""
    minute_set, hour_set, day_set, month_set, weekday_set = parse(expression)
    fields = expression.split()
    dom_wild, dow_wild = fields[2] == "*", fields[4] == "*"
    times_of_day = sorted(itertools.product(sorted(hour_set), sorted(minute_set)))
    if not times_of_day:
        raise InvalidCronError(f"cron expression matches no time of day: {expression!r}")

    start = after.replace(second=0, microsecond=0)
    minute_of_day = start.hour * 60 + start.minute

    for offset in range(_SEARCH_LIMIT_DAYS):
        day = start + timedelta(days=offset)
        if day.month not in month_set:
            continue
        if not _day_matches(day, day_set, weekday_set, dom_wild=dom_wild, dow_wild=dow_wild):
            continue
        for hour, minute in times_of_day:
            if offset == 0 and hour * 60 + minute <= minute_of_day:
                continue
            return day.replace(hour=hour, minute=minute)
    raise InvalidCronError(f"cron expression never matches within four years: {expression!r}")
