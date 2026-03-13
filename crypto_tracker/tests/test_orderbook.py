"""Tests for on-demand order book depth probing."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ananke.fee_registry import FeeRegistry
from ananke.orderbook import (
    ExecutionEstimate,
    OrderBookProbe,
    OrderBookSnapshot,
    _native_symbol,
    _parse_depth,
    calculate_execution_price,
)


# ---------------------------------------------------------------------------
# Symbol conversion
# ---------------------------------------------------------------------------


class TestNativeSymbol:
    def test_binance(self) -> None:
        assert _native_symbol("Binance", "BTC", "USDT") == "BTCUSDT"

    def test_bybit(self) -> None:
        assert _native_symbol("Bybit", "ETH", "USDT") == "ETHUSDT"

    def test_okx(self) -> None:
        assert _native_symbol("OKX", "BTC", "USDT") == "BTC-USDT"

    def test_kucoin(self) -> None:
        assert _native_symbol("KuCoin", "SOL", "USDT") == "SOL-USDT"

    def test_gateio(self) -> None:
        assert _native_symbol("Gate.io", "DOGE", "USDT") == "DOGE_USDT"

    def test_kraken_btc_remapped(self) -> None:
        assert _native_symbol("Kraken", "BTC", "USD") == "XBT/USD"

    def test_kraken_other(self) -> None:
        assert _native_symbol("Kraken", "ETH", "USD") == "ETH/USD"

    def test_unknown_exchange(self) -> None:
        assert _native_symbol("FutureExchange", "BTC", "USDT") == "BTCUSDT"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestParseDepth:
    def test_binance(self) -> None:
        data = {
            "bids": [["100.0", "1.5"], ["99.5", "2.0"]],
            "asks": [["100.5", "1.0"], ["101.0", "3.0"]],
        }
        bids, asks = _parse_depth("Binance", data)
        assert bids == [(100.0, 1.5), (99.5, 2.0)]
        assert asks == [(100.5, 1.0), (101.0, 3.0)]

    def test_bybit(self) -> None:
        data = {
            "result": {
                "b": [["50000", "0.1"], ["49990", "0.2"]],
                "a": [["50010", "0.15"]],
            },
        }
        bids, asks = _parse_depth("Bybit", data)
        assert bids == [(50000.0, 0.1), (49990.0, 0.2)]
        assert asks == [(50010.0, 0.15)]

    def test_okx(self) -> None:
        data = {
            "data": [{
                "bids": [["100", "5", "0", "3"]],
                "asks": [["101", "4", "0", "2"]],
            }],
        }
        bids, asks = _parse_depth("OKX", data)
        assert bids == [(100.0, 5.0)]
        assert asks == [(101.0, 4.0)]

    def test_kucoin(self) -> None:
        data = {
            "data": {
                "bids": [["200", "10"]],
                "asks": [["201", "8"]],
            },
        }
        bids, asks = _parse_depth("KuCoin", data)
        assert bids == [(200.0, 10.0)]
        assert asks == [(201.0, 8.0)]

    def test_gateio(self) -> None:
        data = {
            "bids": [["0.5", "1000"]],
            "asks": [["0.51", "900"]],
        }
        bids, asks = _parse_depth("Gate.io", data)
        assert bids == [(0.5, 1000.0)]
        assert asks == [(0.51, 900.0)]

    def test_kraken(self) -> None:
        data = {
            "result": {
                "XXBTZUSD": {
                    "bids": [["60000", "0.5", 1234567890]],
                    "asks": [["60010", "0.3", 1234567890]],
                },
            },
        }
        bids, asks = _parse_depth("Kraken", data)
        assert bids == [(60000.0, 0.5)]
        assert asks == [(60010.0, 0.3)]

    def test_empty_data(self) -> None:
        bids, asks = _parse_depth("Binance", {})
        assert bids == []
        assert asks == []

    def test_unknown_exchange(self) -> None:
        bids, asks = _parse_depth("Unknown", {"bids": [], "asks": []})
        assert bids == []
        assert asks == []


# ---------------------------------------------------------------------------
# VWAP walk-through
# ---------------------------------------------------------------------------


class TestExecutionPrice:
    def test_single_level_full_fill(self) -> None:
        """$500 buy on a level with $1000 available."""
        asks = [(100.0, 10.0)]  # 10 units @ $100 = $1000 available
        est = calculate_execution_price(asks, 500.0, "buy")
        assert est.effective_price == 100.0
        assert est.slippage_pct == 0.0
        assert est.filled_amount_quote == 500.0
        assert est.depth_available_quote == 1000.0
        assert est.levels_consumed == 1

    def test_multi_level_buy(self) -> None:
        """Buy $150: $100 at level 1, $50 at level 2."""
        asks = [(100.0, 1.0), (110.0, 1.0)]  # $100 + $110 = $210 avail
        est = calculate_execution_price(asks, 150.0, "buy")
        # Level 1: buy $100 worth → 1.0 unit @ 100
        # Level 2: buy $50 worth → 50/110 ≈ 0.4545 units @ 110
        total_qty = 1.0 + 50.0 / 110.0
        effective = 150.0 / total_qty
        assert est.effective_price == pytest.approx(effective, rel=1e-6)
        assert est.slippage_pct == pytest.approx(
            (effective - 100.0) / 100.0 * 100, rel=1e-6,
        )
        assert est.filled_amount_quote == 150.0
        assert est.levels_consumed == 2

    def test_multi_level_sell(self) -> None:
        """Sell $150 worth: bids descending."""
        bids = [(100.0, 1.0), (90.0, 1.0)]  # $100 + $90 = $190 avail
        est = calculate_execution_price(bids, 150.0, "sell")
        # Level 1: sell $100 worth → 1.0 unit @ 100
        # Level 2: sell $50 worth → 50/90 ≈ 0.5556 units @ 90
        total_qty = 1.0 + 50.0 / 90.0
        effective = 150.0 / total_qty
        slippage = (100.0 - effective) / 100.0 * 100
        assert est.effective_price == pytest.approx(effective, rel=1e-6)
        assert est.slippage_pct == pytest.approx(slippage, rel=1e-6)
        assert est.filled_amount_quote == 150.0

    def test_partial_fill(self) -> None:
        """Not enough depth to fill the full order."""
        asks = [(100.0, 0.5)]  # only $50 available
        est = calculate_execution_price(asks, 500.0, "buy")
        assert est.filled_amount_quote == 50.0
        assert est.depth_available_quote == 50.0

    def test_empty_levels(self) -> None:
        est = calculate_execution_price([], 1000.0, "buy")
        assert est.effective_price == 0.0
        assert est.levels_consumed == 0

    def test_zero_amount(self) -> None:
        asks = [(100.0, 1.0)]
        est = calculate_execution_price(asks, 0.0, "buy")
        assert est.effective_price == 0.0

    def test_zero_price_level_skipped(self) -> None:
        """Zero-price at top of book means invalid snapshot → returns zeros."""
        asks = [(0.0, 100.0), (100.0, 1.0)]
        est = calculate_execution_price(asks, 50.0, "buy")
        assert est.effective_price == 0.0
        assert est.levels_consumed == 0

    def test_zero_price_mid_book_skipped(self) -> None:
        """Zero-price level in middle of book is skipped during walk."""
        asks = [(100.0, 1.0), (0.0, 5.0), (110.0, 1.0)]
        est = calculate_execution_price(asks, 200.0, "buy")
        # Fills $100 at level 1, skips level 2, fills $100 at level 3
        assert est.levels_consumed == 2
        assert est.filled_amount_quote == 200.0

    def test_exact_fill_at_boundary(self) -> None:
        """Trade size exactly matches first level."""
        asks = [(50.0, 2.0), (55.0, 3.0)]  # $100, $165
        est = calculate_execution_price(asks, 100.0, "buy")
        assert est.effective_price == 50.0
        assert est.slippage_pct == 0.0
        assert est.levels_consumed == 1
        assert est.depth_available_quote == 265.0

    def test_large_book_depth_counted(self) -> None:
        """Total depth includes levels beyond the fill."""
        asks = [(10.0, 1.0), (11.0, 1.0), (12.0, 1.0), (13.0, 1.0)]
        est = calculate_execution_price(asks, 10.0, "buy")
        # Fill only level 1
        assert est.levels_consumed == 1
        # Total depth = 10 + 11 + 12 + 13 = 46
        assert est.depth_available_quote == 46.0


# ---------------------------------------------------------------------------
# OrderBookProbe (with mocked HTTP)
# ---------------------------------------------------------------------------


def _mock_session_get(response_data, status=200):
    """Create a mock context manager for aiohttp session.get."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=response_data)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


