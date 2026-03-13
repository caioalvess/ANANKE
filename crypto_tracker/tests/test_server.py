"""Tests for web server filtering and arbitrage logic."""

from ananke.coin_registry import CoinRegistry
from ananke.config import ArbitrageConfig
from ananke.fee_registry import FeeRegistry
from ananke.models import Ticker
from ananke.web.server import (
    _compute_arbitrage,
    _filter_arbitrage,
    _filter_tickers,
    _serialize_ticker,
)


def _make_ticker(
    symbol: str,
    exchange: str,
    quote: str = "USDT",
    bid: float = 0.0,
    ask: float = 0.0,
    volume_quote: float = 1000.0,
    price: float = 100.0,
) -> Ticker:
    return Ticker(
        symbol=symbol,
        base_asset=symbol.replace(quote, ""),
        quote_asset=quote,
        price=price,
        bid=bid,
        ask=ask,
        volume_quote=volume_quote,
        exchange=exchange,
    )


# --- Ticker filter tests ---


def test_serialize_ticker_keys() -> None:
    t = _make_ticker("BTCUSDT", "Binance")
    result = _serialize_ticker(t)
    assert result["s"] == "BTCUSDT"
    assert result["ex"] == "Binance"
    assert "p" in result
    assert "sp" in result


def test_filter_by_exchange() -> None:
    tickers = [
        _make_ticker("BTCUSDT", "Binance"),
        _make_ticker("ETHUSDT", "Bybit"),
    ]
    result = _filter_tickers(tickers, "Binance", "USDT")
    assert len(result) == 1
    assert result[0]["s"] == "BTCUSDT"


def test_filter_by_quote() -> None:
    tickers = [
        _make_ticker("BTCUSDT", "Binance", "USDT"),
        _make_ticker("ETHBTC", "Binance", "BTC"),
    ]
    result = _filter_tickers(tickers, "Binance", "USDT")
    assert len(result) == 1
    assert result[0]["s"] == "BTCUSDT"


def test_filter_all_quotes() -> None:
    tickers = [
        _make_ticker("BTCUSDT", "Binance", "USDT"),
        _make_ticker("ETHBTC", "Binance", "BTC"),
    ]
    result = _filter_tickers(tickers, "Binance", "ALL")
    assert len(result) == 2


def test_filter_empty() -> None:
    result = _filter_tickers([], "Binance", "USDT")
    assert result == []


# --- Arbitrage engine tests (no registry = graceful degradation) ---


def test_arb_basic_opportunity() -> None:
    """BTC on Binance bid=100, on Kraken ask=99 → profit ~1.01%."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0),
    ]
    result = _compute_arbitrage(tickers)
    assert len(result) == 1
    opp = result[0]
    assert opp["b"] == "BTC"
    assert opp["bx"] == "Binance"  # best bid
    assert opp["ax"] == "Kraken"   # best ask
    assert opp["bi"] == 100.0
    assert opp["ak"] == 99.0
    assert opp["pf"] > 0


def test_arb_no_opportunity_same_exchange() -> None:
    """Single exchange → no cross-exchange arb possible."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=99.0),
    ]
    result = _compute_arbitrage(tickers)
    assert len(result) == 0


def test_arb_no_profit() -> None:
    """Best bid < best ask → no opportunity."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=99.0, ask=100.0),
        _make_ticker("BTCUSDT", "Kraken", bid=98.5, ask=99.5),
    ]
    result = _compute_arbitrage(tickers)
    assert len(result) == 0


def test_arb_skips_zero_bid_ask() -> None:
    """Tickers with 0 bid or ask are excluded."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=0.0),
        _make_ticker("BTCUSDT", "Kraken", bid=0.0, ask=99.0),
    ]
    result = _compute_arbitrage(tickers)
    assert len(result) == 0


def test_arb_multiple_exchanges() -> None:
    """Best bid and ask picked correctly from 3 exchanges."""
    tickers = [
        _make_ticker("ETHUSDT", "Binance", bid=3000.0, ask=3005.0),
        _make_ticker("ETHUSDT", "OKX", bid=2990.0, ask=2995.0),
        _make_ticker("ETHUSDT", "Kraken", bid=3010.0, ask=3008.0),
    ]
    result = _compute_arbitrage(tickers)
    assert len(result) == 1
    opp = result[0]
    # Best bid=3010 (Kraken), best ask=2995 (OKX)
    assert opp["bx"] == "Kraken"
    assert opp["ax"] == "OKX"
    assert opp["bi"] == 3010.0
    assert opp["ak"] == 2995.0


