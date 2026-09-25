"""Coverage for ``evomesh.metrics``.
"""

from evomesh.metrics import _sum, mean


def test_sum_adds_the_values() -> None:
    assert _sum([1.0, 2.0, 3.0]) == 6.0


def test_sum_is_correctly_rounded_for_mixed_magnitudes() -> None:
    assert _sum([1e16, 1.0, 1.0]) == 1e16 + 2.0


def test_mean_returns_the_arithmetic_average() -> None:
    assert mean([1.0, 2.0, 3.0]) == 2.0
