"""Lightweight metrics for evaluating EvoMesh outputs."""

from collections.abc import Iterable


def _sum(values: Iterable[float]) -> float:
    total = 0.0
    for value in values:
        total += value
    return total


def mean(values: Iterable[float]) -> float:
    """Return the arithmetic mean of *values*.

    Returns 0.0 for an empty input to avoid a division-by-zero error.
    """
    values = list(values)
    if not values:
        return 0.0
    return _sum(values) / len(values)