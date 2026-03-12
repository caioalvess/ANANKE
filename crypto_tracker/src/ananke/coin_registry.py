"""CoinGecko + KuCoin canonical identity registry for cross-exchange arbitrage.

Problem: same ticker symbol can refer to different tokens on different exchanges.
E.g., "U" on Binance = Uranium Finance (~$1.00), on Bybit = U Network (~$0.001).

Architecture (two layers, never mixed):

  Layer A — Market data (price source of truth)
    bid, ask, volume, trades — comes from exchanges in real time.

  Layer B — Reference data (identity source of truth)
    "Is this the same asset on both exchanges?" — comes from CoinGecko
    catalog + KuCoin fullName, cached locally.

CoinGecko provides the canonical coin catalog: each coin has a unique `id`
(e.g. "bitcoin"), a non-unique `symbol` (e.g. "BTC"), and a `name`.
KuCoin provides `fullName` for its listed currencies — the only exchange
in our set that exposes this publicly.

Resolution tiers (per exchange, per symbol):

  1. Globally confirmed — symbol is unique in CoinGecko (1 entry only)
     OR the dominant coin is a top blue-chip.  No CEX would list a
     different token under BTC/ETH/SOL.  Confirmed on ALL exchanges.

  2. KuCoin confirmed — multi-entry symbol where KuCoin's fullName
     matches exactly one CoinGecko entry name.  Confirmed on KuCoin ONLY.
     Other exchanges cannot verify without fullName → blocked there.

  3. Ambiguous / unknown — blocked from cross-exchange arbitrage.

The golden rule: the arbitrage engine only compares two markets when
BOTH sides resolve to the SAME canonical ID.  If identity cannot be
confirmed on an exchange, that side is excluded.  Period.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

_COINGECKO_COINS_LIST = "https://api.coingecko.com/api/v3/coins/list"
_COINGECKO_COINS_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
_KUCOIN_CURRENCIES = "https://api.kucoin.com/api/v1/currencies"

# Only coins ranked in the top-N by market cap are auto-confirmed when
# their symbol has multiple CoinGecko entries.  Top-50 = absolute blue
# chips where no CEX would ever list a different token under that symbol.
_BLUE_CHIP_RANK = 50

_CACHE_DIR = Path.home() / ".ananke"
_CACHE_FILE = _CACHE_DIR / "coin_registry.json"
_CACHE_TTL = 86400  # 24 hours


class CoinRegistry:
    """Maps (exchange, base_symbol) to canonical CoinGecko ID.

    Three tiers:
    - global_confirmed: confirmed on ALL exchanges (unique symbol or blue chip)
    - kucoin_confirmed: confirmed on KuCoin only (fullName matched CoinGecko)
    - ambiguous: known multi-entry symbols that could not be resolved
    """

    def __init__(
        self,
        global_confirmed: dict[str, str],
        kucoin_confirmed: dict[str, str],
        ambiguous: frozenset[str],
    ) -> None:
        self._global = global_confirmed      # SYMBOL -> coingecko_id
        self._kucoin = kucoin_confirmed      # SYMBOL -> coingecko_id
        self._ambiguous = ambiguous          # blocked symbols

    def resolve(self, base_symbol: str, exchange: str = "") -> str | None:
        """Resolve a symbol to its canonical CoinGecko ID.

        Returns the canonical ID only if identity is confirmed for the
        given exchange.  Returns None (blocked) if ambiguous, unknown,
        or unconfirmed on that exchange.
        """
        upper = base_symbol.upper()
        # Tier 1: globally confirmed (unique or blue chip)
        if upper in self._global:
            return self._global[upper]
        # Tier 2: KuCoin-specific confirmation via fullName
        if exchange == "KuCoin" and upper in self._kucoin:
            return self._kucoin[upper]
        # Tier 3: ambiguous, unknown, or unconfirmed on this exchange
        return None

    @property
    def global_count(self) -> int:
        return len(self._global)

    @property
    def kucoin_count(self) -> int:
        return len(self._kucoin)

    @property
    def ambiguous_count(self) -> int:
        return len(self._ambiguous)

    def has_data(self) -> bool:
        """True if the registry was populated (not empty/degraded)."""
        return bool(self._global or self._kucoin or self._ambiguous)

    @staticmethod
    def empty() -> "CoinRegistry":
        """Empty registry — graceful degradation, allows everything."""
        return CoinRegistry({}, {}, frozenset())


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _load_cache() -> CoinRegistry | None:
    """Load cached registry if fresh enough."""
    try:
        if not _CACHE_FILE.exists():
            return None
        data = json.loads(_CACHE_FILE.read_text())
        if time.time() - data.get("ts", 0) > _CACHE_TTL:
            return None
        reg = CoinRegistry(
            global_confirmed=data.get("global_confirmed", {}),
            kucoin_confirmed=data.get("kucoin_confirmed", {}),
            ambiguous=frozenset(data.get("ambiguous", [])),
        )
        logger.info(
            "Loaded coin registry from cache: %d global, %d kucoin, %d ambiguous",
            reg.global_count, reg.kucoin_count, reg.ambiguous_count,
        )
        return reg
    except Exception:
        return None


def _save_cache(registry: CoinRegistry) -> None:
    """Persist registry to disk."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "ts": time.time(),
            "global_confirmed": registry._global,
            "kucoin_confirmed": registry._kucoin,
            "ambiguous": sorted(registry._ambiguous),
        }))
    except Exception:
        logger.debug("Could not write coin registry cache", exc_info=True)


