"""CoinGecko + exchange name cross-validation registry for cross-exchange arbitrage.

Problem: same ticker symbol can refer to different tokens on different exchanges.
E.g., "U" on Binance = Uranium Finance (~$1.00), on Bybit = U Network (~$0.001).

Architecture (two layers, never mixed):

  Layer A — Market data (price source of truth)
    bid, ask, volume, trades — comes from exchanges in real time.

  Layer B — Reference data (identity source of truth)
    "Is this the same asset on both exchanges?" — comes from CoinGecko
    catalog + exchange-provided names (KuCoin fullName, Gate.io base_name),
    cached locally.

CoinGecko provides the canonical coin catalog: each coin has a unique `id`
(e.g. "bitcoin"), a non-unique `symbol` (e.g. "BTC"), and a `name`.
KuCoin provides `fullName` and Gate.io provides `base_name` — used to
cross-validate that a symbol on an exchange matches the CoinGecko entry.

Resolution tiers (per exchange, per symbol):

  1. Globally confirmed — symbol is unique in CoinGecko (1 entry only)
     OR the dominant coin is a top-50 blue-chip.  Confirmed on ALL exchanges
     UNLESS an exchange's own name contradicts the CoinGecko name
     (exchange_blocked).

  1b. Dominant coin — multi-entry symbol where one coin's market cap
      dominates (≥100x runner-up, or ≥10x AND >$10M).  Globally confirmed
      with cross-validation.

  2. KuCoin confirmed — multi-entry symbol where KuCoin's fullName
     matches exactly one CoinGecko entry name.  Confirmed on KuCoin ONLY.
     Other exchanges cannot verify without fullName → blocked there.

  3. Ambiguous / unknown — blocked from cross-exchange arbitrage.

  Exchange-specific block: if an exchange provides a name for a globally-
  confirmed symbol that does NOT match the CoinGecko name, that symbol is
  blocked on that specific exchange only.  CoinGecko's catalog is incomplete;
  an exchange may list a different token under the same symbol.

The golden rule: the arbitrage engine only compares two markets when
BOTH sides resolve to the SAME canonical ID.  If identity cannot be
confirmed on an exchange, that side is excluded.  Period.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

_COINGECKO_COINS_LIST = "https://api.coingecko.com/api/v3/coins/list"
_COINGECKO_COINS_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
_KUCOIN_CURRENCIES = "https://api.kucoin.com/api/v1/currencies"
_GATEIO_CURRENCY_PAIRS = "https://api.gateio.ws/api/v4/spot/currency_pairs"

# Only coins ranked in the top-N by market cap are auto-confirmed when
# their symbol has multiple CoinGecko entries.  Top-50 = absolute blue
# chips where no CEX would ever list a different token under that symbol.
_BLUE_CHIP_RANK = 50

# Dominant coin tier: fetch top 300 by market cap for dominance analysis.
# If one entry's market cap is ≥100x the runner-up → dominant.
# If ≥10x AND >$10M → dominant.
_DOMINANT_RANK = 300
_DOMINANT_MIN_CAP = 10_000_000  # $10M minimum for 10x rule

_CACHE_DIR = Path.home() / ".ananke"
_CACHE_FILE = _CACHE_DIR / "coin_registry.json"
_CACHE_TTL = 86400  # 24 hours


class CoinRegistry:
    """Maps (exchange, base_symbol) to canonical CoinGecko ID.

    Four layers:
    - exchange_blocked: per-exchange blocks where name doesn't match CoinGecko
    - global_confirmed: confirmed on ALL exchanges (unique symbol or blue chip)
    - kucoin_confirmed: confirmed on KuCoin only (fullName matched CoinGecko)
    - ambiguous: known multi-entry symbols that could not be resolved
    """

    def __init__(
        self,
        global_confirmed: dict[str, str],
        kucoin_confirmed: dict[str, str],
        ambiguous: frozenset[str],
        exchange_blocked: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self._global = global_confirmed      # SYMBOL -> coingecko_id
        self._kucoin = kucoin_confirmed      # SYMBOL -> coingecko_id
        self._ambiguous = ambiguous          # blocked symbols
        self._exchange_blocked = exchange_blocked  # {("Gate.io", "VRA"), ...}

    def resolve(self, base_symbol: str, exchange: str = "") -> str | None:
        """Resolve a symbol to its canonical CoinGecko ID.

        Returns the canonical ID only if identity is confirmed for the
        given exchange.  Returns None (blocked) if ambiguous, unknown,
        unconfirmed, or exchange-specific name mismatch.
        """
        upper = base_symbol.upper()
        # Exchange-specific block (name mismatch with CoinGecko)
        if (exchange, upper) in self._exchange_blocked:
            return None
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

    @property
    def exchange_blocked_count(self) -> int:
        return len(self._exchange_blocked)

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
        blocked_raw = data.get("exchange_blocked", [])
        reg = CoinRegistry(
            global_confirmed=data.get("global_confirmed", {}),
            kucoin_confirmed=data.get("kucoin_confirmed", {}),
            ambiguous=frozenset(data.get("ambiguous", [])),
            exchange_blocked=frozenset(tuple(p) for p in blocked_raw),
        )
        logger.info(
            "Loaded coin registry from cache: %d global, %d kucoin, "
            "%d ambiguous, %d exchange-blocked",
            reg.global_count, reg.kucoin_count,
            reg.ambiguous_count, reg.exchange_blocked_count,
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
            "exchange_blocked": sorted(
                [list(p) for p in registry._exchange_blocked]
            ),
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


async def _fetch_market_caps(
    session: aiohttp.ClientSession,
    count: int = _DOMINANT_RANK,
) -> dict[str, float]:
    """Fetch top coins by market cap. Returns {coingecko_id: market_cap_usd}.

    Paginated: CoinGecko free API allows max 250 per page.
    """
    result: dict[str, float] = {}
    pages = [(1, min(count, 250))]
    if count > 250:
        pages.append((2, count - 250))

    for page, per_page in pages:
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": str(per_page),
            "page": str(page),
        }
        retries = 0
        success = False
        while retries < 3:
            async with session.get(_COINGECKO_COINS_MARKETS, params=params) as resp:
                if resp.status == 429:
                    retries += 1
                    wait = int(resp.headers.get("Retry-After", 5))
                    logger.warning(
                        "CoinGecko rate limited, retry %d/3 in %ds",
                        retries, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    logger.warning(
                        "CoinGecko /coins/markets page %d returned %d",
                        page, resp.status,
                    )
                    return result
                data = await resp.json()
                for c in data:
                    cid = c.get("id")
                    cap = c.get("market_cap")
                    if cid and cap is not None:
                        result[cid] = float(cap)
                success = True
                break
        if not success:
            logger.warning(
                "CoinGecko rate limit exhausted for /coins/markets page %d",
                page,
            )
            return result
        if page < len(pages):
            await asyncio.sleep(1.5)

    return result


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


async def _fetch_gateio_names(
    session: aiohttp.ClientSession,
) -> dict[str, str]:
    """Fetch Gate.io token names. Returns {SYMBOL: base_name}."""
    try:
        req_timeout = aiohttp.ClientTimeout(total=60)
        async with session.get(
            _GATEIO_CURRENCY_PAIRS, timeout=req_timeout,
        ) as resp:
            if resp.status != 200:
                logger.warning("Gate.io /currency_pairs returned %d", resp.status)
                return {}
            data = await resp.json()
    except Exception:
        logger.warning("Gate.io /currency_pairs unreachable", exc_info=True)
        return {}

    result: dict[str, str] = {}
    for pair in data:
        if pair.get("trade_status") != "tradable":
            continue
        base = pair.get("base", "").upper()
        name = pair.get("base_name", "")
        if base and name and base not in result:
            result[base] = name
    logger.info("Gate.io: loaded names for %d currencies", len(result))
    return result


def _names_match(name_a: str, name_b: str) -> bool:
    """Check if two token names refer to the same project.

    Naming varies wildly across sources: "Bitcoin Cash" vs "BitcoinCash",
    "Ankr Network" vs "AnkrNetwork", "USDC" vs "USD Coin", "Taraxa" vs
    "Taraxa Coin".  Strategy: normalize aggressively (remove non-alnum,
    lowercase), then check exact or substring containment.
    """
    a = re.sub(r"[^a-z0-9]", "", name_a.lower())
    b = re.sub(r"[^a-z0-9]", "", name_b.lower())
    if not a or not b:
        return True  # can't compare, assume match
    if a == b:
        return True
    # Substring: handles "usdc" in "usdcoin", "sushi" in "sushiswap",
    # "ankr" prefix overlap, "taraxa" in "taraxacoin", etc.
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------


def _build_mappings(
    coins_list: list[dict],
    market_caps: dict[str, float],
    kucoin_names: dict[str, str],
    gateio_names: dict[str, str] | None = None,
) -> CoinRegistry:
    """Build canonical mappings from CoinGecko + exchange names.

    Strategy:
    1. Group CoinGecko coins by uppercase symbol.
    2. Unique symbol (1 coin) -> globally confirmed.
    3. Multiple coins, exactly 1 is a top-50 blue chip -> globally confirmed.
    4. Multiple coins, one dominates by market cap (100x or 10x+$10M) ->
       globally confirmed ("dominant coin" tier).
    5. Multiple coins, KuCoin fullName matches exactly 1 name -> KuCoin confirmed.
    6. Otherwise -> ambiguous (blocked from cross-exchange arbitrage).
    7. Cross-validate: for globally confirmed symbols, verify exchange-provided
       names match CoinGecko name.  Mismatch -> block on that exchange only.
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

    # CoinGecko name for symbols confirmed via "unique" or "dominant" path.
    # Blue chips skip cross-validation — no exchange would list a
    # different token under BTC/ETH/SOL/etc.
    crossval_names: dict[str, str] = {}

    # Derive blue chip IDs (top 50 by market cap)
    sorted_caps = sorted(market_caps.items(), key=lambda x: x[1], reverse=True)
    blue_chip_ids = {cid for cid, _ in sorted_caps[:_BLUE_CHIP_RANK]}

    dominant_count = 0

    for sym, entries in by_symbol.items():
        if len(entries) == 1:
            # Unique symbol — confirmed on all exchanges
            global_confirmed[sym] = entries[0]["id"]
            crossval_names[sym] = entries[0]["name"]
            continue

        # Multiple entries — try blue chip disambiguation
        blue_hits = [e for e in entries if e["id"] in blue_chip_ids]
        if len(blue_hits) == 1:
            # Exactly one is a top blue chip — trusted, no cross-validation
            global_confirmed[sym] = blue_hits[0]["id"]
            continue

        # Dominant coin tier — market cap dominance analysis
        caps_with_data = [
            (e, market_caps[e["id"]])
            for e in entries
            if e["id"] in market_caps and market_caps[e["id"]] > 0
        ]
        if caps_with_data:
            caps_with_data.sort(key=lambda x: x[1], reverse=True)
            top_entry, top_cap = caps_with_data[0]
            runner_up_cap = (
                caps_with_data[1][1] if len(caps_with_data) >= 2 else 0.0
            )

            dominant = False
            if runner_up_cap == 0:
                # Only one entry has market cap data — dominant if significant
                dominant = top_cap >= _DOMINANT_MIN_CAP
            else:
                ratio = top_cap / runner_up_cap
                dominant = (
                    ratio >= 100
                    or (ratio >= 10 and top_cap >= _DOMINANT_MIN_CAP)
                )

            if dominant:
                global_confirmed[sym] = top_entry["id"]
                crossval_names[sym] = top_entry["name"]
                dominant_count += 1
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

    if dominant_count:
        logger.info(
            "Dominant coin tier resolved %d symbols", dominant_count,
        )

    # --- Cross-validate exchange names against CoinGecko ---
    # For globally confirmed symbols, if an exchange provides a name that
    # doesn't match CoinGecko, block that (exchange, symbol) pair.
    exchange_blocked: set[tuple[str, str]] = set()

    exchange_name_sources: dict[str, dict[str, str]] = {
        "KuCoin": kucoin_names,
    }
    if gateio_names:
        exchange_name_sources["Gate.io"] = gateio_names

    for sym, cg_name in crossval_names.items():
        for ex_name, name_map in exchange_name_sources.items():
            ex_token_name = name_map.get(sym, "")
            if ex_token_name and not _names_match(cg_name, ex_token_name):
                exchange_blocked.add((ex_name, sym))
                logger.info(
                    "Name mismatch: %s on %s is '%s' but CoinGecko says '%s' "
                    "— blocked on %s",
                    sym, ex_name, ex_token_name, cg_name, ex_name,
                )

    if exchange_blocked:
        logger.info(
            "Cross-validation blocked %d (exchange, symbol) pairs",
            len(exchange_blocked),
        )

    return CoinRegistry(
        global_confirmed, kucoin_confirmed,
        frozenset(ambiguous), frozenset(exchange_blocked),
    )


