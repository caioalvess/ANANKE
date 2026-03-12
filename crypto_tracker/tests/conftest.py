"""Shared fixtures for ANANKE tests."""

import pytest

from ananke.config import AppConfig, BinanceConfig, DisplayConfig, WebConfig
from ananke.models import Ticker


@pytest.fixture
def sample_ticker() -> Ticker:
    """A realistic BTC/USDT ticker for testing."""
    return Ticker(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        price=67_500.00,
        price_change=1_250.00,
        price_change_pct=1.88,
        high_24h=68_200.00,
        low_24h=65_800.00,
        volume_base=12_345.67,
        volume_quote=833_456_789.00,
        bid=67_499.50,
        ask=67_500.50,
        open_price=66_250.00,
        trades_count=1_234_567,
        exchange="Binance",
    )


@pytest.fixture
def sample_tickers() -> list[Ticker]:
    """A set of diverse tickers for testing filters and sorting."""
    return [
        Ticker(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
            price=67_500.0, volume_quote=833_000_000.0, price_change_pct=1.88,
        ),
        Ticker(
            symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT",
            price=3_450.0, volume_quote=420_000_000.0, price_change_pct=-2.15,
        ),
        Ticker(
            symbol="ETHBTC", base_asset="ETH", quote_asset="BTC",
            price=0.0511, volume_quote=1_200.0, price_change_pct=0.45,
        ),
        Ticker(
            symbol="BNBUSDT", base_asset="BNB", quote_asset="USDT",
            price=580.0, volume_quote=95_000_000.0, price_change_pct=0.0,
        ),
        Ticker(
            symbol="SOLUSDT", base_asset="SOL", quote_asset="USDT",
            price=145.0, volume_quote=310_000_000.0, price_change_pct=5.23,
        ),
    ]


@pytest.fixture
def test_config() -> AppConfig:
    """Config with test-safe defaults."""
    return AppConfig(
        binance=BinanceConfig(rest_timeout_sec=5),
        web=WebConfig(host="127.0.0.1", port=0),
        display=DisplayConfig(page_size=10, refresh_ms=100),
        log_level="DEBUG",
    )
