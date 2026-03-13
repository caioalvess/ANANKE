"""Tests for triangular arbitrage detection via Bellman-Ford."""

from datetime import datetime

import pytest

from ananke.models import Ticker
from ananke.triangular import (
    build_graph,
    compute_triangular_all,
    detect_triangular,
)


def _ticker(
    base: str,
    quote: str,
    bid: float,
    ask: float,
    exchange: str = "TestEx",
    volume_quote: float = 100_000.0,
) -> Ticker:
    """Helper to build a Ticker for testing."""
    return Ticker(
        symbol=f"{base}{quote}",
        base_asset=base,
        quote_asset=quote,
        bid=bid,
        ask=ask,
        volume_quote=volume_quote,
        exchange=exchange,
        last_update=datetime(2026, 1, 1),
    )


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_single_pair_creates_two_edges(self) -> None:
        tickers = [_ticker("BTC", "USDT", 100_000, 100_100)]
        nodes, edges = build_graph(tickers, 0.001)
        assert "BTC" in nodes
        assert "USDT" in nodes
        assert len(edges) == 2

    def test_edge_directions(self) -> None:
        tickers = [_ticker("BTC", "USDT", 100_000, 100_100)]
        nodes, edges = build_graph(tickers, 0.0)
        # One edge: USDT→BTC (buy), one: BTC→USDT (sell)
        buy_edge = [e for e in edges if e.side == "buy"][0]
        sell_edge = [e for e in edges if e.side == "sell"][0]
        assert nodes[buy_edge.src] == "USDT"
        assert nodes[buy_edge.dst] == "BTC"
        assert nodes[sell_edge.src] == "BTC"
        assert nodes[sell_edge.dst] == "USDT"

    def test_buy_rate_includes_fee(self) -> None:
        tickers = [_ticker("BTC", "USDT", 100_000, 100_000)]
        _, edges = build_graph(tickers, 0.001)
        buy_edge = [e for e in edges if e.side == "buy"][0]
        # rate = (1/ask) * (1-fee) = (1/100000) * 0.999
        expected = (1.0 / 100_000) * 0.999
        assert abs(buy_edge.rate - expected) < 1e-15

    def test_sell_rate_includes_fee(self) -> None:
        tickers = [_ticker("ETH", "USDT", 4000, 4010)]
        _, edges = build_graph(tickers, 0.001)
        sell_edge = [e for e in edges if e.side == "sell"][0]
        expected = 4000 * 0.999
        assert abs(sell_edge.rate - expected) < 1e-10

    def test_zero_bid_ask_skipped(self) -> None:
        tickers = [_ticker("BTC", "USDT", 0, 0)]
        _, edges = build_graph(tickers, 0.001)
        assert len(edges) == 0

    def test_multiple_pairs_create_shared_nodes(self) -> None:
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_100),
            _ticker("ETH", "USDT", 4000, 4010),
            _ticker("ETH", "BTC", 0.04, 0.0401),
        ]
        nodes, edges = build_graph(tickers, 0.001)
        assert len(nodes) == 3  # BTC, USDT, ETH
        assert len(edges) == 6  # 2 per pair


# ---------------------------------------------------------------------------
# Triangular detection — synthetic cycles
# ---------------------------------------------------------------------------