class TestOrderBookProbe:
    @pytest.fixture
    def probe(self) -> OrderBookProbe:
        return OrderBookProbe()

    @pytest.mark.asyncio
    async def test_fetch_depth_binance(self, probe: OrderBookProbe) -> None:
        binance_resp = {
            "bids": [["60000", "0.5"], ["59990", "1.0"]],
            "asks": [["60010", "0.3"], ["60020", "0.8"]],
        }
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=_mock_session_get(binance_resp))

        probe._session = mock_session
        snap = await probe.fetch_depth("Binance", "BTC", "USDT")

        assert snap is not None
        assert snap.exchange == "Binance"
        assert len(snap.bids) == 2
        assert len(snap.asks) == 2
        assert snap.bids[0] == (60000.0, 0.5)
        assert snap.asks[0] == (60010.0, 0.3)

    @pytest.mark.asyncio
    async def test_fetch_depth_caching(self, probe: OrderBookProbe) -> None:
        resp = {"bids": [["100", "1"]], "asks": [["101", "1"]]}
        mock_session = MagicMock()
        mock_session.closed = False
        call_count = 0

        def counting_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _mock_session_get(resp)

        mock_session.get = counting_get
        probe._session = mock_session

        # First call: actual fetch
        snap1 = await probe.fetch_depth("Binance", "BTC", "USDT")
        assert snap1 is not None
        assert call_count == 1

        # Second call: should hit cache
        snap2 = await probe.fetch_depth("Binance", "BTC", "USDT")
        assert snap2 is snap1
        assert call_count == 1  # no new HTTP call

    @pytest.mark.asyncio
    async def test_fetch_depth_http_error(self, probe: OrderBookProbe) -> None:
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=_mock_session_get({}, status=429))
        probe._session = mock_session

        snap = await probe.fetch_depth("Binance", "BTC", "USDT")
        assert snap is None

    @pytest.mark.asyncio
    async def test_fetch_depth_unknown_exchange(self, probe: OrderBookProbe) -> None:
        snap = await probe.fetch_depth("UnknownExchange", "BTC", "USDT")
        assert snap is None

    @pytest.mark.asyncio
    async def test_enrich_adds_depth_fields(self, probe: OrderBookProbe) -> None:
        """Enrich sets ex1k and mdq on arb results."""
        results = [{
            "s": "BTCUSDT", "b": "BTC", "q": "USDT",
            "bx": "Binance", "ax": "Kraken",
            "bi": 60100.0, "ak": 60000.0,
            "pf": 0.167, "npf": 0.1,
        }]

        # Mock fetch_depth to return synthetic snapshots
        async def mock_fetch(exchange, base, quote):
            if exchange == "Kraken":
                return OrderBookSnapshot(
                    exchange="Kraken", symbol="XBT/USDT",
                    bids=[], asks=[(60000.0, 1.0), (60010.0, 1.0)],
                )
            return OrderBookSnapshot(
                exchange="Binance", symbol="BTCUSDT",
                bids=[(60100.0, 1.0), (60090.0, 1.0)], asks=[],
            )

        probe.fetch_depth = mock_fetch
        await probe.enrich_arb_results(results, trade_size=1000.0)

        assert results[0]["ex1k"] is not None
        assert results[0]["mdq"] is not None
        assert isinstance(results[0]["ex1k"], float)

    @pytest.mark.asyncio
    async def test_enrich_failed_fetch_sets_none(
        self, probe: OrderBookProbe,
    ) -> None:
        results = [{
            "s": "BTCUSDT", "b": "BTC", "q": "USDT",
            "bx": "Binance", "ax": "Kraken",
            "bi": 100.0, "ak": 99.0, "pf": 1.0, "npf": 0.8,
        }]

        async def mock_fetch(exchange, base, quote):
            return None

        probe.fetch_depth = mock_fetch
        await probe.enrich_arb_results(results, trade_size=1000.0)

        assert results[0]["ex1k"] is None
        assert results[0]["mdq"] is None

    @pytest.mark.asyncio
    async def test_enrich_non_top_n_get_none_fields(
        self, probe: OrderBookProbe,
    ) -> None:
        """Results outside top_n still get ex1k/mdq fields (as None)."""
        results = [
            {"s": f"T{i}USDT", "b": f"T{i}", "q": "USDT",
             "bx": "Binance", "ax": "Kraken",
             "bi": 100.0 + i, "ak": 99.0, "pf": float(i), "npf": float(i)}
            for i in range(5)
        ]

        async def mock_fetch(exchange, base, quote):
            return None

        probe.fetch_depth = mock_fetch
        await probe.enrich_arb_results(results, top_n=2, trade_size=1000.0)

        # All results should have the fields
        for r in results:
            assert "ex1k" in r
            assert "mdq" in r

    @pytest.mark.asyncio
    async def test_enrich_with_fees(self, probe: OrderBookProbe) -> None:
        """Enrichment uses taker fees from FeeRegistry."""
        results = [{
            "s": "BTCUSDT", "b": "BTC", "q": "USDT",
            "bx": "Binance", "ax": "Kraken",
            "bi": 60100.0, "ak": 60000.0,
            "pf": 0.167, "npf": 0.1,
        }]

        async def mock_fetch(exchange, base, quote):
            if exchange == "Kraken":
                return OrderBookSnapshot(
                    exchange="Kraken", symbol="XBT/USDT",
                    bids=[], asks=[(60000.0, 1.0)],
                )
            return OrderBookSnapshot(
                exchange="Binance", symbol="BTCUSDT",
                bids=[(60100.0, 1.0)], asks=[],
            )

        probe.fetch_depth = mock_fetch
        fees = FeeRegistry(
            taker={"Binance": 0.001, "Kraken": 0.004},
            withdrawal={},
        )
        await probe.enrich_arb_results(results, trade_size=1000.0, fees=fees)

        # With fees, exec profit should be less than gross profit
        assert results[0]["ex1k"] is not None
        assert results[0]["ex1k"] < results[0]["pf"]

    @pytest.mark.asyncio
    async def test_enrich_empty_results(self, probe: OrderBookProbe) -> None:
        results = []
        await probe.enrich_arb_results(results, trade_size=1000.0)
        assert results == []

    @pytest.mark.asyncio
    async def test_close(self, probe: OrderBookProbe) -> None:
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        probe._session = mock_session
        await probe.close()
        mock_session.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Integration: execution-adjusted profit vs top-of-book
