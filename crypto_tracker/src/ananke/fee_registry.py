"""Exchange fee registry for realistic arbitrage profit calculation.

Two types of fees matter for cross-exchange arbitrage:

1. Taker fees — percentage per trade (buy/sell).  Known defaults per
   exchange, hardcoded (lowest public tier, no VIP discount).

2. Withdrawal fees — fixed amount in base asset to transfer between
   exchanges.  Primary source: KuCoin /api/v3/currencies (public,
   no API key).  Fallback: withdrawalfees.com (aggregates 16 exchanges).
   Both use cheapest available network per asset.

Net profit after taker fees (scale-independent):
  npf = (bid*(1-sell_taker) - ask*(1+buy_taker)) / (ask*(1+buy_taker)) * 100

Withdrawal fee shown separately in quote currency (wf = wd_fee * bid)
because its impact depends on trade size:
  true_net = npf - (wf / trade_amount) * 100
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

_KUCOIN_CURRENCIES_V3 = "https://api.kucoin.com/api/v3/currencies"

_CACHE_DIR = Path.home() / ".ananke"
_CACHE_FILE = _CACHE_DIR / "fee_registry.json"
_CACHE_TTL = 86400  # 24 hours

# Default taker fees — lowest public tier, no VIP discount.
# Source: each exchange's published fee schedule.
_DEFAULT_TAKER: dict[str, float] = {
    "Binance": 0.001,    # 0.10%
    "Bybit": 0.001,      # 0.10%
    "OKX": 0.001,        # 0.10%
    "Kraken": 0.004,     # 0.40%
    "KuCoin": 0.001,     # 0.10%
}


class FeeRegistry:
    """Provides fee data for arbitrage profit calculation.

    Taker fees are per-exchange percentages (e.g. 0.001 = 0.1%).
    Withdrawal fees are per-asset fixed amounts in base currency,
    using the cheapest available network.
    """

    def __init__(
        self,
        taker: dict[str, float],
        withdrawal: dict[str, float],
    ) -> None:
        self._taker = taker               # exchange -> fee rate
        self._withdrawal = withdrawal     # SYMBOL -> fee in base asset

    def taker_fee(self, exchange: str) -> float:
        """Taker fee rate for an exchange (e.g. 0.001 for 0.1%)."""
        return self._taker.get(exchange, 0.001)

    def withdrawal_fee(self, symbol: str) -> float:
        """Withdrawal fee in base asset units (cheapest network)."""
        return self._withdrawal.get(symbol.upper(), 0.0)

    def net_profit_after_taker(
        self,
        bid: float,
        ask: float,
        bid_exchange: str,
        ask_exchange: str,
    ) -> float:
        """Net profit % after taker fees only (scale-independent).

        Assumes: buy at ask on ask_exchange, sell at bid on bid_exchange.
        """
        buy_taker = self.taker_fee(ask_exchange)
        sell_taker = self.taker_fee(bid_exchange)

        buy_cost = ask * (1 + buy_taker)
        sell_revenue = bid * (1 - sell_taker)

        if buy_cost <= 0:
            return 0.0
        return (sell_revenue - buy_cost) / buy_cost * 100

    def withdrawal_cost_quote(
        self,
        base_symbol: str,
        price: float,
    ) -> float:
        """Withdrawal fee converted to quote currency."""
        return self.withdrawal_fee(base_symbol) * price

    @property
    def withdrawal_count(self) -> int:
        return len(self._withdrawal)

    @staticmethod
    def empty() -> "FeeRegistry":
        """Fee registry with default taker fees and no withdrawal data."""
        return FeeRegistry(_DEFAULT_TAKER.copy(), {})


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _load_cache() -> FeeRegistry | None:
    """Load cached fee registry if fresh enough."""
    try:
        if not _CACHE_FILE.exists():
            return None
        data = json.loads(_CACHE_FILE.read_text())
        if time.time() - data.get("ts", 0) > _CACHE_TTL:
            return None
        reg = FeeRegistry(
            taker=data.get("taker", _DEFAULT_TAKER),
            withdrawal=data.get("withdrawal", {}),
        )
        logger.info(
            "Loaded fee registry from cache: %d withdrawal fees",
            reg.withdrawal_count,
        )
        return reg
    except Exception:
        return None


def _save_cache(registry: FeeRegistry) -> None:
    """Persist fee registry to disk."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "ts": time.time(),
            "taker": registry._taker,
            "withdrawal": registry._withdrawal,
        }))
    except Exception:
        logger.debug("Could not write fee registry cache", exc_info=True)


# ---------------------------------------------------------------------------
# KuCoin withdrawal fee fetcher
# ---------------------------------------------------------------------------


