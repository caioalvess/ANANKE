"""Kraken exchange implementation using REST polling."""

import asyncio
import contextlib
import logging
from datetime import datetime

import aiohttp

from ananke.config import KrakenConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


def _val(src: dict, field: str, idx: int) -> float:
    """Safe array access for Kraken's array-based ticker fields."""
    arr = src.get(field, [])
    return safe_float(arr[idx]) if idx < len(arr) else 0.0


# Kraken legacy assets that need normalization.
# Legacy crypto assets have X prefix (4-char), fiats have Z prefix (4-char).
_ASSET_REMAP: dict[str, str] = {
    "XXBT": "BTC",
    "XBT": "BTC",
    "XETH": "ETH",
    "XLTC": "LTC",
    "XXLM": "XLM",
    "XXMR": "XMR",
    "XXRP": "XRP",
    "XETC": "ETC",
    "XXTZ": "XTZ",
    "XMLN": "MLN",
    "XREP": "REP",
    "XXDG": "DOGE",
    "ZUSD": "USD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZJPY": "JPY",
    "ZCAD": "CAD",
    "ZAUD": "AUD",
    "ZCHF": "CHF",
}


def _normalize_asset(asset: str) -> str:
    """Normalize Kraken's legacy asset names to standard tickers."""
    if asset in _ASSET_REMAP:
        return _ASSET_REMAP[asset]
    return asset


class KrakenExchange(Exchange):
    """
    Kraken spot market implementation.

    Uses REST polling on /0/public/Ticker (no pair param = all pairs).
    Kraken uses legacy naming (XXBTZUSD, XBT instead of BTC) which is
    normalized to standard tickers for cross-exchange matching.
    """

    def __init__(self, config: KrakenConfig | None = None) -> None:
        super().__init__("Kraken")
        self.config = config or KrakenConfig()
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._symbol_info: dict[str, dict[str, str]] = {}
        self._running = False

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.rest_timeout_sec)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def fetch_exchange_info(self) -> None:
        """Fetch symbol metadata from Kraken AssetPairs endpoint."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/0/public/AssetPairs"
        self._symbol_info.clear()

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if data.get("error"):
            logger.error("Kraken AssetPairs error: %s", data["error"])
            return

        for pair_key, info in data.get("result", {}).items():
            # Skip dark pool pairs (suffixed with .d)
            if pair_key.endswith(".d"):
                continue
            if info.get("status") != "online":
                continue

            base = _normalize_asset(info["base"])
            quote = _normalize_asset(info["quote"])

            self._symbol_info[pair_key] = {
                "base": base,
                "quote": quote,
                "symbol": f"{base}{quote}",
            }

        logger.info("Kraken: loaded %d spot symbols", len(self._symbol_info))

    async def connect(self) -> None:
        """Start the polling loop."""
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def disconnect(self) -> None:
        """Stop polling and close HTTP session."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._session and not self._session.closed:
            await self._session.close()

    async def _poll_loop(self) -> None:
        """Poll Kraken REST API at configured interval."""
        while self._running:
            try:
                await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("Kraken REST error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        """Fetch all spot tickers in a single REST call."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/0/public/Ticker"

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if data.get("error"):
            logger.warning("Kraken Ticker error: %s", data["error"])
            return

        now = datetime.now()

        for pair_key, item in data.get("result", {}).items():
            info = self._symbol_info.get(pair_key)
            if not info:
                continue

            # c = [price, lotVolume], o = open (single string, not array)
            price = _val(item, "c", 0)
            open_price = safe_float(item.get("o"))
            price_change = price - open_price if open_price else 0.0
            price_change_pct = (price_change / open_price * 100) if open_price else 0.0

            volume_base = _val(item,"v", 1)
            # Kraken has no quote volume; compute from VWAP * volume
            vwap_24h = _val(item,"p", 1)
            volume_quote = vwap_24h * volume_base

            self.tickers[info["symbol"]] = Ticker(
                symbol=info["symbol"],
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=price,
                price_change=price_change,
                price_change_pct=price_change_pct,
                high_24h=_val(item,"h", 1),
                low_24h=_val(item,"l", 1),
                volume_base=volume_base,
                volume_quote=volume_quote,
                bid=_val(item,"b", 0),
                ask=_val(item,"a", 0),
                open_price=open_price,
                trades_count=int(_val(item,"t", 1)),
                last_update=now,
                exchange=self.name,
            )

        self._notify()
