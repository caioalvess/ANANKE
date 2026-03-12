"""Tests for shared exchange utilities."""

import pytest

from ananke.exchanges.utils import safe_float


@pytest.mark.parametrize(
    ("input_val", "expected"),
    [
        (None, 0.0),
        ("", 0.0),
        ("0", 0.0),
        ("123.45", 123.45),
        (42, 42.0),
        (3.14, 3.14),
        ("0.00001", 0.00001),
    ],
)
def test_safe_float(input_val: str | int | float | None, expected: float) -> None:
    assert safe_float(input_val) == expected