def _safe_float(val: object) -> float:
    """Convert value to float, returning 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


async def _fetch_withdrawal_fees(
    session: aiohttp.ClientSession,
) -> dict[str, float]:
    """Fetch withdrawal fees from KuCoin /api/v3/currencies.

    Returns {SYMBOL: cheapest_withdrawal_fee} using the minimum fee
    across all available networks (chains) for each currency.
    """
    retries = 0
    while retries < 3:
        try:
            async with session.get(_KUCOIN_CURRENCIES_V3) as resp:
                if resp.status == 429:
                    retries += 1
                    wait = int(resp.headers.get("Retry-After", 5))
                    logger.warning(
                        "KuCoin /currencies rate limited, retry %d/3 in %ds",
                        retries, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    logger.warning(
                        "KuCoin /api/v3/currencies returned %d", resp.status,
                    )
                    return {}
                data = await resp.json()
        except Exception:
            logger.warning("KuCoin /currencies unreachable", exc_info=True)
            return {}

        if str(data.get("code")) != "200000":
            logger.warning("KuCoin /currencies error: %s", data.get("msg"))
            return {}

        result: dict[str, float] = {}
        for c in data.get("data", []):
            sym = c.get("currency", "").upper()
            if not sym:
                continue

            # v3 format: chains array with per-chain fees
            chains = c.get("chains", [])
            if chains:
                fees = []
                for ch in chains:
                    if not ch.get("isWithdrawEnabled"):
                        continue
                    fee = _safe_float(ch.get("withdrawalMinFee"))
                    if fee > 0:
                        fees.append(fee)
                if fees:
                    result[sym] = min(fees)  # cheapest network
            else:
                # v1 flat format fallback
                fee = _safe_float(c.get("withdrawalMinFee"))
                if fee > 0:
                    result[sym] = fee

        logger.info("KuCoin: loaded withdrawal fees for %d currencies", len(result))
        return result

    logger.warning("KuCoin /currencies rate limit exhausted")
    return {}


# ---------------------------------------------------------------------------
# withdrawalfees.com fallback (SvelteKit __data.json)
# ---------------------------------------------------------------------------

_WFEES_BASE = "https://withdrawalfees.com/coins"
_WFEES_PAGE_SIZE = 50
_WFEES_DELAY = 0.3  # seconds between requests to avoid rate limiting


def _parse_wfees_page(raw: dict) -> dict[str, float]:
    """Parse a withdrawalfees.com SvelteKit __data.json page.

    SvelteKit stores data as a flat array with index references.
    Each coin entry has {symbol: idx, min: idx, ...} pointing to
    values elsewhere in the flat array.

    Returns {SYMBOL: min_withdrawal_fee}.
    """
    result: dict[str, float] = {}
    try:
        flat = raw["nodes"][1]["data"]
        coin_indices = flat[2]  # list of indices to coin entries
        for ci in coin_indices:
            schema = flat[ci]
            sym = flat[schema["symbol"]].upper()
            min_fee = flat[schema["min"]]
            if isinstance(min_fee, (int, float)) and min_fee > 0:
                result[sym] = min_fee
    except (KeyError, IndexError, TypeError):
        pass
    return result


def _parse_wfees_total_pages(raw: dict) -> int:
    """Extract total number of pages from the first response."""
    try:
        flat = raw["nodes"][1]["data"]
        count = flat[0].get("count", 0)
        return (count // _WFEES_PAGE_SIZE) + 1
    except (KeyError, IndexError, TypeError):
        return 0


async def _fetch_wfees_fallback(
    session: aiohttp.ClientSession,
) -> dict[str, float]:
    """Fetch minimum withdrawal fees from withdrawalfees.com.

    Paginates through all coin listing pages, extracting the minimum
    fee (cheapest network) for each symbol.  Used as fallback for
    symbols not covered by KuCoin.

    Returns {SYMBOL: min_withdrawal_fee}.
    """
    result: dict[str, float] = {}

    # First page to get total count
    try:
        async with session.get(f"{_WFEES_BASE}/1/__data.json") as resp:
            if resp.status != 200:
                logger.warning("withdrawalfees.com returned %d", resp.status)
                return {}
            first = await resp.json(content_type=None)
    except Exception:
        logger.warning("withdrawalfees.com unreachable", exc_info=True)
        return {}

    total_pages = _parse_wfees_total_pages(first)
    if total_pages <= 0:
        return {}

    result.update(_parse_wfees_page(first))

    # Remaining pages
    for page in range(2, total_pages + 1):
        await asyncio.sleep(_WFEES_DELAY)
        try:
            async with session.get(
                f"{_WFEES_BASE}/{page}/__data.json",
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                result.update(_parse_wfees_page(data))
        except Exception:
            logger.debug("withdrawalfees.com page %d failed", page)

    logger.info(
        "withdrawalfees.com: loaded min fees for %d currencies",
        len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


async def build_fee_registry() -> FeeRegistry:
    """Build the fee registry.

    Withdrawal fee sources (cached 24h):
      1. KuCoin /api/v3/currencies — primary (1 API call, ~2k coins)
      2. withdrawalfees.com — fallback for missing symbols (~32 pages)

    KuCoin data takes priority; withdrawalfees.com fills gaps only.
    Taker fees are hardcoded defaults per exchange.

    On failure returns a registry with default taker fees and
    no withdrawal data (withdrawal fees treated as zero).
    """
    cached = _load_cache()
    if cached is not None:
        return cached

    withdrawal: dict[str, float] = {}

    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Primary: KuCoin
            kucoin_fees = await _fetch_withdrawal_fees(session)
            withdrawal.update(kucoin_fees)

            # Fallback: withdrawalfees.com for missing symbols
            wfees = await _fetch_wfees_fallback(session)
            fallback_count = 0
            for sym, fee in wfees.items():
                if sym not in withdrawal:
                    withdrawal[sym] = fee
                    fallback_count += 1

            logger.info(
                "Withdrawal fees: %d from KuCoin, %d from withdrawalfees.com",
                len(kucoin_fees),
                fallback_count,
            )
    except Exception:
        logger.warning(
            "Fee data unreachable — using defaults", exc_info=True,
        )
        return FeeRegistry.empty()

    registry = FeeRegistry(_DEFAULT_TAKER.copy(), withdrawal)
    logger.info(
        "Built fee registry: %d total withdrawal fees, %d exchange taker rates",
        registry.withdrawal_count,
        len(_DEFAULT_TAKER),
    )
    _save_cache(registry)
    return registry
