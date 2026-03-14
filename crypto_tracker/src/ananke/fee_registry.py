"""Exchange fee registry for realistic arbitrage profit calculation.

Two types of fees matter for cross-exchange arbitrage:

1. Taker fees — percentage per trade (buy/sell).  Known defaults per
   exchange, hardcoded (lowest public tier, no VIP discount).

2. Withdrawal fees — fixed amount in base asset to transfer between
   exchanges.  Per-exchange fees from public endpoints (Binance, KuCoin)
   and withdrawalfees.com per-exchange pages (Bybit, Gate.io, Kraken).
   Authenticated APIs (OKX, Binance, Bybit) add data when keys are set.
   Fallback: withdrawalfees.com min-fee across all exchanges.
   All sources use cheapest available network per asset.

Net profit after taker fees (scale-independent):
  npf = (bid*(1-sell_taker) - ask*(1+buy_taker)) / (ask*(1+buy_taker)) * 100

Withdrawal fee shown separately in quote currency (wf = wd_fee * bid)
because its impact depends on trade size:
  true_net = npf - (wf / trade_amount) * 100
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from os import environ
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
    "Kraken": 0.0026,    # 0.26% (Pro, lowest tier)
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
        withdrawal: dict[tuple[str, str], float],
        withdraw_blocked: frozenset[tuple[str, str]] = frozenset(),
        deposit_blocked: frozenset[tuple[str, str]] = frozenset(),
        fallback_withdrawal: dict[str, float] | None = None,
    ) -> None:
        self._taker = taker               # exchange -> fee rate
        self._withdrawal = withdrawal     # (exchange, SYMBOL) -> fee in base asset
        self._fallback = fallback_withdrawal or {}  # SYMBOL -> fee (KuCoin/wfees)
        self._withdraw_blocked = withdraw_blocked  # (exchange, SYMBOL)
        self._deposit_blocked = deposit_blocked    # (exchange, SYMBOL)

    def taker_fee(self, exchange: str) -> float:
        """Taker fee rate for an exchange (e.g. 0.001 for 0.1%)."""
        return self._taker.get(exchange, 0.001)

    def withdrawal_fee(self, symbol: str, exchange: str = "") -> float:
        """Withdrawal fee in base asset units (cheapest network).

        Fallback chain: exchange-specific → KuCoin → wfees → 0.0.
        """
        upper = symbol.upper()
        if exchange:
            fee = self._withdrawal.get((exchange, upper))
            if fee is not None:
                return fee
        return self._fallback.get(upper, 0.0)

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
        exchange: str = "",
    ) -> float:
        """Withdrawal fee converted to quote currency."""
        return self.withdrawal_fee(base_symbol, exchange) * price

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
        return len(self._withdrawal) + len(self._fallback)

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

        # Per-exchange withdrawal fees: [[exchange, symbol, fee], ...]
        withdrawal: dict[tuple[str, str], float] = {}
        for entry in data.get("withdrawal_ex", []):
            withdrawal[(entry[0], entry[1])] = entry[2]

        reg = FeeRegistry(
            taker=data.get("taker", _DEFAULT_TAKER),
            withdrawal=withdrawal,
            withdraw_blocked=wb,
            deposit_blocked=db,
            fallback_withdrawal=data.get("fallback_withdrawal", {}),
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
            "withdrawal_ex": [
                [ex, sym, fee]
                for (ex, sym), fee in registry._withdrawal.items()
            ],
            "fallback_withdrawal": registry._fallback,
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


class _CurrencyData:
    """Parsed currency data from an exchange (fees + transfer status)."""
    __slots__ = ("exchange", "fees", "withdraw_blocked", "deposit_blocked")

    def __init__(self, exchange: str) -> None:
        self.exchange = exchange
        self.fees: dict[str, float] = {}
        self.withdraw_blocked: set[tuple[str, str]] = set()
        self.deposit_blocked: set[tuple[str, str]] = set()


async def _fetch_kucoin_currency_data(
    session: aiohttp.ClientSession,
) -> _CurrencyData:
    """Fetch withdrawal fees + transfer status from KuCoin /api/v3/currencies.

    Returns fees {SYMBOL: cheapest_withdrawal_fee} and sets of
    (exchange, SYMBOL) pairs where withdraw/deposit is fully blocked
    (all chains disabled).
    """
    result = _CurrencyData("KuCoin")
    exchange = result.exchange

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
_WFEES_EX_BASE = "https://withdrawalfees.com/exchanges"
_WFEES_PAGE_SIZE = 50
_WFEES_DELAY = 0.3  # seconds between requests to avoid rate limiting

# Exchanges to fetch per-exchange fees from withdrawalfees.com.
# Maps our internal exchange name → wfees slug.
_WFEES_EXCHANGE_SLUGS: dict[str, str] = {
    "Bybit": "bybit",
    "Gate.io": "gate",
    "Kraken": "kraken",
}


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


def _parse_wfees_exchange_page(
    raw: dict,
) -> dict[str, float]:
    """Parse a withdrawalfees.com /exchanges/{slug} page.

    Returns {SYMBOL: cheapest_withdrawal_fee} for that exchange.
    Multiple chains per coin → keeps the minimum fee.
    """
    result: dict[str, float] = {}
    try:
        flat = raw["nodes"][1]["data"]
        schema = flat[0]
        fee_indices = flat[schema["fees"]]
        for fi in fee_indices:
            entry = flat[fi]
            amount = flat[entry["amount"]]
            if not isinstance(amount, (int, float)) or amount < 0:
                continue
            coin_obj = flat[entry["coin"]]
            sym = flat[coin_obj["symbol"]].upper()
            # Keep cheapest chain per symbol
            if sym not in result or amount < result[sym]:
                result[sym] = amount
    except (KeyError, IndexError, TypeError):
        pass
    return result


def _parse_wfees_exchange_total_pages(raw: dict) -> int:
    """Extract total pages from a /exchanges/{slug} response."""
    try:
        flat = raw["nodes"][1]["data"]
        schema = flat[0]
        count = flat[schema["count"]]
        return (count // _WFEES_PAGE_SIZE) + 1
    except (KeyError, IndexError, TypeError):
        return 0


async def _fetch_wfees_exchange(
    session: aiohttp.ClientSession,
    exchange: str,
    slug: str,
) -> _CurrencyData:
    """Fetch per-coin withdrawal fees for one exchange from withdrawalfees.com.

    Paginates through /exchanges/{slug}/{page}/__data.json.
    Returns _CurrencyData with fees (cheapest network per symbol).
    """
    result = _CurrencyData(exchange)
    base_url = f"{_WFEES_EX_BASE}/{slug}"

    # First page
    try:
        async with session.get(f"{base_url}/__data.json") as resp:
            if resp.status != 200:
                logger.debug(
                    "wfees /exchanges/%s returned %d", slug, resp.status,
                )
                return result
            first = await resp.json(content_type=None)
    except Exception:
        logger.debug("wfees /exchanges/%s unreachable", slug, exc_info=True)
        return result

    total_pages = _parse_wfees_exchange_total_pages(first)
    if total_pages <= 0:
        return result

    fees = _parse_wfees_exchange_page(first)

    for page in range(2, total_pages + 1):
        await asyncio.sleep(_WFEES_DELAY)
        try:
            async with session.get(
                f"{base_url}/{page}/__data.json",
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                page_fees = _parse_wfees_exchange_page(data)
                for sym, fee in page_fees.items():
                    if sym not in fees or fee < fees[sym]:
                        fees[sym] = fee
        except Exception:
            logger.debug("wfees /exchanges/%s page %d failed", slug, page)

    # Only keep fees > 0 (zero-fee chains are real but don't
    # overwrite a known positive fee from a better source)
    for sym, fee in fees.items():
        result.fees[sym] = fee

    logger.info(
        "wfees %s: %d withdrawal fees",
        exchange, len(result.fees),
    )
    return result


async def _fetch_wfees_exchanges(
    session: aiohttp.ClientSession,
) -> list[_CurrencyData]:
    """Fetch withdrawal fees for all configured exchanges from withdrawalfees.com.

    Fetches sequentially per exchange (each exchange paginates internally)
    to stay friendly with rate limits.
    """
    results: list[_CurrencyData] = []
    for exchange, slug in _WFEES_EXCHANGE_SLUGS.items():
        cd = await _fetch_wfees_exchange(session, exchange, slug)
        results.append(cd)
        # Small delay between exchanges
        await asyncio.sleep(_WFEES_DELAY)
    return results


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
# OKX transfer status + withdrawal fees (requires API key)
# ---------------------------------------------------------------------------

_OKX_CURRENCIES = "https://www.okx.com/api/v5/asset/currencies"


def _okx_sign(secret: str, timestamp: str, method: str, path: str) -> str:
    """Generate OKX API signature (base64 HMAC-SHA256)."""
    prehash = f"{timestamp}{method}{path}"
    mac = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


async def _fetch_okx_currency_data(
    session: aiohttp.ClientSession,
) -> _CurrencyData:
    """Fetch transfer status + withdrawal fees from OKX /api/v5/asset/currencies.

    Requires env vars: ANANKE_OKX_API_KEY, ANANKE_OKX_API_SECRET, ANANKE_OKX_PASSPHRASE.
    Returns empty data if keys are not configured.
    """
    result = _CurrencyData("OKX")

    api_key = environ.get("ANANKE_OKX_API_KEY", "")
    api_secret = environ.get("ANANKE_OKX_API_SECRET", "")
    passphrase = environ.get("ANANKE_OKX_PASSPHRASE", "")

    if not all([api_key, api_secret, passphrase]):
        logger.debug("OKX API keys not configured — skipping transfer status")
        return result

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    signature = _okx_sign(api_secret, timestamp, "GET", "/api/v5/asset/currencies")

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }

    try:
        async with session.get(_OKX_CURRENCIES, headers=headers) as resp:
            if resp.status != 200:
                logger.warning("OKX /currencies returned %d", resp.status)
                return result
            data = await resp.json()
    except Exception:
        logger.warning("OKX /currencies unreachable", exc_info=True)
        return result

    if data.get("code") != "0":
        logger.warning("OKX /currencies error: %s", data.get("msg"))
        return result

    # Group chains by currency
    coin_chains: dict[str, list[dict]] = {}
    for item in data.get("data", []):
        ccy = item.get("ccy", "").upper()
        if ccy:
            coin_chains.setdefault(ccy, []).append(item)

    exchange = result.exchange
    for sym, chains in coin_chains.items():
        any_withdraw = any(chain.get("canWd") for chain in chains)
        any_deposit = any(chain.get("canDep") for chain in chains)

        fees = []
        for chain in chains:
            if chain.get("canWd"):
                fee = _safe_float(chain.get("minFee"))
                if fee > 0:
                    fees.append(fee)

        if fees:
            result.fees[sym] = min(fees)
        if not any_withdraw:
            result.withdraw_blocked.add((exchange, sym))
        if not any_deposit:
            result.deposit_blocked.add((exchange, sym))

    logger.info(
        "OKX: %d withdrawal fees, %d withdraw-blocked, %d deposit-blocked",
        len(result.fees),
        len(result.withdraw_blocked),
        len(result.deposit_blocked),
    )
    return result


# ---------------------------------------------------------------------------
# Binance transfer status + withdrawal fees
# ---------------------------------------------------------------------------

_BINANCE_CAPITAL_CONFIG = "https://api.binance.com/sapi/v1/capital/config/getall"
_BINANCE_PUBLIC_COINS = (
    "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll"
)


async def _fetch_binance_public_currency_data(
    session: aiohttp.ClientSession,
) -> _CurrencyData:
    """Fetch withdrawal fees + transfer status from Binance public endpoint.

    No API key required.  Uses the same data source as the Binance fee page
    (binance.com/en/fee/cryptoFee).  Response format mirrors the authenticated
    /sapi/v1/capital/config/getall endpoint.
    """
    result = _CurrencyData("Binance")
    exchange = result.exchange

    headers = {
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        async with session.get(
            _BINANCE_PUBLIC_COINS, headers=headers,
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "Binance public /getNetworkCoinAll returned %d",
                    resp.status,
                )
                return result
            body = await resp.json(content_type=None)
    except Exception:
        logger.warning(
            "Binance public /getNetworkCoinAll unreachable", exc_info=True,
        )
        return result

    data = body.get("data") if isinstance(body, dict) else body
    if not data:
        logger.warning("Binance public /getNetworkCoinAll: empty data")
        return result

    for coin_info in data:
        sym = coin_info.get("coin", "").upper()
        if not sym:
            continue

        networks = coin_info.get("networkList", [])
        any_withdraw = False
        any_deposit = False
        fees: list[float] = []

        for net in networks:
            if net.get("withdrawEnable"):
                any_withdraw = True
                fee = _safe_float(net.get("withdrawFee"))
                if fee > 0:
                    fees.append(fee)
            if net.get("depositEnable"):
                any_deposit = True

        if fees:
            result.fees[sym] = min(fees)
        if not any_withdraw:
            result.withdraw_blocked.add((exchange, sym))
        if not any_deposit:
            result.deposit_blocked.add((exchange, sym))

    logger.info(
        "Binance (public): %d withdrawal fees, "
        "%d withdraw-blocked, %d deposit-blocked",
        len(result.fees),
        len(result.withdraw_blocked),
        len(result.deposit_blocked),
    )
    return result


def _binance_sign(secret: str, query_string: str) -> str:
    """Generate Binance API signature (HMAC-SHA256 hex)."""
    return hmac.new(
        secret.encode(), query_string.encode(), hashlib.sha256,
    ).hexdigest()


async def _fetch_binance_currency_data(
    session: aiohttp.ClientSession,
) -> _CurrencyData:
    """Fetch transfer status + withdrawal fees from Binance /sapi/v1/capital/config/getall.

    Requires env vars: ANANKE_BINANCE_API_KEY, ANANKE_BINANCE_API_SECRET.
    Returns empty data if keys are not configured.
    """
    result = _CurrencyData("Binance")

    api_key = environ.get("ANANKE_BINANCE_API_KEY", "")
    api_secret = environ.get("ANANKE_BINANCE_API_SECRET", "")

    if not all([api_key, api_secret]):
        logger.debug("Binance API keys not configured — skipping transfer status")
        return result

    timestamp_ms = str(int(time.time() * 1000))
    query_string = f"timestamp={timestamp_ms}"
    signature = _binance_sign(api_secret, query_string)
    url = f"{_BINANCE_CAPITAL_CONFIG}?{query_string}&signature={signature}"

    headers = {"X-MBX-APIKEY": api_key}

    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Binance /capital/config returned %d: %s", resp.status, body[:200])
                return result
            data = await resp.json()
    except Exception:
        logger.warning("Binance /capital/config unreachable", exc_info=True)
        return result

    exchange = result.exchange
    for coin_info in data:
        sym = coin_info.get("coin", "").upper()
        if not sym:
            continue

        networks = coin_info.get("networkList", [])
        any_withdraw = False
        any_deposit = False
        fees = []

        for net in networks:
            if net.get("withdrawEnable"):
                any_withdraw = True
                fee = _safe_float(net.get("withdrawFee"))
                if fee > 0:
                    fees.append(fee)
            if net.get("depositEnable"):
                any_deposit = True

        if fees:
            result.fees[sym] = min(fees)
        if not any_withdraw:
            result.withdraw_blocked.add((exchange, sym))
        if not any_deposit:
            result.deposit_blocked.add((exchange, sym))

    logger.info(
        "Binance: %d withdrawal fees, %d withdraw-blocked, %d deposit-blocked",
        len(result.fees),
        len(result.withdraw_blocked),
        len(result.deposit_blocked),
    )
    return result


# ---------------------------------------------------------------------------
# Bybit transfer status + withdrawal fees (requires API key)
# ---------------------------------------------------------------------------

_BYBIT_COIN_INFO = "https://api.bybit.com/v5/asset/coin/query-info"


def _bybit_sign(
    secret: str, timestamp_ms: str, api_key: str, recv_window: str,
    query_string: str = "",
) -> str:
    """Generate Bybit v5 API signature (HMAC-SHA256 hex)."""
    param_str = f"{timestamp_ms}{api_key}{recv_window}{query_string}"
    return hmac.new(
        secret.encode(), param_str.encode(), hashlib.sha256,
    ).hexdigest()


async def _fetch_bybit_currency_data(
    session: aiohttp.ClientSession,
) -> _CurrencyData:
    """Fetch transfer status + withdrawal fees from Bybit /v5/asset/coin/query-info.

    Requires env vars: ANANKE_BYBIT_API_KEY, ANANKE_BYBIT_API_SECRET.
    Returns empty data if keys are not configured.
    """
    result = _CurrencyData("Bybit")

    api_key = environ.get("ANANKE_BYBIT_API_KEY", "")
    api_secret = environ.get("ANANKE_BYBIT_API_SECRET", "")

    if not all([api_key, api_secret]):
        logger.debug("Bybit API keys not configured — skipping transfer status")
        return result

    timestamp_ms = str(int(time.time() * 1000))
    recv_window = "5000"
    signature = _bybit_sign(api_secret, timestamp_ms, api_key, recv_window)

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp_ms,
        "X-BAPI-RECV-WINDOW": recv_window,
    }

    try:
        async with session.get(_BYBIT_COIN_INFO, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Bybit /coin/query-info returned %d: %s", resp.status, body[:200])
                return result
            data = await resp.json()
    except Exception:
        logger.warning("Bybit /coin/query-info unreachable", exc_info=True)
        return result

    ret_code = data.get("retCode")
    if ret_code != 0:
        logger.warning("Bybit /coin/query-info error %s: %s", ret_code, data.get("retMsg"))
        return result

    exchange = result.exchange
    for row in data.get("result", {}).get("rows", []):
        sym = row.get("coin", "").upper()
        if not sym:
            continue

        chains = row.get("chains", [])
        any_withdraw = False
        any_deposit = False
        fees = []

        for ch in chains:
            # Bybit uses "1" for enabled, "0" for disabled
            if str(ch.get("chainDeposit")) == "1":
                any_deposit = True
            if str(ch.get("chainWithdraw")) == "1":
                any_withdraw = True
                fee = _safe_float(ch.get("withdrawFee"))
                if fee > 0:
                    fees.append(fee)

        if fees:
            result.fees[sym] = min(fees)
        if not any_withdraw:
            result.withdraw_blocked.add((exchange, sym))
        if not any_deposit:
            result.deposit_blocked.add((exchange, sym))

    logger.info(
        "Bybit: %d withdrawal fees, %d withdraw-blocked, %d deposit-blocked",
        len(result.fees),
        len(result.withdraw_blocked),
        len(result.deposit_blocked),
    )
    return result


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


async def build_fee_registry() -> FeeRegistry:
    """Build the fee registry.

    Withdrawal fee sources (cached 24h):
      1. Binance public /getNetworkCoinAll — per-network (no key needed)
      2. Per-exchange APIs (KuCoin, OKX, Binance auth, Bybit) — per-chain
      3. withdrawalfees.com per-exchange — covers Bybit, Gate.io, Kraken
      4. withdrawalfees.com min fallback — remaining symbols (~32 pages)

    Transfer status sources (same cache):
      - KuCoin /api/v3/currencies — per-chain (public, no key)
      - Gate.io /api/v4/spot/currencies — per-coin (public, no key)
      - Kraken /0/public/Assets — per-asset status (public, no key)
      - Binance public /getNetworkCoinAll — per-network (no key needed)
      - OKX /api/v5/asset/currencies — per-chain (requires API key)
      - Binance /sapi/v1/capital/config/getall — per-network (requires API key)
      - Bybit /v5/asset/coin/query-info — per-chain (requires API key)

    Taker fees are hardcoded defaults per exchange.
    On failure returns a registry with default taker fees and
    no withdrawal data (withdrawal fees treated as zero).
    """
    cached = _load_cache()
    if cached is not None:
        return cached

    withdrawal: dict[tuple[str, str], float] = {}
    fallback_withdrawal: dict[str, float] = {}
    all_withdraw_blocked: set[tuple[str, str]] = set()
    all_deposit_blocked: set[tuple[str, str]] = set()

    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # All exchange currency data fetchers concurrently
            (
                kucoin_data,
                (gateio_wb, gateio_db),
                (kraken_wb, kraken_db),
                okx_data,
                binance_auth_data,
                binance_pub_data,
                bybit_data,
            ) = await asyncio.gather(
                _fetch_kucoin_currency_data(session),
                _fetch_gateio_transfer_status(session),
                _fetch_kraken_transfer_status(session),
                _fetch_okx_currency_data(session),
                _fetch_binance_currency_data(session),
                _fetch_binance_public_currency_data(session),
                _fetch_bybit_currency_data(session),
            )

            # Merge Binance: auth wins when available, public fills gaps
            binance_data = _CurrencyData("Binance")
            for sym, fee in binance_pub_data.fees.items():
                binance_data.fees[sym] = fee
            binance_data.withdraw_blocked.update(binance_pub_data.withdraw_blocked)
            binance_data.deposit_blocked.update(binance_pub_data.deposit_blocked)
            # Auth data overwrites (may reflect VIP-specific fees)
            for sym, fee in binance_auth_data.fees.items():
                binance_data.fees[sym] = fee
            binance_data.withdraw_blocked = (
                binance_auth_data.withdraw_blocked or binance_data.withdraw_blocked
            )
            binance_data.deposit_blocked = (
                binance_auth_data.deposit_blocked or binance_data.deposit_blocked
            )

            # Collect per-exchange withdrawal fees
            for cd in (kucoin_data, okx_data, binance_data, bybit_data):
                for sym, fee in cd.fees.items():
                    withdrawal[(cd.exchange, sym)] = fee
                all_withdraw_blocked.update(cd.withdraw_blocked)
                all_deposit_blocked.update(cd.deposit_blocked)

            # Gate.io + Kraken return (blocked, blocked) tuples
            all_withdraw_blocked.update(gateio_wb)
            all_deposit_blocked.update(gateio_db)
            all_withdraw_blocked.update(kraken_wb)
            all_deposit_blocked.update(kraken_db)

            # Fallback fees: merge all exchange fees → generic fallback
            # Priority: KuCoin > OKX > Binance > Bybit
            for cd in (bybit_data, binance_data, okx_data, kucoin_data):
                fallback_withdrawal.update(cd.fees)

            # withdrawalfees.com per-exchange: covers Bybit, Gate.io, Kraken
            # Only add if we don't already have exchange-specific data
            # (auth APIs are more authoritative when available)
            wfees_ex_data = await _fetch_wfees_exchanges(session)
            wfees_ex_count = 0
            for cd in wfees_ex_data:
                for sym, fee in cd.fees.items():
                    key = (cd.exchange, sym)
                    if key not in withdrawal:
                        withdrawal[key] = fee
                        wfees_ex_count += 1
                # wfees doesn't provide transfer status — keep existing

            # withdrawalfees.com min-fee fills remaining gaps
            wfees = await _fetch_wfees_fallback(session)
            fallback_count = 0
            for sym, fee in wfees.items():
                if sym not in fallback_withdrawal:
                    fallback_withdrawal[sym] = fee
                    fallback_count += 1

            # Log coverage summary
            sources = []
            for label, cd in [
                ("KuCoin", kucoin_data), ("OKX", okx_data),
                ("Binance", binance_data), ("Bybit", bybit_data),
            ]:
                if cd.fees:
                    sources.append(f"{len(cd.fees)} from {label}")
            for cd in wfees_ex_data:
                if cd.fees:
                    sources.append(
                        f"{len(cd.fees)} from wfees/{cd.exchange}",
                    )
            if fallback_count:
                sources.append(f"{fallback_count} from withdrawalfees.com")
            logger.info("Withdrawal fees: %s", ", ".join(sources) or "none")

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
        fallback_withdrawal,
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