def test_arb_different_quotes_separate() -> None:
    """Same base but different quotes are separate opportunities."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", "USDT", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "Kraken", "USDT", bid=98.0, ask=99.0),
        _make_ticker("BTCETH", "Binance", "ETH", bid=50.0, ask=50.5),
        _make_ticker("BTCETH", "Kraken", "ETH", bid=48.0, ask=49.0),
    ]
    result = _compute_arbitrage(tickers)
    assert len(result) == 2


# --- Arbitrage with registry: global confirmation ---


def _registry_with_globals() -> CoinRegistry:
    """Registry where BTC, ETH, SOL are globally confirmed."""
    return CoinRegistry(
        global_confirmed={"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"},
        kucoin_confirmed={},
        ambiguous=frozenset({"U", "LIT", "LUNA"}),
    )


def test_arb_global_confirmed_matched() -> None:
    """Globally confirmed symbol is matched across ALL exchanges."""
    tickers = [
        _make_ticker("ETHUSDT", "Binance", bid=3015.0, ask=3020.0),
        _make_ticker("ETHUSDT", "OKX", bid=2990.0, ask=3000.0),
    ]
    result = _compute_arbitrage(tickers, registry=_registry_with_globals())
    assert len(result) == 1
    assert result[0]["pf"] == round((3015.0 - 3000.0) / 3000.0 * 100, 4)


def test_arb_ambiguous_symbol_excluded() -> None:
    """Ambiguous symbol is excluded on ALL exchanges."""
    tickers = [
        _make_ticker("UUSDT", "Binance", bid=0.9997, ask=1.0),
        _make_ticker("UUSDT", "Bybit", bid=0.0008, ask=0.000871),
    ]
    result = _compute_arbitrage(tickers, registry=_registry_with_globals())
    assert len(result) == 0


def test_arb_unknown_symbol_excluded() -> None:
    """Symbol not in registry at all (unknown) is excluded."""
    tickers = [
        _make_ticker("FAKUSDT", "Binance", bid=10.0, ask=10.5),
        _make_ticker("FAKUSDT", "Kraken", bid=9.0, ask=9.5),
    ]
    result = _compute_arbitrage(tickers, registry=_registry_with_globals())
    assert len(result) == 0


def test_arb_large_spread_not_capped() -> None:
    """Legitimate high-spread arb is never filtered by profit amount."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=150.0, ask=155.0),
        _make_ticker("BTCUSDT", "Kraken", bid=95.0, ask=100.0),
    ]
    result = _compute_arbitrage(tickers, registry=_registry_with_globals())
    assert len(result) == 1
    assert result[0]["pf"] == round((150.0 - 100.0) / 100.0 * 100, 4)  # 50%


def test_arb_exchange_blocked_symbol() -> None:
    """Symbol blocked on a specific exchange via name cross-validation."""
    registry = CoinRegistry(
        global_confirmed={"VRA": "verasity", "BTC": "bitcoin"},
        kucoin_confirmed={},
        ambiguous=frozenset(),
        exchange_blocked=frozenset({("Gate.io", "VRA")}),
    )
    tickers = [
        _make_ticker("VRAUSDT", "KuCoin", bid=0.0001, ask=0.00011),
        _make_ticker("VRAUSDT", "Gate.io", bid=0.00002, ask=0.000021),
    ]
    result = _compute_arbitrage(tickers, registry=registry)
    # Gate.io VRA blocked → not grouped with KuCoin VRA → no arb
    assert len(result) == 0


def test_arb_exchange_blocked_doesnt_affect_others() -> None:
    """Exchange block is specific — same symbol on other exchanges still works."""
    registry = CoinRegistry(
        global_confirmed={"VRA": "verasity"},
        kucoin_confirmed={},
        ambiguous=frozenset(),
        exchange_blocked=frozenset({("Gate.io", "VRA")}),
    )
    tickers = [
        _make_ticker("VRAUSDT", "Binance", bid=0.00011, ask=0.000115),
        _make_ticker("VRAUSDT", "KuCoin", bid=0.0001, ask=0.000105),
    ]
    result = _compute_arbitrage(tickers, registry=registry)
    # Binance and KuCoin both resolve — arb found
    assert len(result) == 1


# --- Arbitrage with registry: KuCoin-specific confirmation ---


def _registry_with_kucoin() -> CoinRegistry:
    """Registry where LUNA is confirmed on KuCoin only (via fullName)."""
    return CoinRegistry(
        global_confirmed={"BTC": "bitcoin", "ETH": "ethereum"},
        kucoin_confirmed={"LUNA": "terra-luna-2", "LIT": "litentry"},
        ambiguous=frozenset({"LUNA", "LIT"}),
    )