class TestDetectTriangular:
    def test_profitable_cycle_detected(self) -> None:
        """Create a known profitable cycle: USDT → BTC → ETH → USDT.

        Prices set so the cycle yields ~2.4% gross before fees.
        With 0.1% taker fee (3 legs = 0.3%), net should be ~2.1%.
        """
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),  # tight spread
            _ticker("ETH", "BTC", 0.0400, 0.0400),     # tight spread
            _ticker("ETH", "USDT", 4100, 4100),         # ETH "overpriced" in USDT
        ]
        # Cycle: USDT → BTC (buy BTC at 100K) → ETH (buy ETH at 0.04 BTC)
        #         → USDT (sell ETH at 4100)
        # Product (no fee): (1/100000) * (1/0.04) * 4100 = 1.025
        # With 0.1% per leg: 1.025 * 0.999^3 ≈ 1.02193 → ~2.19%
        opps = detect_triangular(tickers, "TestEx", 0.001)
        assert len(opps) >= 1
        best = opps[0]
        assert best.profit_pct > 2.0
        assert best.profit_pct < 3.0
        assert len(best.path) >= 4  # includes closing node
        assert best.path[0] == best.path[-1]  # cycle closes

    def test_no_cycle_when_fair_prices(self) -> None:
        """Fair prices → no triangular opportunity."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_100),
            _ticker("ETH", "BTC", 0.04, 0.0401),
            _ticker("ETH", "USDT", 4000, 4010),
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        assert len(opps) == 0

    def test_cycle_with_zero_fee(self) -> None:
        """With zero fees, even small mispricing is profitable."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),
            _ticker("ETH", "BTC", 0.04, 0.04),
            _ticker("ETH", "USDT", 4010, 4010),  # slightly overpriced
        ]
        opps = detect_triangular(tickers, "TestEx", 0.0)
        assert len(opps) >= 1
        assert opps[0].profit_pct > 0

    def test_reverse_cycle_detected(self) -> None:
        """The reverse cycle direction should also be detected if profitable.

        USDT → ETH → BTC → USDT (the other direction).
        """
        # Make ETH underpriced vs BTC path
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),
            _ticker("ETH", "BTC", 0.0410, 0.0410),  # ETH overpriced in BTC
            _ticker("ETH", "USDT", 4000, 4000),       # but cheap in USDT
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        # At least one profitable direction
        assert len(opps) >= 1

    def test_min_volume_is_bottleneck(self) -> None:
        """min_volume_quote should be the smallest volume among legs."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000, volume_quote=500_000),
            _ticker("ETH", "BTC", 0.04, 0.04, volume_quote=50_000),
            _ticker("ETH", "USDT", 4100, 4100, volume_quote=200_000),
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        assert len(opps) >= 1
        assert opps[0].min_volume_quote == 50_000

    def test_empty_tickers(self) -> None:
        opps = detect_triangular([], "TestEx", 0.001)
        assert opps == []

    def test_no_hub_currency(self) -> None:
        """If no hub currencies exist, no cycles are found."""
        tickers = [
            _ticker("ABC", "XYZ", 1.0, 1.0),
            _ticker("DEF", "XYZ", 2.0, 2.0),
            _ticker("DEF", "ABC", 2.0, 2.0),
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        assert opps == []

    def test_high_fee_kills_profit(self) -> None:
        """With very high fees, cycle that was profitable becomes unprofitable."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),
            _ticker("ETH", "BTC", 0.04, 0.04),
            _ticker("ETH", "USDT", 4100, 4100),  # 2.5% mispricing
        ]
        # With 1% fee per leg (3%), should eat the ~2.5% profit
        opps = detect_triangular(tickers, "TestEx", 0.01)
        assert len(opps) == 0


# ---------------------------------------------------------------------------
# compute_triangular_all (serialization + multi-exchange)
# ---------------------------------------------------------------------------


