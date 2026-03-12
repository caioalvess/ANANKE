"""Tests for ananke.display formatting functions."""

from ananke.display import fmt_int, fmt_price, fmt_volume


def test_fmt_price_zero() -> None:
    assert fmt_price(0) == "\u2014"


def test_fmt_price_large() -> None:
    result = fmt_price(67_500.0)
    assert "67,500.00" in result


def test_fmt_price_small() -> None:
    result = fmt_price(0.00000123)
    assert "0.00000123" in result


def test_fmt_price_mid() -> None:
    result = fmt_price(3.4567)
    assert "3.4567" in result


def test_fmt_volume_billions() -> None:
    assert "B" in fmt_volume(2_500_000_000)


def test_fmt_volume_millions() -> None:
    assert "M" in fmt_volume(45_000_000)


def test_fmt_volume_thousands() -> None:
    assert "K" in fmt_volume(12_500)


def test_fmt_volume_zero() -> None:
    assert fmt_volume(0) == "\u2014"


def test_fmt_int_zero() -> None:
    assert fmt_int(0) == "\u2014"


def test_fmt_int_large() -> None:
    assert "1,234,567" in fmt_int(1_234_567)