def test_arb_kucoin_confirmed_not_on_other_exchanges() -> None:
    """LUNA confirmed on KuCoin but NOT on Binance → no cross-exchange arb."""
    tickers = [
        _make_ticker("LUNAUSDT", "Binance", bid=1.50, ask=1.55),
        _make_ticker("LUNAUSDT", "Bybit", bid=0.80, ask=0.85),
    ]
    # LUNA is ambiguous on Binance and Bybit (no fullName) → both excluded
    result = _compute_arbitrage(tickers, registry=_registry_with_kucoin())
    assert len(result) == 0


def test_arb_kucoin_confirmed_kucoin_side_enters() -> None:
    """LUNA on KuCoin resolves correctly, but Binance side is blocked."""
    tickers = [
        _make_ticker("LUNAUSDT", "KuCoin", bid=1.50, ask=1.55),
        _make_ticker("LUNAUSDT", "Binance", bid=0.80, ask=0.85),
    ]
    # KuCoin LUNA → terra-luna-2 (confirmed). Binance LUNA → None (blocked).
    # Only one side enters → no cross-exchange arb possible.
    result = _compute_arbitrage(tickers, registry=_registry_with_kucoin())
    assert len(result) == 0


def test_arb_kucoin_to_kucoin_not_cross_exchange() -> None:
    """Two KuCoin tickers with same kucoin-confirmed symbol — same exchange, no arb."""
    tickers = [
        _make_ticker("LUNAUSDT", "KuCoin", bid=1.50, ask=1.55),
        _make_ticker("LUNAUSDT", "KuCoin", bid=0.80, ask=0.85),
    ]
    result = _compute_arbitrage(tickers, registry=_registry_with_kucoin())
    assert len(result) == 0


def test_arb_global_still_works_with_kucoin_registry() -> None:
    """Globally confirmed symbols work across all exchanges even in mixed registry."""
    tickers = [
        _make_ticker("ETHUSDT", "Binance", bid=3015.0, ask=3020.0),
        _make_ticker("ETHUSDT", "KuCoin", bid=2990.0, ask=3000.0),
    ]
    result = _compute_arbitrage(tickers, registry=_registry_with_kucoin())
    assert len(result) == 1
    assert result[0]["b"] == "ETH"


# --- Graceful degradation ---


def test_arb_empty_registry_allows_everything() -> None:
    """Empty registry = CoinGecko unavailable → all symbols allowed,
    including extreme spreads (graceful degradation, no profit cap)."""
    tickers = [
        _make_ticker("UUSDT", "Binance", bid=0.9997, ask=1.0),
        _make_ticker("UUSDT", "Bybit", bid=0.0008, ask=0.000871),
    ]
    result = _compute_arbitrage(tickers, registry=CoinRegistry.empty())
    assert len(result) == 1  # accepted — registry was unavailable