class TestComputeTriangularAll:
    def test_serialized_output_format(self) -> None:
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000, exchange="Binance"),
            _ticker("ETH", "BTC", 0.04, 0.04, exchange="Binance"),
            _ticker("ETH", "USDT", 4100, 4100, exchange="Binance"),
        ]
        results = compute_triangular_all(tickers, {"Binance": 0.001})
        assert len(results) >= 1
        r = results[0]
        assert "ex" in r
        assert "path" in r
        assert "pf" in r
        assert "legs" in r
        assert "nlegs" in r
        assert "mvol" in r
        assert r["ex"] == "Binance"
        assert "→" in r["path"]

    def test_exchange_filter(self) -> None:
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000, exchange="Binance"),
            _ticker("ETH", "BTC", 0.04, 0.04, exchange="Binance"),
            _ticker("ETH", "USDT", 4100, 4100, exchange="Binance"),
            _ticker("BTC", "USDT", 100_000, 100_000, exchange="OKX"),
            _ticker("ETH", "BTC", 0.04, 0.04, exchange="OKX"),
            _ticker("ETH", "USDT", 4100, 4100, exchange="OKX"),
        ]
        results = compute_triangular_all(tickers, exchange_filter="OKX")
        assert all(r["ex"] == "OKX" for r in results)

    def test_cross_exchange_tickers_not_mixed(self) -> None:
        """Tickers from different exchanges should not form triangles together."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000, exchange="Binance"),
            _ticker("ETH", "BTC", 0.04, 0.04, exchange="OKX"),  # different exchange
            _ticker("ETH", "USDT", 4100, 4100, exchange="Binance"),
        ]
        results = compute_triangular_all(tickers)
        # No triangle possible because the graph per exchange is incomplete
        assert len(results) == 0

    def test_sorted_by_profit_descending(self) -> None:
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000, exchange="Binance"),
            _ticker("ETH", "BTC", 0.04, 0.04, exchange="Binance"),
            _ticker("ETH", "USDT", 4100, 4100, exchange="Binance"),
            _ticker("SOL", "USDT", 200, 200, exchange="Binance"),
            _ticker("SOL", "BTC", 0.002, 0.002, exchange="Binance"),
        ]
        results = compute_triangular_all(tickers, {"Binance": 0.001})
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i]["pf"] >= results[i + 1]["pf"]

    def test_legs_contain_details(self) -> None:
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000, exchange="Binance"),
            _ticker("ETH", "BTC", 0.04, 0.04, exchange="Binance"),
            _ticker("ETH", "USDT", 4100, 4100, exchange="Binance"),
        ]
        results = compute_triangular_all(tickers, {"Binance": 0.001})
        assert len(results) >= 1
        for leg in results[0]["legs"]:
            assert "f" in leg  # from
            assert "t" in leg  # to
            assert "p" in leg  # pair
            assert "r" in leg  # rate
            assert "sd" in leg  # side


# ---------------------------------------------------------------------------
# Edge cases and cycle properties
# ---------------------------------------------------------------------------


class TestCycleProperties:
    def test_cycle_closes(self) -> None:
        """Every detected cycle must start and end on the same currency."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),
            _ticker("ETH", "BTC", 0.04, 0.04),
            _ticker("ETH", "USDT", 4100, 4100),
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        for opp in opps:
            assert opp.path[0] == opp.path[-1]
            assert len(opp.path) - 1 == len(opp.legs)

    def test_profit_is_positive(self) -> None:
        """All reported opportunities must have positive profit."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),
            _ticker("ETH", "BTC", 0.04, 0.04),
            _ticker("ETH", "USDT", 4100, 4100),
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        for opp in opps:
            assert opp.profit_pct > 0

    def test_max_legs_respected(self) -> None:
        """Cycles should have at most 4 legs."""
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),
            _ticker("ETH", "BTC", 0.04, 0.04),
            _ticker("SOL", "ETH", 0.05, 0.05),
            _ticker("SOL", "USDT", 205, 205),  # mispriced
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        for opp in opps:
            assert len(opp.legs) <= 4

    def test_profit_calculation_accuracy(self) -> None:
        """Verify profit % matches manual calculation."""
        # Exact cycle: USDT → BTC → ETH → USDT
        # rates with 0 fee: 1/100000, 1/0.04, 4100
        # product = (1/100000) * (1/0.04) * 4100 = 1.025
        # with 0.1% per leg: 1.025 * (0.999)^3 = 1.025 * 0.997003 ≈ 1.021928
        # profit ≈ 2.1928%
        tickers = [
            _ticker("BTC", "USDT", 100_000, 100_000),
            _ticker("ETH", "BTC", 0.04, 0.04),
            _ticker("ETH", "USDT", 4100, 4100),
        ]
        opps = detect_triangular(tickers, "TestEx", 0.001)
        assert len(opps) >= 1
        # Find the USDT→BTC→ETH→USDT cycle
        for opp in opps:
            if "USDT" in opp.path and "BTC" in opp.path and "ETH" in opp.path:
                expected = (1.025 * (0.999 ** 3) - 1) * 100
                assert abs(opp.profit_pct - expected) < 0.01
                break
        else:
            pytest.fail("Expected USDT→BTC→ETH cycle not found")