# ---------------------------------------------------------------------------


class TestExecProfitCalculation:
    def test_slippage_eats_profit(self) -> None:
        """Thin book: slippage eliminates the arb profit."""
        # Top of book: ask=100, bid=101 → ~1% gross profit
        # But ask book has only $50 at 100, rest at 105
        asks = [(100.0, 0.5), (105.0, 10.0)]
        bids = [(101.0, 0.5), (96.0, 10.0)]

        buy_est = calculate_execution_price(asks, 1000.0, "buy")
        sell_est = calculate_execution_price(bids, 1000.0, "sell")

        # VWAP buy will be much higher than 100
        assert buy_est.effective_price > 104
        # VWAP sell will be much lower than 101
        assert sell_est.effective_price < 97
        # Net: buy high, sell low → negative profit
        exec_pf = (sell_est.effective_price - buy_est.effective_price) / buy_est.effective_price * 100
        assert exec_pf < 0

    def test_deep_book_preserves_profit(self) -> None:
        """Thick book: spread preserved after walking the book."""
        asks = [(100.0, 100.0)]  # $10,000 at best ask
        bids = [(101.0, 100.0)]  # $10,100 at best bid

        buy_est = calculate_execution_price(asks, 1000.0, "buy")
        sell_est = calculate_execution_price(bids, 1000.0, "sell")

        assert buy_est.effective_price == 100.0
        assert sell_est.effective_price == 101.0
        exec_pf = (101.0 - 100.0) / 100.0 * 100
        assert exec_pf == pytest.approx(1.0)