def test_arb_no_registry_matches_everything() -> None:
    """No registry (None) = same as empty, all symbols allowed."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0),
    ]
    result = _compute_arbitrage(tickers, registry=None)
    assert len(result) == 1


def test_arb_only_ambiguous_registry_blocks() -> None:
    """Registry with only ambiguous entries (no confirmed) still blocks."""
    tickers = [
        _make_ticker("UUSDT", "Binance", bid=0.9997, ask=1.0),
        _make_ticker("UUSDT", "Bybit", bid=0.0008, ask=0.000871),
    ]
    registry = CoinRegistry(
        global_confirmed={},
        kucoin_confirmed={},
        ambiguous=frozenset({"U"}),
    )
    result = _compute_arbitrage(tickers, registry=registry)
    assert len(result) == 0


# --- Arbitrage filter tests ---


def test_filter_arb_by_quote() -> None:
    arb = [
        {"s": "BTCUSDT", "b": "BTC", "q": "USDT", "bx": "Binance", "ax": "Kraken",
         "bi": 100, "ak": 99, "pf": 1.0, "bv": 1000, "av": 900},
        {"s": "ETHBTC", "b": "ETH", "q": "BTC", "bx": "OKX", "ax": "Binance",
         "bi": 0.05, "ak": 0.049, "pf": 2.0, "bv": 500, "av": 400},
    ]
    result = _filter_arbitrage(arb, [], "USDT")
    assert len(result) == 1
    assert result[0]["q"] == "USDT"


def test_filter_arb_by_single_exchange() -> None:
    """Single exchange selected: show opps where either side matches."""
    arb = [
        {"s": "BTCUSDT", "b": "BTC", "q": "USDT", "bx": "Binance", "ax": "Kraken",
         "bi": 100, "ak": 99, "pf": 1.0, "bv": 1000, "av": 900},
        {"s": "ETHUSDT", "b": "ETH", "q": "USDT", "bx": "OKX", "ax": "KuCoin",
         "bi": 3000, "ak": 2990, "pf": 0.3, "bv": 500, "av": 400},
    ]
    result = _filter_arbitrage(arb, ["Binance"], "ALL")
    assert len(result) == 1
    assert result[0]["bx"] == "Binance"


def test_filter_arb_by_multi_exchange_pair() -> None:
    """2 exchanges selected: show only opps where BOTH sides are in the set."""
    arb = [
        {"s": "BTCUSDT", "b": "BTC", "q": "USDT", "bx": "Binance", "ax": "Kraken",
         "bi": 100, "ak": 99, "pf": 1.0, "bv": 1000, "av": 900},
        {"s": "ETHUSDT", "b": "ETH", "q": "USDT", "bx": "OKX", "ax": "KuCoin",
         "bi": 3000, "ak": 2990, "pf": 0.3, "bv": 500, "av": 400},
        {"s": "SOLUSDT", "b": "SOL", "q": "USDT", "bx": "Binance", "ax": "OKX",
         "bi": 150, "ak": 149, "pf": 0.5, "bv": 800, "av": 700},
    ]
    # Binance + Kraken: only BTC (Binance↔Kraken)
    result = _filter_arbitrage(arb, ["Binance", "Kraken"], "ALL")
    assert len(result) == 1
    assert result[0]["s"] == "BTCUSDT"

    # Binance + OKX: only SOL (Binance↔OKX)
    result = _filter_arbitrage(arb, ["Binance", "OKX"], "ALL")
    assert len(result) == 1
    assert result[0]["s"] == "SOLUSDT"

    # All three: BTC + SOL (both have Binance, paired with Kraken/OKX)
    result = _filter_arbitrage(arb, ["Binance", "Kraken", "OKX"], "ALL")
    assert len(result) == 2


def test_filter_arb_empty_exchanges_all() -> None:
    """Empty exchange list: no filter, show everything."""
    arb = [
        {"s": "BTCUSDT", "b": "BTC", "q": "USDT", "bx": "Binance", "ax": "Kraken",
         "bi": 100, "ak": 99, "pf": 1.0, "bv": 1000, "av": 900},
        {"s": "ETHBTC", "b": "ETH", "q": "BTC", "bx": "OKX", "ax": "Binance",
         "bi": 0.05, "ak": 0.049, "pf": 2.0, "bv": 500, "av": 400},
    ]
    result = _filter_arbitrage(arb, [], "ALL")
    assert len(result) == 2


# --- Fee-adjusted arbitrage tests ---


def _fee_registry() -> FeeRegistry:
    """Fee registry with known taker fees and a BTC withdrawal fee."""
    return FeeRegistry(
        taker={"Binance": 0.001, "Kraken": 0.004, "OKX": 0.001, "KuCoin": 0.001},
        withdrawal={},
        fallback_withdrawal={"BTC": 0.0005, "ETH": 0.005},
    )


def test_arb_npf_less_than_pf() -> None:
    """Net profit after taker fees is always less than gross profit."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0),
    ]
    result = _compute_arbitrage(tickers, fees=_fee_registry())
    assert len(result) == 1
    opp = result[0]
    assert "npf" in opp
    assert "wf" in opp
    assert "tnpf" in opp
    assert opp["npf"] < opp["pf"]


def test_arb_npf_calculation_same_taker() -> None:
    """Verify net profit calculation with equal taker fees on both sides."""
    # Binance taker 0.1%, OKX taker 0.1%
    # bid=1010, ask=1000
    # gross = (1010-1000)/1000 * 100 = 1.0%
    # buy_cost = 1000 * 1.001 = 1001
    # sell_rev = 1010 * 0.999 = 1008.99
    # net = (1008.99 - 1001) / 1001 * 100 = 0.7982%
    tickers = [
        _make_ticker("ETHUSDT", "Binance", bid=1010.0, ask=1015.0),
        _make_ticker("ETHUSDT", "OKX", bid=1005.0, ask=1000.0),
    ]
    fees = _fee_registry()
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 1
    opp = result[0]
    expected_npf = fees.net_profit_after_taker(
        bid=1010.0, ask=1000.0,
        bid_exchange="Binance", ask_exchange="OKX",
    )
    assert opp["npf"] == round(expected_npf, 4)


