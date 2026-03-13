"""On-demand L2 order book probing for arbitrage depth analysis.

Fetches order book snapshots only for opportunities that pass initial
filters — avoids streaming thousands of order books.  Results are
cached for 3 seconds to avoid redundant requests within the same
broadcast cycle.

VWAP walk-through: for a given trade size in quote currency, walks
the order book levels to calculate the effective execution price
and slippage vs top-of-book.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

from ananke.exchanges.utils import safe_float

logger = logging.getLogger(__name__)

_CACHE_TTL = 3.0  # seconds
_MAX_CONCURRENT_PER_EXCHANGE = 5
_FETCH_TIMEOUT = 3.0  # seconds per request
_ENRICH_TIMEOUT = 2.0  # global timeout for enrichment pass


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class OrderBookSnapshot:
    """L2 order book snapshot from a single exchange."""

    exchange: str
    symbol: str
    bids: list[tuple[float, float]]  # [(price, qty), ...] descending price
    asks: list[tuple[float, float]]  # [(price, qty), ...] ascending price
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ExecutionEstimate:
    """Result of walking through the order book for a given trade size."""

    effective_price: float  # VWAP through the book
    slippage_pct: float  # vs top-of-book price
    filled_amount_quote: float  # how much actually fillable
    depth_available_quote: float  # total available in book
    levels_consumed: int  # how many levels used


# ---------------------------------------------------------------------------
# Symbol conversion per exchange
# ---------------------------------------------------------------------------

_KRAKEN_REVERSE = {"BTC": "XBT"}


def _native_symbol(exchange: str, base: str, quote: str) -> str:
    """Convert normalized (base, quote) to exchange-native symbol format."""
    if exchange in ("Binance", "Bybit"):
        return f"{base}{quote}"
    if exchange in ("OKX", "KuCoin"):
        return f"{base}-{quote}"
    if exchange == "Gate.io":
        return f"{base}_{quote}"
    if exchange == "Kraken":
        b = _KRAKEN_REVERSE.get(base, base)
        return f"{b}/{quote}"
    return f"{base}{quote}"


# ---------------------------------------------------------------------------
# Depth URL + response parsing per exchange
# ---------------------------------------------------------------------------

# Base URLs matching config defaults (public endpoints, no auth).
_DEPTH_URLS: dict[str, str] = {
    "Binance": "https://api.binance.com/api/v3/depth",
    "Bybit": "https://api.bybit.com/v5/market/orderbook",
    "OKX": "https://www.okx.com/api/v5/market/books",
    "KuCoin": "https://api.kucoin.com/api/v1/market/orderbook/level2_20",
    "Gate.io": "https://api.gateio.ws/api/v4/spot/order_book",
    "Kraken": "https://api.kraken.com/0/public/Depth",
}


def _depth_params(exchange: str, native_sym: str) -> dict[str, str]:
    """Build query parameters for the depth request."""
    if exchange == "Binance":
        return {"symbol": native_sym, "limit": "10"}
    if exchange == "Bybit":
        return {"category": "spot", "symbol": native_sym, "limit": "25"}
    if exchange == "OKX":
        return {"instId": native_sym, "sz": "10"}
    if exchange == "KuCoin":
        return {"symbol": native_sym}
    if exchange == "Gate.io":
        return {"currency_pair": native_sym, "limit": "10"}
    if exchange == "Kraken":
        return {"pair": native_sym, "count": "10"}
    return {}


def _parse_levels(raw: list) -> list[tuple[float, float]]:
    """Parse [[price, qty, ...], ...] to [(float, float), ...]."""
    return [(safe_float(e[0]), safe_float(e[1])) for e in raw if len(e) >= 2]


def _parse_depth(
    exchange: str, data: dict,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Parse exchange-specific depth response into (bids, asks)."""
    if exchange == "Binance":
        return _parse_levels(data.get("bids", [])), _parse_levels(data.get("asks", []))
    if exchange == "Bybit":
        r = data.get("result", {})
        return _parse_levels(r.get("b", [])), _parse_levels(r.get("a", []))
    if exchange == "OKX":
        d = (data.get("data") or [{}])[0]
        return _parse_levels(d.get("bids", [])), _parse_levels(d.get("asks", []))
    if exchange == "KuCoin":
        d = data.get("data", {})
        return _parse_levels(d.get("bids", [])), _parse_levels(d.get("asks", []))
    if exchange == "Gate.io":
        return _parse_levels(data.get("bids", [])), _parse_levels(data.get("asks", []))
    if exchange == "Kraken":
        result = data.get("result", {})
        pair_data = next(iter(result.values()), {})
        return _parse_levels(pair_data.get("bids", [])), _parse_levels(pair_data.get("asks", []))
    return [], []


