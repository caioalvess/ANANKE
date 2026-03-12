"""Bybit exchange implementation using REST polling (V5 API)."""

import asyncio
import contextlib
import logging
from datetime import datetime

import aiohttp

from ananke.config import BybitConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


class BybitExchange(Exchange):
    """
    Bybit spot market implementation.

    Uses REST polling on /v5/market/tickers?category=spot because
    Bybit's WebSocket requires per-symbol subscriptions (no bulk stream).
    Polling at 2s intervals is well within rate limits (10 req/s public).
    """

    def __init__(self, config: BybitConfig | None = None) -> None:
        super().__init__("Bybit")
        self.config = config or BybitConfig()
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
        """Fetch symbol metadata from Bybit REST API."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/v5/market/instruments-info"
        cursor = ""
        self._symbol_info.clear()

        while True:
            params: dict[str, str] = {"category": "spot", "limit": "1000"}
            if cursor:
                params["cursor"] = cursor

            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            result = data.get("result", {})
            for s in result.get("list", []):
                if s.get("status") == "Trading":
                    self._symbol_info[s["symbol"]] = {
                        "base": s["baseCoin"],
                        "quote": s["quoteCoin"],
                    }

            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break

        logger.info("Bybit: loaded %d spot symbols", len(self._symbol_info))

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
        """Poll Bybit REST API at configured interval."""
        while self._running:
            try:
                await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("Bybit REST error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        """Fetch all spot tickers in a single REST call."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/v5/market/tickers"

        async with session.get(url, params={"category": "spot"}) as resp:
            resp.raise_for_status()
            data = await resp.json()

        now = datetime.now()

        for item in data.get("result", {}).get("list", []):
            symbol = item.get("symbol", "")
            info = self._symbol_info.get(symbol)
            if not info:
                continue

            price = safe_float(item.get("lastPrice"))
            prev_price = safe_float(item.get("prevPrice24h"))
            pct_raw = safe_float(item.get("price24hPcnt"))

            self.tickers[symbol] = Ticker(
                symbol=symbol,
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=price,
                price_change=price - prev_price if prev_price else 0.0,
                price_change_pct=pct_raw * 100,
                high_24h=safe_float(item.get("highPrice24h")),
                low_24h=safe_float(item.get("lowPrice24h")),
                volume_base=safe_float(item.get("volume24h")),
                volume_quote=safe_float(item.get("turnover24h")),
                bid=safe_float(item.get("bid1Price")),
                ask=safe_float(item.get("ask1Price")),
                open_price=prev_price,
                trades_count=0,
                last_update=now,
                exchange=self.name,
            )

        self._notify()
