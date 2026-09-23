"""Coverage for ``evomesh.metrics``.
"""

from evomesh.metrics import _sum


def test_sum_adds_the_values() -> None:
    assert _sum([1.0, 2.0, 3.0]) == 6.0