# ---------------------------------------------------------------------------
# VWAP walk-through
# ---------------------------------------------------------------------------


def calculate_execution_price(
    levels: list[tuple[float, float]],
    amount_quote: float,
    side: str,
) -> ExecutionEstimate:
    """Walk order book levels to calculate effective execution price.

    For 'buy' (walking asks): levels should be ascending price.
    For 'sell' (walking bids): levels should be descending price.

    Args:
        levels: [(price, qty_base), ...] ordered best-to-worst.
        amount_quote: trade size in quote currency.
        side: 'buy' or 'sell'.

    Returns:
        ExecutionEstimate with VWAP, slippage, fill info.
    """
    if not levels or amount_quote <= 0:
        return ExecutionEstimate(0.0, 0.0, 0.0, 0.0, 0)

    top_price = levels[0][0]
    if top_price <= 0:
        return ExecutionEstimate(0.0, 0.0, 0.0, 0.0, 0)

    total_cost = 0.0
    total_qty = 0.0
    remaining = amount_quote
    levels_used = 0
    depth_total = 0.0

    for price, qty in levels:
        if price <= 0:
            continue
        level_quote = price * qty
        depth_total += level_quote

        if remaining <= 0:
            continue

        levels_used += 1
        fillable = min(remaining, level_quote)
        total_cost += fillable
        total_qty += fillable / price
        remaining -= fillable

    if total_qty <= 0:
        return ExecutionEstimate(0.0, 0.0, 0.0, depth_total, 0)

    effective_price = total_cost / total_qty
    if side == "buy":
        slippage = (effective_price - top_price) / top_price * 100
    else:
        slippage = (top_price - effective_price) / top_price * 100

    return ExecutionEstimate(
        effective_price=effective_price,
        slippage_pct=slippage,
        filled_amount_quote=total_cost,
        depth_available_quote=depth_total,
        levels_consumed=levels_used,
    )


# ---------------------------------------------------------------------------
# OrderBookProbe
# ---------------------------------------------------------------------------


