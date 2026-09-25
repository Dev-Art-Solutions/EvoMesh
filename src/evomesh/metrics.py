"""Lightweight metrics for evaluating EvoMesh outputs."""

import math
from collections.abc import Iterable


def _sum(values: Iterable[float]) -> float:
    return math.fsum(values)


def mean(values: Iterable[float]) -> float:
    """Return the arithmetic mean of *values*.

    Returns 0.0 for an empty input to avoid a division-by-zero error.
    """
    values = list(values)
    if not values:
        return 0.0
    return _sum(values) / len(values)