async def build_registry() -> CoinRegistry:
    """Build the canonical coin registry.

    Data sources (5 API calls at startup, cached 24h):
    1. CoinGecko /coins/list — full coin catalog (id, symbol, name)
    2-3. CoinGecko /coins/markets — top 300 by market cap (2 pages)
    4. KuCoin /api/v1/currencies — fullName for listed currencies
    5. Gate.io /api/v4/spot/currency_pairs — base_name for listed pairs

    Exchange names are cross-validated against CoinGecko names to detect
    cases where an exchange lists a different token under the same symbol.

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

            # Fetch market caps (top 300, paginated — for blue chip + dominant tiers)
            market_caps = await _fetch_market_caps(session)

            # Fetch exchange names concurrently (independent APIs)
            kucoin_names, gateio_names = await asyncio.gather(
                _fetch_kucoin_fullnames(session),
                _fetch_gateio_names(session),
            )
    except Exception:
        logger.warning(
            "Registry data sources unreachable — registry unavailable",
            exc_info=True,
        )
        return CoinRegistry.empty()

    registry = _build_mappings(
        coins_list, market_caps, kucoin_names, gateio_names,
    )
    logger.info(
        "Built coin registry: %d global, %d kucoin-only, %d ambiguous, "
        "%d exchange-blocked (from %d CoinGecko coins, %d KuCoin, %d Gate.io)",
        registry.global_count,
        registry.kucoin_count,
        registry.ambiguous_count,
        registry.exchange_blocked_count,
        len(coins_list),
        len(kucoin_names),
        len(gateio_names),
    )
    _save_cache(registry)
    return registry