# ---------------------------------------------------------------------------
# API fetchers
# ---------------------------------------------------------------------------


async def _fetch_coins_list(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch complete coin catalog from CoinGecko /coins/list."""
    retries = 0
    while retries < 3:
        async with session.get(_COINGECKO_COINS_LIST) as resp:
            if resp.status == 429:
                retries += 1
                wait = int(resp.headers.get("Retry-After", 5))
                logger.warning(
                    "CoinGecko /coins/list rate limited, retry %d/3 in %ds",
                    retries, wait,
                )
                await asyncio.sleep(wait)
                continue
            if resp.status != 200:
                logger.warning("CoinGecko /coins/list returned %d", resp.status)
                return []
            return await resp.json()
    logger.warning("CoinGecko /coins/list rate limit exhausted")
    return []


async def _fetch_blue_chips(
    session: aiohttp.ClientSession,
    count: int = _BLUE_CHIP_RANK,
) -> set[str]:
    """Fetch top coins by market cap. Returns set of CoinGecko IDs."""
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": str(count),
        "page": "1",
    }
    retries = 0
    while retries < 3:
        async with session.get(_COINGECKO_COINS_MARKETS, params=params) as resp:
            if resp.status == 429:
                retries += 1
                wait = int(resp.headers.get("Retry-After", 5))
                logger.warning(
                    "CoinGecko rate limited, retry %d/3 in %ds", retries, wait,
                )
                await asyncio.sleep(wait)
                continue
            if resp.status != 200:
                logger.warning("CoinGecko /coins/markets returned %d", resp.status)
                return set()
            data = await resp.json()
            return {c["id"] for c in data if c.get("id")}
    logger.warning("CoinGecko rate limit exhausted for /coins/markets")
    return set()


async def _fetch_kucoin_fullnames(
    session: aiohttp.ClientSession,
) -> dict[str, str]:
    """Fetch KuCoin currency list. Returns {SYMBOL: fullName}."""
    try:
        async with session.get(_KUCOIN_CURRENCIES) as resp:
            if resp.status != 200:
                logger.warning("KuCoin /currencies returned %d", resp.status)
                return {}
            data = await resp.json()
    except Exception:
        logger.warning("KuCoin /currencies unreachable", exc_info=True)
        return {}

    if str(data.get("code")) != "200000":
        logger.warning("KuCoin /currencies error: %s", data.get("msg"))
        return {}

    result: dict[str, str] = {}
    for c in data.get("data", []):
        sym = c.get("currency", "").upper()
        name = c.get("fullName", "")
        if sym and name:
            result[sym] = name
    logger.info("KuCoin: loaded fullName for %d currencies", len(result))
    return result


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------


def _build_mappings(
    coins_list: list[dict],
    blue_chip_ids: set[str],
    kucoin_names: dict[str, str],
) -> CoinRegistry:
    """Build canonical mappings from CoinGecko + KuCoin data.

    Strategy:
    1. Group CoinGecko coins by uppercase symbol.
    2. Unique symbol (1 coin) -> globally confirmed.
    3. Multiple coins, exactly 1 is a blue chip -> globally confirmed.
    4. Multiple coins, KuCoin fullName matches exactly 1 name -> KuCoin confirmed.
    5. Otherwise -> ambiguous (blocked from cross-exchange arbitrage).
    """
    # Group by symbol: {SYMBOL: [{id, name}, ...]}
    by_symbol: dict[str, list[dict[str, str]]] = {}
    for coin in coins_list:
        sym = coin.get("symbol", "").upper()
        cid = coin.get("id", "")
        name = coin.get("name", "")
        if not sym or not cid:
            continue
        by_symbol.setdefault(sym, []).append({"id": cid, "name": name})

    global_confirmed: dict[str, str] = {}
    kucoin_confirmed: dict[str, str] = {}
    ambiguous: set[str] = set()

    for sym, entries in by_symbol.items():
        if len(entries) == 1:
            # Unique symbol — confirmed on all exchanges
            global_confirmed[sym] = entries[0]["id"]
            continue

        # Multiple entries — try blue chip disambiguation
        blue_hits = [e for e in entries if e["id"] in blue_chip_ids]
        if len(blue_hits) == 1:
            # Exactly one is a top blue chip — no CEX would list another
            global_confirmed[sym] = blue_hits[0]["id"]
            continue

        # Not globally resolvable — try KuCoin fullName matching
        kc_name = kucoin_names.get(sym, "")
        if kc_name:
            name_matches = [
                e for e in entries
                if e["name"].lower() == kc_name.lower()
            ]
            if len(name_matches) == 1:
                kucoin_confirmed[sym] = name_matches[0]["id"]
                # Still ambiguous on other exchanges
                ambiguous.add(sym)
                continue

        # Truly ambiguous — blocked everywhere
        ambiguous.add(sym)

    return CoinRegistry(global_confirmed, kucoin_confirmed, frozenset(ambiguous))


async def build_registry() -> CoinRegistry:
    """Build the canonical coin registry.

    Data sources (3 API calls at startup, cached 24h):
    1. CoinGecko /coins/list — full coin catalog (id, symbol, name)
    2. CoinGecko /coins/markets — top blue chips by market cap
    3. KuCoin /api/v1/currencies — fullName for listed currencies

    On any failure returns an empty registry (graceful degradation —
    all symbols allowed, accepting the risk of collisions).
    """
    cached = _load_cache()
    if cached is not None:
        return cached

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Fetch CoinGecko catalog
            coins_list = await _fetch_coins_list(session)
            if not coins_list:
                logger.warning("CoinGecko catalog empty — registry unavailable")
                return CoinRegistry.empty()

            await asyncio.sleep(1.5)

            # Fetch blue chips (1 call, top-50)
            blue_chip_ids = await _fetch_blue_chips(session)

            # Fetch KuCoin fullNames concurrently (independent API)
            kucoin_names = await _fetch_kucoin_fullnames(session)
    except Exception:
        logger.warning(
            "Registry data sources unreachable — registry unavailable",
            exc_info=True,
        )
        return CoinRegistry.empty()

    registry = _build_mappings(coins_list, blue_chip_ids, kucoin_names)
    logger.info(
        "Built coin registry: %d global, %d kucoin-only, %d ambiguous "
        "(from %d CoinGecko coins, %d KuCoin currencies)",
        registry.global_count,
        registry.kucoin_count,
        registry.ambiguous_count,
        len(coins_list),
        len(kucoin_names),
    )
    _save_cache(registry)
    return registry
