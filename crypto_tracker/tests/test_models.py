"""Tests for ananke.models module."""

from ananke.models import Ticker


def test_ticker_spread(sample_ticker: Ticker) -> None:
    expected = ((67_500.50 - 67_499.50) / 67_499.50) * 100
    assert abs(sample_ticker.spread - expected) < 1e-6


def test_ticker_spread_zero_bid() -> None:
    t = Ticker(symbol="X", base_asset="X", quote_asset="Y", bid=0, ask=1)
    assert t.spread == 0.0


def test_ticker_amplitude(sample_ticker: Ticker) -> None:
    expected = ((68_200.0 - 65_800.0) / 65_800.0) * 100
    assert abs(sample_ticker.amplitude - expected) < 1e-6


def test_ticker_amplitude_zero_low() -> None:
    t = Ticker(symbol="X", base_asset="X", quote_asset="Y", low_24h=0, high_24h=100)
    assert t.amplitude == 0.0