def test_arb_npf_with_kraken_higher_fee() -> None:
    """Kraken's higher taker fee (0.4%) significantly reduces net profit."""
    # Binance bid=100, Kraken ask=99
    # gross = (100-99)/99 * 100 = 1.0101%
    # buy_cost = 99 * 1.004 = 99.396 (Kraken 0.4%)
    # sell_rev = 100 * 0.999 = 99.9 (Binance 0.1%)
    # net = (99.9 - 99.396) / 99.396 * 100 = 0.5072%
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0),
    ]
    fees = _fee_registry()
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 1
    opp = result[0]
    # Sell on Binance (0.1%), buy on Kraken (0.4%)
    assert opp["bx"] == "Binance"
    assert opp["ax"] == "Kraken"
    assert opp["npf"] < opp["pf"]
    # Kraken's 0.4% should eat ~0.5% of the gross profit
    assert opp["npf"] < opp["pf"] - 0.4


def test_arb_wf_in_quote_currency() -> None:
    """Withdrawal fee is expressed in quote currency (fee * bid price).

    Uses bid because withdrawal fee = base units you can't sell,
    and selling happens at bid price on the target exchange.
    """
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=60100.0, ask=60200.0),
        _make_ticker("BTCUSDT", "Kraken", bid=59800.0, ask=60000.0),
    ]
    fees = _fee_registry()
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 1
    opp = result[0]
    # BTC withdrawal fee = 0.0005 BTC, bid = 60100 (sell side)
    # wf = 0.0005 * 60100 = 30.05
    assert opp["wf"] == 30.05


def test_arb_npf_negative_after_fees() -> None:
    """Small gross profit can become negative after taker fees."""
    # Tiny spread: bid=100.1, ask=100.0 → gross ~0.1%
    # After 0.1% + 0.1% taker → net should be negative
    tickers = [
        _make_ticker("ETHUSDT", "Binance", bid=100.1, ask=100.2),
        _make_ticker("ETHUSDT", "OKX", bid=99.9, ask=100.0),
    ]
    result = _compute_arbitrage(tickers, fees=_fee_registry())
    assert len(result) == 1
    assert result[0]["pf"] > 0   # gross is positive
    assert result[0]["npf"] < 0  # net is negative after fees


def test_arb_no_fees_npf_equals_pf() -> None:
    """Without fee registry, npf equals pf and tnpf equals npf."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0),
    ]
    result = _compute_arbitrage(tickers)
    assert len(result) == 1
    assert result[0]["npf"] == result[0]["pf"]
    assert result[0]["tnpf"] == result[0]["npf"]
    assert result[0]["wf"] == 0.0


# --- Transfer status (executable arb filter) tests ---


def _fee_registry_with_blocks(
    withdraw_blocked: frozenset[tuple[str, str]] = frozenset(),
    deposit_blocked: frozenset[tuple[str, str]] = frozenset(),
) -> FeeRegistry:
    return FeeRegistry(
        taker={"Binance": 0.001, "Kraken": 0.004, "KuCoin": 0.001, "Gate.io": 0.002},
        withdrawal={},
        fallback_withdrawal={"BTC": 0.0005},
        withdraw_blocked=withdraw_blocked,
        deposit_blocked=deposit_blocked,
    )


def test_arb_withdraw_blocked_on_ask_exchange() -> None:
    """Arb filtered when withdrawal is blocked on the ask (buy) exchange."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "KuCoin", bid=98.0, ask=99.0),
    ]
    fees = _fee_registry_with_blocks(
        withdraw_blocked=frozenset({("KuCoin", "BTC")}),
    )
    # Buy on KuCoin (ask), sell on Binance (bid) — need to withdraw from KuCoin
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 0


def test_arb_deposit_blocked_on_bid_exchange() -> None:
    """Arb filtered when deposit is blocked on the bid (sell) exchange."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "KuCoin", bid=98.0, ask=99.0),
    ]
    fees = _fee_registry_with_blocks(
        deposit_blocked=frozenset({("Binance", "BTC")}),
    )
    # Buy on KuCoin, deposit to Binance — deposit blocked on Binance
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 0


def test_arb_withdraw_blocked_on_bid_doesnt_filter() -> None:
    """Withdraw blocked on BID exchange doesn't affect arb (irrelevant direction)."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "KuCoin", bid=98.0, ask=99.0),
    ]
    fees = _fee_registry_with_blocks(
        withdraw_blocked=frozenset({("Binance", "BTC")}),
    )
    # Withdraw blocked on Binance (bid side) — doesn't matter, we sell there
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 1


