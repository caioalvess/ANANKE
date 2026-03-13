"""Tests for in-memory metrics collector."""

import pytest

from ananke.metrics import MetricsCollector, _opp_key, _opp_label


def _opp(
    base: str = "BTC",
    quote: str = "USDT",
    ask_ex: str = "Binance",
    bid_ex: str = "Bybit",
    profit: float = 1.5,
) -> dict:
    """Build a minimal arb opportunity dict for testing."""
    return {
        "s": f"{base}{quote}",
        "b": base,
        "q": quote,
        "pf": profit,
        "ax": ask_ex,
        "bx": bid_ex,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestOppKey:
    def test_basic(self) -> None:
        opp = _opp()
        assert _opp_key(opp) == "BTC_USDT_Binance_Bybit"

    def test_different_pair(self) -> None:
        opp = _opp(base="ETH", quote="BTC", ask_ex="OKX", bid_ex="Kraken")
        assert _opp_key(opp) == "ETH_BTC_OKX_Kraken"


class TestOppLabel:
    def test_roundtrip(self) -> None:
        key = "BTC_USDT_Binance_Bybit"
        label = _opp_label(key)
        assert label == {"b": "BTC", "q": "USDT", "ax": "Binance", "bx": "Bybit"}

    def test_fallback(self) -> None:
        label = _opp_label("bad")
        assert label["b"] == "bad"
        assert label["q"] == ""


# ---------------------------------------------------------------------------
# MetricsCollector — recording
# ---------------------------------------------------------------------------


class TestRecord:
    def test_empty_results(self) -> None:
        mc = MetricsCollector()
        mc.record([])
        assert len(mc._buffer) == 1
        assert len(mc._buffer[0].opps) == 0

    def test_records_snapshots(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(), _opp(base="ETH")])
        assert len(mc._buffer) == 1
        assert len(mc._buffer[0].opps) == 2

    def test_tracks_active_since(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        assert "BTC_USDT_Binance_Bybit" in mc._active_since

    def test_removes_gone_keys(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        assert "BTC_USDT_Binance_Bybit" in mc._active_since

        mc.record([])  # BTC gone
        assert "BTC_USDT_Binance_Bybit" not in mc._active_since

    def test_buffer_maxlen(self) -> None:
        mc = MetricsCollector(buffer_size=5)
        for _ in range(10):
            mc.record([_opp()])
        assert len(mc._buffer) == 5

    def test_multiple_cycles(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(profit=1.0)])
        mc.record([_opp(profit=2.0)])
        mc.record([_opp(profit=3.0)])
        assert len(mc._buffer) == 3
        # Latest should have profit=3.0
        latest = mc._buffer[-1]
        profits = [s.profit for s in latest.opps]
        assert profits == [3.0]


# ---------------------------------------------------------------------------
# Window entries
# ---------------------------------------------------------------------------


class TestWindowEntries:
    def test_empty_buffer(self) -> None:
        mc = MetricsCollector()
        assert mc._window_entries() == []

    def test_all_within_window(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        mc.record([_opp()])
        entries = mc._window_entries(window_sec=300)
        assert len(entries) == 2

    def test_outside_window_excluded(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        # Manually backdate the first entry
        mc._buffer[0].ts -= 400
        mc.record([_opp()])
        entries = mc._window_entries(window_sec=300)
        assert len(entries) == 1


# ---------------------------------------------------------------------------
# Pair stats
# ---------------------------------------------------------------------------


class TestPairStats:
    def test_empty(self) -> None:
        mc = MetricsCollector()
        assert mc.get_pair_stats() == {}

    def test_counts_occurrences(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(profit=1.0)])
        mc.record([_opp(profit=2.0)])
        mc.record([_opp(profit=3.0)])
        stats = mc.get_pair_stats()
        key = "BTC_USDT_Binance_Bybit"
        assert key in stats
        assert stats[key]["count"] == 3

    def test_profit_avg(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(profit=1.0)])
        mc.record([_opp(profit=3.0)])
        stats = mc.get_pair_stats()
        key = "BTC_USDT_Binance_Bybit"
        assert stats[key]["profit_avg"] == pytest.approx(2.0)

    def test_profit_max(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(profit=1.0)])
        mc.record([_opp(profit=5.0)])
        mc.record([_opp(profit=2.0)])
        stats = mc.get_pair_stats()
        assert stats["BTC_USDT_Binance_Bybit"]["profit_max"] == 5.0

    def test_multiple_pairs(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(base="BTC"), _opp(base="ETH")])
        stats = mc.get_pair_stats()
        assert len(stats) == 2


