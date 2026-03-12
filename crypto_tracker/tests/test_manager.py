"""Tests for ananke.exchanges.manager module."""

import pytest

from ananke.exchanges.base import Exchange
from ananke.exchanges.manager import ExchangeManager
from ananke.models import Ticker


class MockExchange(Exchange):
    """Minimal Exchange implementation for testing."""

    def __init__(self, name: str, tickers: dict[str, Ticker] | None = None) -> None:
        super().__init__(name)
        if tickers:
            self.tickers = tickers

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def fetch_exchange_info(self) -> None:
        pass


@pytest.fixture
def manager() -> ExchangeManager:
    t1 = Ticker(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", price=67000, exchange="ExA")
    t2 = Ticker(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT", price=3400, exchange="ExA")
    t3 = Ticker(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", price=67050, exchange="ExB")

    ex_a = MockExchange("ExA", {"BTCUSDT": t1, "ETHUSDT": t2})
    ex_b = MockExchange("ExB", {"BTCUSDT": t3})

    return ExchangeManager([ex_a, ex_b])


def test_exchange_names(manager: ExchangeManager) -> None:
    assert manager.exchange_names == ["ExA", "ExB"]


def test_get_all_tickers(manager: ExchangeManager) -> None:
    tickers = manager.get_all_tickers()
    assert len(tickers) == 3


def test_get_exchange_tickers(manager: ExchangeManager) -> None:
    assert len(manager.get_exchange_tickers("ExA")) == 2
    assert len(manager.get_exchange_tickers("ExB")) == 1
    assert len(manager.get_exchange_tickers("Unknown")) == 0


def test_has_data(manager: ExchangeManager) -> None:
    assert manager.has_data()


def test_has_data_empty() -> None:
    m = ExchangeManager([MockExchange("Empty")])
    assert not m.has_data()


def test_total_symbols(manager: ExchangeManager) -> None:
    assert manager.total_symbols() == 3


def test_get_exchange(manager: ExchangeManager) -> None:
    assert manager.get_exchange("ExA") is not None
    assert manager.get_exchange("ExA").name == "ExA"
    assert manager.get_exchange("Nope") is None
