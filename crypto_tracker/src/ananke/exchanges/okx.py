"""OKX exchange implementation — WebSocket primary, REST polling fallback."""

import asyncio
import contextlib
import json
import logging
from datetime import datetime

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from ananke.config import OkxConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


class OkxExchange(Exchange):
    """
    OKX spot market implementation.

    Primary: WebSocket wss://ws.okx.com:8443/ws/v5/public
      - Single subscribe for all SPOT tickers
      - Ping frame every 25s (OKX requires activity within 30s)

    Fallback: REST polling /api/v5/market/tickers?instType=SPOT
      - Activates after ws_max_failures consecutive WS failures
      - Deactivates when WS reconnects successfully
    """

    def __init__(self, config: OkxConfig | None = None) -> None:
        super().__init__("OKX")
        self.config = config or OkxConfig()
        self._ws_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._symbol_info: dict[str, dict[str, str]] = {}
        self._running = False
        self._ws_failures = 0
        self._ws_connected = False

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
        """Start WebSocket listener + REST fallback poller."""
        self._running = True
        self._ws_failures = 0
        self._ws_task = asyncio.create_task(self._ws_listen())
        self._poll_task = asyncio.create_task(self._poll_fallback())

    async def disconnect(self) -> None:
        """Stop all tasks and close HTTP session."""
        self._running = False
        for task in (self._ws_task, self._poll_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._session and not self._session.closed:
            await self._session.close()

    # --- WebSocket ---

    async def _ws_listen(self) -> None:
        """WebSocket listener with auto-reconnect and backoff."""
        cfg = self.config
        while self._running:
            try:
                async with websockets.connect(
                    cfg.ws_url,
                    ping_interval=cfg.ws_ping_interval,
                    ping_timeout=cfg.ws_ping_timeout,
                    close_timeout=cfg.ws_close_timeout,
                ) as ws:
                    logger.info("OKX WebSocket connected")
                    self._ws_connected = True
                    self._ws_failures = 0

                    # Subscribe to all SPOT tickers
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": [{"channel": "tickers", "instType": "SPOT"}],
                    }))

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            if "data" in msg:
                                self._process_ws_tickers(msg["data"])
                        except json.JSONDecodeError:
                            logger.warning("OKX: invalid JSON from WS")

            except (ConnectionClosed, InvalidURI, OSError) as e:
                self._ws_connected = False
                self._ws_failures += 1
                delay = min(cfg.ws_reconnect_delay * self._ws_failures, 30)
                logger.warning(
                    "OKX WS disconnected (%d/%d): %s — reconnecting in %ds",
                    self._ws_failures, cfg.ws_max_failures, e, delay,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        self._ws_connected = False

    def _process_ws_tickers(self, data: list[dict[str, str]]) -> None:
        """Parse OKX WebSocket ticker push."""
        now = datetime.now()
        for item in data:
            inst_id = item.get("instId", "")
            info = self._symbol_info.get(inst_id)
            if not info:
                continue

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

    # --- REST fallback ---

    async def _poll_fallback(self) -> None:
        """REST polling — only active when WS is down past max failures."""
        while self._running:
            try:
                if self._ws_failures >= self.config.ws_max_failures and not self._ws_connected:
                    await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("OKX REST fallback error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        """Fetch all spot tickers via REST (fallback)."""
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