def test_arb_deposit_blocked_on_ask_doesnt_filter() -> None:
    """Deposit blocked on ASK exchange doesn't affect arb (irrelevant direction)."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "KuCoin", bid=98.0, ask=99.0),
    ]
    fees = _fee_registry_with_blocks(
        deposit_blocked=frozenset({("KuCoin", "BTC")}),
    )
    # Deposit blocked on KuCoin (ask side) — doesn't matter, we buy there
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 1


def test_arb_no_transfer_blocks_passes() -> None:
    """No transfer blocks → arb passes through normally."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "KuCoin", bid=98.0, ask=99.0),
    ]
    fees = _fee_registry_with_blocks()
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 1


def test_arb_both_directions_blocked() -> None:
    """Both withdraw on ask + deposit on bid blocked → filtered."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "KuCoin", bid=98.0, ask=99.0),
    ]
    fees = _fee_registry_with_blocks(
        withdraw_blocked=frozenset({("KuCoin", "BTC")}),
        deposit_blocked=frozenset({("Binance", "BTC")}),
    )
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 0


def test_can_execute_arb_method() -> None:
    """Direct test of FeeRegistry.can_execute_arb method."""
    fees = _fee_registry_with_blocks(
        withdraw_blocked=frozenset({("KuCoin", "VRA")}),
        deposit_blocked=frozenset({("Gate.io", "DIN")}),
    )
    # VRA withdraw blocked on KuCoin
    assert not fees.can_execute_arb("Binance", "KuCoin", "VRA")
    assert fees.can_execute_arb("KuCoin", "Binance", "VRA")  # bid side, irrelevant
    # DIN deposit blocked on Gate.io
    assert not fees.can_execute_arb("Gate.io", "Binance", "DIN")
    assert fees.can_execute_arb("Binance", "Gate.io", "DIN")  # ask side, irrelevant
    # BTC — no blocks
    assert fees.can_execute_arb("Binance", "KuCoin", "BTC")


# --- Arbitrage quality filters (ArbitrageConfig) ---


def test_arb_min_volume_filters_low_liquidity() -> None:
    """Tickers with low volume are excluded before grouping."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=12),
    ]
    cfg = ArbitrageConfig(min_volume_quote=10_000)
    result = _compute_arbitrage(tickers, arb_config=cfg)
    # Kraken side has only $12 volume → excluded → no arb
    assert len(result) == 0


def test_arb_min_volume_both_sides_pass() -> None:
    """Both sides above min volume → arb passes."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=20_000),
    ]
    cfg = ArbitrageConfig(min_volume_quote=10_000)
    result = _compute_arbitrage(tickers, arb_config=cfg)
    assert len(result) == 1


def test_arb_max_spread_filters_illiquid_pair() -> None:
    """Pair with wide bid-ask spread is excluded as illiquid."""
    # Spread = (105 - 100) / 100 = 5%
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=110.0, ask=110.5),
        _make_ticker("BTCUSDT", "Kraken", bid=100.0, ask=105.0),
    ]
    cfg = ArbitrageConfig(max_pair_spread_pct=3.0)
    result = _compute_arbitrage(tickers, arb_config=cfg)
    # Kraken spread 5% > max 3% → excluded → no arb
    assert len(result) == 0


def test_arb_max_spread_tight_pairs_pass() -> None:
    """Pairs with tight spread pass the filter."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=50_000),
    ]
    cfg = ArbitrageConfig(max_pair_spread_pct=5.0)
    result = _compute_arbitrage(tickers, arb_config=cfg)
    assert len(result) == 1


def test_arb_min_profit_filters_low_profit() -> None:
    """Opportunities below min profit threshold are excluded."""
    # profit = (100 - 99) / 99 * 100 ≈ 1.01%
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=50_000),
    ]
    cfg = ArbitrageConfig(min_profit_pct=2.0)
    result = _compute_arbitrage(tickers, arb_config=cfg)
    assert len(result) == 0


def test_arb_min_profit_high_profit_passes() -> None:
    """Opportunities above min profit threshold pass."""
    # profit = (105 - 99) / 99 * 100 ≈ 6.06%
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=105.0, ask=105.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=50_000),
    ]
    cfg = ArbitrageConfig(min_profit_pct=5.0)
    result = _compute_arbitrage(tickers, arb_config=cfg)
    assert len(result) == 1
    assert result[0]["pf"] > 5.0