class OrderBookProbe:
    """Fetches L2 order book on-demand for specific pairs."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[tuple[str, str, str], tuple[OrderBookSnapshot, float]] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _get_semaphore(self, exchange: str) -> asyncio.Semaphore:
        if exchange not in self._semaphores:
            self._semaphores[exchange] = asyncio.Semaphore(
                _MAX_CONCURRENT_PER_EXCHANGE,
            )
        return self._semaphores[exchange]

    async def fetch_depth(
        self, exchange: str, base: str, quote: str,
    ) -> OrderBookSnapshot | None:
        """Fetch L2 order book with caching and rate limiting."""
        now = time.monotonic()
        cache_key = (exchange, base, quote)

        cached = self._cache.get(cache_key)
        if cached and now - cached[1] < _CACHE_TTL:
            return cached[0]

        url = _DEPTH_URLS.get(exchange)
        if not url:
            return None

        native_sym = _native_symbol(exchange, base, quote)
        params = _depth_params(exchange, native_sym)

        sem = self._get_semaphore(exchange)
        async with sem:
            try:
                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.debug(
                            "Depth %s %s: HTTP %d", exchange, native_sym, resp.status,
                        )
                        return None
                    data = await resp.json()
            except Exception:
                logger.debug(
                    "Depth %s %s failed", exchange, native_sym, exc_info=True,
                )
                return None

        bids, asks = _parse_depth(exchange, data)
        if not bids and not asks:
            return None

        snapshot = OrderBookSnapshot(
            exchange=exchange,
            symbol=native_sym,
            bids=bids,
            asks=asks,
        )
        self._cache[cache_key] = (snapshot, now)
        return snapshot

    async def enrich_arb_results(
        self,
        results: list[dict],
        *,
        top_n: int = 20,
        trade_size: float = 1000.0,
        fees: object | None = None,
    ) -> None:
        """Add depth data to the top N arb results (in-place).

        For each opportunity in the top N (by gross profit):
        - Fetches ask-side book (buy side) and bid-side book (sell side)
        - Calculates execution price for given trade_size
        - Sets 'ex1k' (exec profit %) and 'mdq' (min depth $) fields.
        """
        if not results:
            return

        sorted_idx = sorted(
            range(len(results)),
            key=lambda i: results[i].get("pf", 0),
            reverse=True,
        )
        top_indices = set(sorted_idx[:top_n])

        # Build fetch tasks for top results
        tasks: list[tuple[int, asyncio.Task]] = []
        for idx in sorted_idx[:top_n]:
            r = results[idx]
            tasks.append((
                idx,
                asyncio.ensure_future(self._fetch_pair_depth(r)),
            ))

        # Await all with global timeout
        raw_tasks = [t for _, t in tasks]
        try:
            await asyncio.wait_for(
                asyncio.gather(*raw_tasks, return_exceptions=True),
                timeout=_ENRICH_TIMEOUT,
            )
        except TimeoutError:
            logger.debug("Depth enrichment timed out")

        # Process results
        for idx, task in tasks:
            r = results[idx]
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is None:
                    pair_result = task.result()
                    if pair_result is not None:
                        ask_snap, bid_snap = pair_result
                        self._apply_depth(r, ask_snap, bid_snap, trade_size, fees)
                        continue
            r["ex1k"] = None
            r["mdq"] = None

        # Ensure non-enriched results have fields
        for i, r in enumerate(results):
            if i not in top_indices:
                r.setdefault("ex1k", None)
                r.setdefault("mdq", None)

    async def _fetch_pair_depth(
        self, r: dict,
    ) -> tuple[OrderBookSnapshot, OrderBookSnapshot] | None:
        """Fetch both sides of an arb opportunity in parallel."""
        base, quote = r["b"], r["q"]
        ask_task = self.fetch_depth(r["ax"], base, quote)
        bid_task = self.fetch_depth(r["bx"], base, quote)
        ask_snap, bid_snap = await asyncio.gather(ask_task, bid_task)
        if ask_snap is None or bid_snap is None:
            return None
        return ask_snap, bid_snap

    @staticmethod
    def _apply_depth(
        r: dict,
        ask_snap: OrderBookSnapshot,
        bid_snap: OrderBookSnapshot,
        trade_size: float,
        fees: object | None,
    ) -> None:
        """Calculate execution-adjusted profit and set depth fields."""
        buy_est = calculate_execution_price(ask_snap.asks, trade_size, "buy")
        sell_est = calculate_execution_price(bid_snap.bids, trade_size, "sell")

        if buy_est.effective_price <= 0 or sell_est.effective_price <= 0:
            r["ex1k"] = None
            r["mdq"] = round(
                min(buy_est.depth_available_quote, sell_est.depth_available_quote), 2,
            )
            return

        if fees is not None:
            buy_taker = fees.taker_fee(r["ax"])
            sell_taker = fees.taker_fee(r["bx"])
        else:
            buy_taker = 0.0
            sell_taker = 0.0

        buy_cost = buy_est.effective_price * (1 + buy_taker)
        sell_rev = sell_est.effective_price * (1 - sell_taker)
        exec_pf = (sell_rev - buy_cost) / buy_cost * 100 if buy_cost > 0 else 0.0

        r["ex1k"] = round(exec_pf, 4)
        r["mdq"] = round(
            min(buy_est.depth_available_quote, sell_est.depth_available_quote), 2,
        )

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
