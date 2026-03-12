"""Gate.io exchange implementation using REST polling (API v4)."""

import asyncio
import contextlib
import logging
import re
from datetime import datetime

import aiohttp

from ananke.config import GateioConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)

# ETF leveraged tokens: symbols ending in 3L, 3S, 5L, 5S, etc.
_ETF_SUFFIX = re.compile(r"\d+[LS]$")


class GateioExchange(Exchange):
    """
    Gate.io spot market implementation.

    Uses REST polling on /api/v4/spot/tickers (all pairs, single call).
    Gate.io uses underscore-separated symbols (BTC_USDT) normalized to BTCUSDT.
    """

    def __init__(self, config: GateioConfig | None = None) -> None:
        super().__init__("Gate.io")
        self.config = config or GateioConfig()
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
        """Fetch symbol metadata from Gate.io REST API."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v4/spot/currency_pairs"
        self._symbol_info.clear()

        # Gate.io returns ~2400 pairs — larger payload than other exchanges.
        # Use a generous per-request timeout for this one-time startup call.
        req_timeout = aiohttp.ClientTimeout(total=60)
        async with session.get(url, timeout=req_timeout) as resp:
            resp.raise_for_status()
            data = await resp.json()

        for s in data:
            if s.get("trade_status") != "tradable":
                continue
            base = s.get("base", "")
            # Skip ETF leveraged tokens (e.g. BTC3L, ETH5S)
            if _ETF_SUFFIX.search(base):
                continue
            pair_id = s.get("id", "")
            self._symbol_info[pair_id] = {
                "base": base,
                "quote": s.get("quote", ""),
            }

        logger.info("Gate.io: loaded %d spot symbols", len(self._symbol_info))

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
        """Poll Gate.io REST API at configured interval."""
        while self._running:
            try:
                await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("Gate.io REST error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        """Fetch all spot tickers in a single REST call."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v4/spot/tickers"

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        now = datetime.now()

        for item in data:
            pair_id = item.get("currency_pair", "")
            info = self._symbol_info.get(pair_id)
            if not info:
                continue

            # Normalize BTC_USDT → BTCUSDT
            symbol = pair_id.replace("_", "")

            price = safe_float(item.get("last"))
            # change_percentage is already in % (e.g. "-0.42" = -0.42%)
            change_pct = safe_float(item.get("change_percentage"))
            high = safe_float(item.get("high_24h"))
            low = safe_float(item.get("low_24h"))

            # Derive price_change from percentage and price
            open_price = price / (1 + change_pct / 100) if change_pct != 0 else price
            price_change = price - open_price

            self.tickers[symbol] = Ticker(
                symbol=symbol,
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=price,
                price_change=price_change,
                price_change_pct=change_pct,
                high_24h=high,
                low_24h=low,
                volume_base=safe_float(item.get("base_volume")),
                volume_quote=safe_float(item.get("quote_volume")),
                bid=safe_float(item.get("highest_bid")),
                ask=safe_float(item.get("lowest_ask")),
                open_price=open_price,
                trades_count=0,
                last_update=now,
                exchange=self.name,
            )

        self._notify()