def test_arb_no_config_no_filtering() -> None:
    """Without ArbitrageConfig, no quality filters applied (backward compat)."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=5),
    ]
    # No arb_config → default behavior, $5 volume passes
    result = _compute_arbitrage(tickers)
    assert len(result) == 1


def test_arb_default_config_filters_low_volume() -> None:
    """Default ArbitrageConfig (min_volume=10K) filters low-vol pairs."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=5),
    ]
    cfg = ArbitrageConfig()  # defaults: min_volume=10K, max_spread=5%
    result = _compute_arbitrage(tickers, arb_config=cfg)
    assert len(result) == 0


def test_arb_combined_filters() -> None:
    """Multiple filters applied together."""
    tickers = [
        # Good: high vol, tight spread, profitable
        _make_ticker("BTCUSDT", "Binance", bid=105.0, ask=105.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=30_000),
        # Bad: low volume on Kraken side
        _make_ticker("ETHUSDT", "Binance", bid=3010.0, ask=3015.0, volume_quote=50_000),
        _make_ticker("ETHUSDT", "Kraken", bid=2990.0, ask=3000.0, volume_quote=500),
    ]
    cfg = ArbitrageConfig(min_volume_quote=10_000, min_profit_pct=1.0)
    result = _compute_arbitrage(tickers, arb_config=cfg)
    assert len(result) == 1
    assert result[0]["b"] == "BTC"


# --- True net profit (tnpf) tests ---


def test_arb_tnpf_accounts_for_withdrawal_fee() -> None:
    """tnpf = npf - (wf / ref_trade_size) * 100."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=60100.0, ask=60200.0, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=59800.0, ask=60000.0, volume_quote=50_000),
    ]
    fees = _fee_registry()
    cfg = ArbitrageConfig(ref_trade_size=1000.0)
    result = _compute_arbitrage(tickers, fees=fees, arb_config=cfg)
    assert len(result) == 1
    opp = result[0]
    # wf = 0.0005 * 60100 = 30.05
    # tnpf = npf - (30.05 / 1000) * 100 = npf - 3.005
    expected_tnpf = round(opp["npf"] - (opp["wf"] / 1000.0) * 100, 4)
    assert opp["tnpf"] == expected_tnpf
    assert opp["tnpf"] < opp["npf"]


def test_arb_tnpf_equals_npf_when_no_wf() -> None:
    """When withdrawal fee is 0, tnpf equals npf."""
    tickers = [
        _make_ticker("FAKUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("FAKUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=50_000),
    ]
    fees = _fee_registry()  # FAK not in fallback → wf=0
    cfg = ArbitrageConfig(ref_trade_size=1000.0)
    result = _compute_arbitrage(tickers, fees=fees, arb_config=cfg)
    assert len(result) == 1
    assert result[0]["wf"] == 0.0
    assert result[0]["tnpf"] == result[0]["npf"]


def test_arb_tnpf_larger_trade_size_reduces_impact() -> None:
    """Larger ref_trade_size reduces withdrawal fee impact on tnpf."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=60100.0, ask=60200.0, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=59800.0, ask=60000.0, volume_quote=50_000),
    ]
    fees = _fee_registry()
    small = ArbitrageConfig(ref_trade_size=1000.0)
    large = ArbitrageConfig(ref_trade_size=10000.0)
    r_small = _compute_arbitrage(tickers, fees=fees, arb_config=small)
    r_large = _compute_arbitrage(tickers, fees=fees, arb_config=large)
    # Larger trade → less impact → higher tnpf
    assert r_large[0]["tnpf"] > r_small[0]["tnpf"]
    # Both should have same npf
    assert r_small[0]["npf"] == r_large[0]["npf"]


# --- Per-exchange withdrawal fee tests ---


def test_arb_per_exchange_withdrawal_fee() -> None:
    """Exchange-specific withdrawal fee takes priority over fallback."""
    fees = FeeRegistry(
        taker={"Binance": 0.001, "Kraken": 0.004},
        withdrawal={("Kraken", "BTC"): 0.0001},  # Kraken-specific
        fallback_withdrawal={"BTC": 0.0005},      # generic fallback
    )
    # Withdrawal is from ask exchange (Kraken)
    assert fees.withdrawal_fee("BTC", "Kraken") == 0.0001
    # Binance has no exchange-specific → falls back to 0.0005
    assert fees.withdrawal_fee("BTC", "Binance") == 0.0005


