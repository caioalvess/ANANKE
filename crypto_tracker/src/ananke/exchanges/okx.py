"""OKX exchange implementation using REST polling (V5 API)."""

import asyncio
import contextlib
import logging
from datetime import datetime

import aiohttp

from ananke.config import OkxConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


class OkxExchange(Exchange):
    """
    OKX spot market implementation.

    Uses REST polling on /api/v5/market/tickers?instType=SPOT.
    OKX uses hyphenated instIds (BTC-USDT) instead of concatenated (BTCUSDT).
    """

    def __init__(self, config: OkxConfig | None = None) -> None:
        super().__init__("OKX")
        self.config = config or OkxConfig()
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
        """Fetch symbol metadata from OKX REST API."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v5/public/instruments"
        self._symbol_info.clear()

        async with session.get(url, params={"instType": "SPOT"}) as resp:
            resp.raise_for_status()
            data = await resp.json()

        for s in data.get("data", []):
            if s.get("state") == "live":
                self._symbol_info[s["instId"]] = {
                    "base": s["baseCcy"],
                    "quote": s["quoteCcy"],
                }

        logger.info("OKX: loaded %d spot symbols", len(self._symbol_info))

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
        """Poll OKX REST API at configured interval."""
        while self._running:
            try:
                await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("OKX REST error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        """Fetch all spot tickers in a single REST call."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v5/market/tickers"

        async with session.get(url, params={"instType": "SPOT"}) as resp:
            resp.raise_for_status()
            data = await resp.json()

        now = datetime.now()

        for item in data.get("data", []):
            inst_id = item.get("instId", "")
            info = self._symbol_info.get(inst_id)
            if not info:
                continue

            # OKX uses BTC-USDT format; normalize to BTCUSDT for cross-exchange matching
            symbol = inst_id.replace("-", "")

            price = safe_float(item.get("last"))
            open_24h = safe_float(item.get("open24h"))
            price_change = price - open_24h if open_24h else 0.0
            price_change_pct = (price_change / open_24h * 100) if open_24h else 0.0

            self.tickers[symbol] = Ticker(
                symbol=symbol,
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=price,
                price_change=price_change,
                price_change_pct=price_change_pct,
                high_24h=safe_float(item.get("high24h")),
                low_24h=safe_float(item.get("low24h")),
                volume_base=safe_float(item.get("vol24h")),
                volume_quote=safe_float(item.get("volCcy24h")),
                bid=safe_float(item.get("bidPx")),
                ask=safe_float(item.get("askPx")),
                open_price=open_24h,
                trades_count=0,
                last_update=now,
                exchange=self.name,
            )

        self._notify()
