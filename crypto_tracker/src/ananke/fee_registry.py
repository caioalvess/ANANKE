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
    "Gate.io": 0.002,    # 0.20%
}


class FeeRegistry:
    """Provides fee data for arbitrage profit calculation.

    Taker fees are per-exchange percentages (e.g. 0.001 = 0.1%).
    Withdrawal fees are per-asset fixed amounts in base currency,
    using the cheapest available network.
    Transfer status (withdraw/deposit blocked) used to filter
    non-executable arbitrage opportunities.
    """

    def __init__(
        self,
        taker: dict[str, float],
        withdrawal: dict[str, float],
        withdraw_blocked: frozenset[tuple[str, str]] = frozenset(),
        deposit_blocked: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self._taker = taker               # exchange -> fee rate
        self._withdrawal = withdrawal     # SYMBOL -> fee in base asset
        self._withdraw_blocked = withdraw_blocked  # (exchange, SYMBOL)
        self._deposit_blocked = deposit_blocked    # (exchange, SYMBOL)

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

    def can_execute_arb(
        self,
        bid_exchange: str,
        ask_exchange: str,
        symbol: str,
    ) -> bool:
        """Check if an arbitrage is executable based on transfer status.

        Arb requires: withdraw from ask_exchange, deposit to bid_exchange.
        Returns True if no blocking info is known (assume enabled by default).
        """
        upper = symbol.upper()
        if (ask_exchange, upper) in self._withdraw_blocked:
            return False
        return (bid_exchange, upper) not in self._deposit_blocked

    @property
    def withdrawal_count(self) -> int:
        return len(self._withdrawal)

    @property
    def transfer_status_count(self) -> int:
        return len(self._withdraw_blocked) + len(self._deposit_blocked)

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

        wb = frozenset(
            (e, s) for e, s in data.get("withdraw_blocked", [])
        )
        db = frozenset(
            (e, s) for e, s in data.get("deposit_blocked", [])
        )

        reg = FeeRegistry(
            taker=data.get("taker", _DEFAULT_TAKER),
            withdrawal=data.get("withdrawal", {}),
            withdraw_blocked=wb,
            deposit_blocked=db,
        )
        logger.info(
            "Loaded fee registry from cache: %d withdrawal fees, "
            "%d withdraw-blocked, %d deposit-blocked",
            reg.withdrawal_count,
            len(wb),
            len(db),
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
            "withdraw_blocked": [list(p) for p in registry._withdraw_blocked],
            "deposit_blocked": [list(p) for p in registry._deposit_blocked],
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


class _KucoinCurrencyData:
    """Parsed KuCoin /api/v3/currencies data."""
    __slots__ = ("fees", "withdraw_blocked", "deposit_blocked")

    def __init__(self) -> None:
        self.fees: dict[str, float] = {}
        self.withdraw_blocked: set[tuple[str, str]] = set()
        self.deposit_blocked: set[tuple[str, str]] = set()


async def _fetch_kucoin_currency_data(
    session: aiohttp.ClientSession,
) -> _KucoinCurrencyData:
    """Fetch withdrawal fees + transfer status from KuCoin /api/v3/currencies.

    Returns fees {SYMBOL: cheapest_withdrawal_fee} and sets of
    (exchange, SYMBOL) pairs where withdraw/deposit is fully blocked
    (all chains disabled).
    """
    result = _KucoinCurrencyData()
    exchange = "KuCoin"

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
                    return result
                data = await resp.json()
        except Exception:
            logger.warning("KuCoin /currencies unreachable", exc_info=True)
            return result

        if str(data.get("code")) != "200000":
            logger.warning("KuCoin /currencies error: %s", data.get("msg"))
            return result

        for c in data.get("data", []):
            sym = c.get("currency", "").upper()
            if not sym:
                continue

            # v3 format: chains array with per-chain fees and status
            chains = c.get("chains", [])
            if chains:
                fees = []
                any_withdraw = False
                any_deposit = False
                for ch in chains:
                    if ch.get("isWithdrawEnabled"):
                        any_withdraw = True
                        fee = _safe_float(ch.get("withdrawalMinFee"))
                        if fee > 0:
                            fees.append(fee)
                    if ch.get("isDepositEnabled"):
                        any_deposit = True
                if fees:
                    result.fees[sym] = min(fees)
                if not any_withdraw:
                    result.withdraw_blocked.add((exchange, sym))
                if not any_deposit:
                    result.deposit_blocked.add((exchange, sym))
            else:
                # v1 flat format fallback
                fee = _safe_float(c.get("withdrawalMinFee"))
                if fee > 0:
                    result.fees[sym] = fee

        logger.info(
            "KuCoin: %d withdrawal fees, %d withdraw-blocked, %d deposit-blocked",
            len(result.fees),
            len(result.withdraw_blocked),
            len(result.deposit_blocked),
        )
        return result

    logger.warning("KuCoin /currencies rate limit exhausted")
    return result


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
# Gate.io transfer status
# ---------------------------------------------------------------------------

_GATEIO_CURRENCIES = "https://api.gateio.ws/api/v4/spot/currencies"


async def _fetch_gateio_transfer_status(
    session: aiohttp.ClientSession,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Fetch deposit/withdrawal status from Gate.io /api/v4/spot/currencies.

    Returns (withdraw_blocked, deposit_blocked) as sets of (exchange, SYMBOL).
    Gate.io fields: withdraw_disabled (bool), deposit_disabled (bool).
    """
    exchange = "Gate.io"
    withdraw_blocked: set[tuple[str, str]] = set()
    deposit_blocked: set[tuple[str, str]] = set()

    try:
        req_timeout = aiohttp.ClientTimeout(total=60)
        async with session.get(_GATEIO_CURRENCIES, timeout=req_timeout) as resp:
            if resp.status != 200:
                logger.warning("Gate.io /currencies returned %d", resp.status)
                return withdraw_blocked, deposit_blocked
            data = await resp.json()
    except Exception:
        logger.warning("Gate.io /currencies unreachable", exc_info=True)
        return withdraw_blocked, deposit_blocked

    for c in data:
        sym = c.get("currency", "").upper()
        if not sym:
            continue
        if c.get("withdraw_disabled"):
            withdraw_blocked.add((exchange, sym))
        if c.get("deposit_disabled"):
            deposit_blocked.add((exchange, sym))

    logger.info(
        "Gate.io: %d withdraw-blocked, %d deposit-blocked",
        len(withdraw_blocked),
        len(deposit_blocked),
    )
    return withdraw_blocked, deposit_blocked


# ---------------------------------------------------------------------------
# Kraken transfer status
# ---------------------------------------------------------------------------

_KRAKEN_ASSETS = "https://api.kraken.com/0/public/Assets"

# Kraken asset status values and what they mean for transfers:
#   "enabled"         → both deposit and withdrawal open
#   "deposit_only"    → deposit open, withdrawal closed
#   "withdrawal_only" → withdrawal open, deposit closed
#   "disabled"        → both closed
_KRAKEN_WD_BLOCKED = {"deposit_only", "disabled"}
_KRAKEN_DP_BLOCKED = {"withdrawal_only", "disabled"}
_KRAKEN_REMAP = {"XBT": "BTC"}


async def _fetch_kraken_transfer_status(
    session: aiohttp.ClientSession,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Fetch deposit/withdrawal status from Kraken /0/public/Assets.

    Returns (withdraw_blocked, deposit_blocked) as sets of (exchange, SYMBOL).
    Kraken uses altname as the ticker symbol (e.g. XBT → BTC via _ASSET_REMAP).
    """
    exchange = "Kraken"
    withdraw_blocked: set[tuple[str, str]] = set()
    deposit_blocked: set[tuple[str, str]] = set()

    try:
        async with session.get(_KRAKEN_ASSETS) as resp:
            if resp.status != 200:
                logger.warning("Kraken /Assets returned %d", resp.status)
                return withdraw_blocked, deposit_blocked
            data = await resp.json()
    except Exception:
        logger.warning("Kraken /Assets unreachable", exc_info=True)
        return withdraw_blocked, deposit_blocked

    if data.get("error"):
        logger.warning("Kraken /Assets error: %s", data["error"])
        return withdraw_blocked, deposit_blocked

    for asset_data in data.get("result", {}).values():
        sym = asset_data.get("altname", "").upper()
        if not sym:
            continue
        sym = _KRAKEN_REMAP.get(sym, sym)
        status = asset_data.get("status", "enabled")
        if status in _KRAKEN_WD_BLOCKED:
            withdraw_blocked.add((exchange, sym))
        if status in _KRAKEN_DP_BLOCKED:
            deposit_blocked.add((exchange, sym))

    blocked_total = len(withdraw_blocked) + len(deposit_blocked)
    if blocked_total:
        logger.info(
            "Kraken: %d withdraw-blocked, %d deposit-blocked",
            len(withdraw_blocked),
            len(deposit_blocked),
        )
    else:
        logger.info("Kraken: all %d assets enabled", len(data.get("result", {})))

    return withdraw_blocked, deposit_blocked


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


async def build_fee_registry() -> FeeRegistry:
    """Build the fee registry.

    Withdrawal fee sources (cached 24h):
      1. KuCoin /api/v3/currencies — primary (1 API call, ~2k coins)
      2. withdrawalfees.com — fallback for missing symbols (~32 pages)

    Transfer status sources (same cache):
      - KuCoin /api/v3/currencies — per-chain isWithdrawEnabled/isDepositEnabled
      - Gate.io /api/v4/spot/currencies — withdraw_disabled/deposit_disabled
      - Kraken /0/public/Assets — status (enabled/deposit_only/withdrawal_only/disabled)

    KuCoin data takes priority; withdrawalfees.com fills gaps only.
    Taker fees are hardcoded defaults per exchange.

    On failure returns a registry with default taker fees and
    no withdrawal data (withdrawal fees treated as zero).
    """
    cached = _load_cache()
    if cached is not None:
        return cached

    withdrawal: dict[str, float] = {}
    all_withdraw_blocked: set[tuple[str, str]] = set()
    all_deposit_blocked: set[tuple[str, str]] = set()

    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # KuCoin + Gate.io + Kraken transfer status concurrently
            kucoin_data, (gateio_wb, gateio_db), (kraken_wb, kraken_db) = (
                await asyncio.gather(
                    _fetch_kucoin_currency_data(session),
                    _fetch_gateio_transfer_status(session),
                    _fetch_kraken_transfer_status(session),
                )
            )

            withdrawal.update(kucoin_data.fees)
            all_withdraw_blocked.update(kucoin_data.withdraw_blocked)
            all_deposit_blocked.update(kucoin_data.deposit_blocked)
            all_withdraw_blocked.update(gateio_wb)
            all_deposit_blocked.update(gateio_db)
            all_withdraw_blocked.update(kraken_wb)
            all_deposit_blocked.update(kraken_db)

            # Fallback: withdrawalfees.com for missing symbols
            wfees = await _fetch_wfees_fallback(session)
            fallback_count = 0
            for sym, fee in wfees.items():
                if sym not in withdrawal:
                    withdrawal[sym] = fee
                    fallback_count += 1

            logger.info(
                "Withdrawal fees: %d from KuCoin, %d from withdrawalfees.com",
                len(kucoin_data.fees),
                fallback_count,
            )
    except Exception:
        logger.warning(
            "Fee data unreachable — using defaults", exc_info=True,
        )
        return FeeRegistry.empty()

    registry = FeeRegistry(
        _DEFAULT_TAKER.copy(),
        withdrawal,
        frozenset(all_withdraw_blocked),
        frozenset(all_deposit_blocked),
    )
    logger.info(
        "Built fee registry: %d withdrawal fees, %d withdraw-blocked, "
        "%d deposit-blocked, %d exchange taker rates",
        registry.withdrawal_count,
        len(all_withdraw_blocked),
        len(all_deposit_blocked),
        len(_DEFAULT_TAKER),
    )
    _save_cache(registry)
    return registry