# ---------------------------------------------------------------------------
# Pair frequency
# ---------------------------------------------------------------------------


class TestPairFreq:
    def test_zero_when_absent(self) -> None:
        mc = MetricsCollector()
        mc.record([])
        assert mc.get_pair_freq("nope") == 0

    def test_counts_appearances(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        mc.record([_opp()])
        mc.record([])  # absent
        mc.record([_opp()])
        assert mc.get_pair_freq("BTC_USDT_Binance_Bybit") == 3


# ---------------------------------------------------------------------------
# Active duration
# ---------------------------------------------------------------------------


class TestActiveDuration:
    def test_zero_when_not_active(self) -> None:
        mc = MetricsCollector()
        assert mc.get_active_duration("nope") == 0.0

    def test_positive_when_active(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        dur = mc.get_active_duration("BTC_USDT_Binance_Bybit")
        assert dur >= 0.0

    def test_resets_when_gone(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        mc.record([])  # gone
        assert mc.get_active_duration("BTC_USDT_Binance_Bybit") == 0.0


# ---------------------------------------------------------------------------
# get_metrics (global)
# ---------------------------------------------------------------------------


class TestGetMetrics:
    def test_empty_collector(self) -> None:
        mc = MetricsCollector()
        m = mc.get_metrics()
        assert m["global"]["total_now"] == 0
        assert m["global"]["total_5m"] == 0
        assert m["pairs"] == []
        assert m["window_sec"] == 300

    def test_with_data(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(profit=2.0), _opp(base="ETH", profit=1.0)])
        mc.record([_opp(profit=3.0)])

        m = mc.get_metrics()
        assert m["global"]["total_now"] == 1  # only BTC in latest
        assert m["global"]["total_5m"] == 2  # BTC + ETH seen in window
        assert m["global"]["avg_spread"] == 3.0  # only latest snapshot

    def test_pairs_sorted_by_freq(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(base="BTC"), _opp(base="ETH")])
        mc.record([_opp(base="BTC")])  # BTC appears twice, ETH once
        m = mc.get_metrics()
        pairs = m["pairs"]
        assert len(pairs) == 2
        assert pairs[0]["b"] == "BTC"
        assert pairs[0]["freq"] == 2
        assert pairs[1]["b"] == "ETH"
        assert pairs[1]["freq"] == 1

    def test_top_exchanges(self) -> None:
        mc = MetricsCollector()
        mc.record([
            _opp(ask_ex="Binance", bid_ex="Bybit"),
            _opp(base="ETH", ask_ex="Binance", bid_ex="OKX"),
        ])
        m = mc.get_metrics()
        exs = {e["ex"]: e["count"] for e in m["global"]["top_exchanges"]}
        # Binance appears in both opps (as ask), count = 2
        assert exs["Binance"] == 2
        assert exs["Bybit"] == 1
        assert exs["OKX"] == 1

    def test_buffer_sec(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        mc.record([_opp()])
        m = mc.get_metrics()
        assert m["buffer_sec"] >= 0.0

    def test_pair_fields(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp(profit=1.5)])
        m = mc.get_metrics()
        p = m["pairs"][0]
        assert p["key"] == "BTC_USDT_Binance_Bybit"
        assert p["b"] == "BTC"
        assert p["q"] == "USDT"
        assert p["ax"] == "Binance"
        assert p["bx"] == "Bybit"
        assert p["spread_avg"] == 1.5
        assert p["active"] is True

    def test_max_20_pairs(self) -> None:
        mc = MetricsCollector()
        opps = [_opp(base=f"T{i}") for i in range(30)]
        mc.record(opps)
        m = mc.get_metrics()
        assert len(m["pairs"]) == 20


# ---------------------------------------------------------------------------
# Enrich arb results
# ---------------------------------------------------------------------------


class TestEnrichArbResults:
    def test_adds_freq_and_dur(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        mc.record([_opp()])

        results = [_opp()]
        mc.enrich_arb_results(results)
        assert "freq" in results[0]
        assert "dur" in results[0]
        assert results[0]["freq"] == 2

    def test_zero_for_unknown(self) -> None:
        mc = MetricsCollector()
        mc.record([])

        results = [_opp(base="UNKNOWN")]
        mc.enrich_arb_results(results)
        assert results[0]["freq"] == 0
        assert results[0]["dur"] == 0.0

    def test_dur_positive_for_active(self) -> None:
        mc = MetricsCollector()
        mc.record([_opp()])
        results = [_opp()]
        mc.enrich_arb_results(results)
        assert results[0]["dur"] >= 0.0
