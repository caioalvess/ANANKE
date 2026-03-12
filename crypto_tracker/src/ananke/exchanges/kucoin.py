"""KuCoin exchange implementation using REST polling."""

import asyncio
import contextlib
import logging
from datetime import datetime

import aiohttp

from ananke.config import KucoinConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


class KucoinExchange(Exchange):
    """
    KuCoin spot market implementation.

    Uses REST polling on /api/v1/market/allTickers (all pairs, single call).
    KuCoin uses hyphenated symbols (BTC-USDT) normalized to BTCUSDT.
    """

    def __init__(self, config: KucoinConfig | None = None) -> None:
        super().__init__("KuCoin")
        self.config = config or KucoinConfig()
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
        """Fetch symbol metadata from KuCoin REST API."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v1/symbols"
        self._symbol_info.clear()

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if str(data.get("code")) != "200000":
            logger.error("KuCoin symbols error: %s", data.get("msg"))
            return

        for s in data.get("data", []):
            if not s.get("enableTrading"):
                continue
            self._symbol_info[s["symbol"]] = {
                "base": s["baseCurrency"],
                "quote": s["quoteCurrency"],
            }

        logger.info("KuCoin: loaded %d spot symbols", len(self._symbol_info))

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
        """Poll KuCoin REST API at configured interval."""
        while self._running:
            try:
                await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("KuCoin REST error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        """Fetch all spot tickers in a single REST call."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v1/market/allTickers"

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if str(data.get("code")) != "200000":
            logger.warning("KuCoin allTickers error: %s", data.get("msg"))
            return

        now = datetime.now()

        for item in data.get("data", {}).get("ticker", []):
            kc_symbol = item.get("symbol", "")
            info = self._symbol_info.get(kc_symbol)
            if not info:
                continue

            # Normalize BTC-USDT → BTCUSDT
            symbol = kc_symbol.replace("-", "")

            price = safe_float(item.get("last"))
            change_price = safe_float(item.get("changePrice"))
            # changeRate is decimal ratio (0.0188 = 1.88%)
            change_rate = safe_float(item.get("changeRate"))

            self.tickers[symbol] = Ticker(
                symbol=symbol,
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=price,
                price_change=change_price,
                price_change_pct=change_rate * 100,
                high_24h=safe_float(item.get("high")),
                low_24h=safe_float(item.get("low")),
                volume_base=safe_float(item.get("vol")),
                volume_quote=safe_float(item.get("volValue")),
                bid=safe_float(item.get("buy")),
                ask=safe_float(item.get("sell")),
                open_price=safe_float(item.get("open")),
                trades_count=0,
                last_update=now,
                exchange=self.name,
            )

        self._notify()