def test_arb_wf_uses_ask_exchange_fee() -> None:
    """wf in arb result uses ask exchange's withdrawal fee."""
    fees = FeeRegistry(
        taker={"Binance": 0.001, "Kraken": 0.004},
        withdrawal={("Kraken", "BTC"): 0.0001},
        fallback_withdrawal={"BTC": 0.0005},
    )
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=60100.0, ask=60200.0, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=59800.0, ask=60000.0, volume_quote=50_000),
    ]
    result = _compute_arbitrage(tickers, fees=fees)
    assert len(result) == 1
    # Ask exchange is Kraken → 0.0001 BTC * 60100 bid = 6.01
    assert result[0]["wf"] == round(0.0001 * 60100.0, 8)


# --- Hedge mode tests ---


def test_hedge_mode_no_withdrawal_fee() -> None:
    """Hedge mode: wf is always 0, tnpf equals npf."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=60100.0, ask=60200.0, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=59800.0, ask=60000.0, volume_quote=50_000),
    ]
    fees = _fee_registry()
    result = _compute_arbitrage(tickers, fees=fees, mode="hedge")
    assert len(result) == 1
    opp = result[0]
    assert opp["wf"] == 0.0
    assert opp["tnpf"] == opp["npf"]


def test_hedge_mode_has_rebal_cost() -> None:
    """Hedge mode: rc (rebal cost) shows withdrawal fee for informational use."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=60100.0, ask=60200.0, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=59800.0, ask=60000.0, volume_quote=50_000),
    ]
    fees = _fee_registry()
    result = _compute_arbitrage(tickers, fees=fees, mode="hedge")
    assert len(result) == 1
    opp = result[0]
    # rc = BTC withdrawal fee (0.0005) * bid price (60100)
    assert opp["rc"] == round(0.0005 * 60100.0, 8)
    assert opp["rc"] > 0


def test_hedge_mode_skips_can_execute_arb() -> None:
    """Hedge mode: transfer blocks are ignored (trader doesn't transfer)."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "KuCoin", bid=98.0, ask=99.0, volume_quote=50_000),
    ]
    fees = _fee_registry_with_blocks(
        withdraw_blocked=frozenset({("KuCoin", "BTC")}),
    )
    # Transfer mode: blocked
    result_transfer = _compute_arbitrage(tickers, fees=fees, mode="transfer")
    assert len(result_transfer) == 0
    # Hedge mode: passes through
    result_hedge = _compute_arbitrage(tickers, fees=fees, mode="hedge")
    assert len(result_hedge) == 1


def test_hedge_mode_npf_same_as_transfer() -> None:
    """Hedge and transfer modes compute the same npf (taker-fee-only profit)."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=50_000),
    ]
    fees = _fee_registry()
    r_hedge = _compute_arbitrage(tickers, fees=fees, mode="hedge")
    r_transfer = _compute_arbitrage(tickers, fees=fees, mode="transfer")
    assert len(r_hedge) == 1
    assert len(r_transfer) == 1
    assert r_hedge[0]["npf"] == r_transfer[0]["npf"]


def test_hedge_mode_has_min_side_volume() -> None:
    """Both modes include msv (min side volume) field."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0, volume_quote=20_000),
    ]
    r_hedge = _compute_arbitrage(tickers, mode="hedge")
    assert len(r_hedge) == 1
    assert r_hedge[0]["msv"] == 20_000

    r_transfer = _compute_arbitrage(tickers, mode="transfer")
    assert len(r_transfer) == 1
    assert r_transfer[0]["msv"] == 20_000


def test_transfer_mode_rc_equals_wf() -> None:
    """In transfer mode, rc equals wf (same withdrawal cost)."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=60100.0, ask=60200.0, volume_quote=50_000),
        _make_ticker("BTCUSDT", "Kraken", bid=59800.0, ask=60000.0, volume_quote=50_000),
    ]
    fees = _fee_registry()
    result = _compute_arbitrage(tickers, fees=fees, mode="transfer")
    assert len(result) == 1
    assert result[0]["rc"] == result[0]["wf"]


def test_hedge_mode_no_fees_rc_zero() -> None:
    """Hedge without fee registry: rc is 0."""
    tickers = [
        _make_ticker("BTCUSDT", "Binance", bid=100.0, ask=100.5),
        _make_ticker("BTCUSDT", "Kraken", bid=98.0, ask=99.0),
    ]
    result = _compute_arbitrage(tickers, mode="hedge")
    assert len(result) == 1
    assert result[0]["rc"] == 0.0
    assert result[0]["wf"] == 0.0
    assert result[0]["tnpf"] == result[0]["npf"]
