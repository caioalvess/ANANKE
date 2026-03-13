"""Bybit exchange implementation — WebSocket primary, REST polling fallback."""

import asyncio
import contextlib
import json
import logging
from datetime import datetime

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from ananke.config import BybitConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)

_SUBSCRIBE_BATCH = 10  # Bybit accepts up to 10 args per subscribe message


class BybitExchange(Exchange):
    """
    Bybit spot market implementation.

    Primary: WebSocket wss://stream.bybit.com/v5/public/spot
      - No "all tickers" stream; subscribes per-symbol in batches of 10
      - Responds to server ping with pong

    Fallback: REST polling /v5/market/tickers?category=spot
      - Activates after ws_max_failures consecutive WS failures
      - Deactivates when WS reconnects successfully
    """

    def __init__(self, config: BybitConfig | None = None) -> None:
        super().__init__("Bybit")
        self.config = config or BybitConfig()
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
                    logger.info("Bybit WebSocket connected")
                    self._ws_connected = True
                    self._ws_failures = 0

                    # Subscribe in batches of 10
                    await self._subscribe_all(ws)

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            # Bybit sends {"op": "ping"} — respond with pong
                            if msg.get("op") == "ping":
                                await ws.send(json.dumps({
                                    "op": "pong",
                                    "req_id": msg.get("req_id", ""),
                                }))
                                continue
                            if "topic" in msg and "data" in msg:
                                self._process_ws_ticker(msg["data"])
                        except json.JSONDecodeError:
                            logger.warning("Bybit: invalid JSON from WS")

            except (ConnectionClosed, InvalidURI, OSError) as e:
                self._ws_connected = False
                self._ws_failures += 1
                delay = min(cfg.ws_reconnect_delay * self._ws_failures, 30)
                logger.warning(
                    "Bybit WS disconnected (%d/%d): %s — reconnecting in %ds",
                    self._ws_failures, cfg.ws_max_failures, e, delay,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        self._ws_connected = False

    async def _subscribe_all(self, ws: object) -> None:
        """Subscribe to all known symbols in batches of 10."""
        symbols = list(self._symbol_info.keys())
        for i in range(0, len(symbols), _SUBSCRIBE_BATCH):
            batch = symbols[i:i + _SUBSCRIBE_BATCH]
            args = [f"tickers.{sym}" for sym in batch]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))

        logger.info(
            "Bybit: subscribed to %d symbols in %d batches",
            len(symbols),
            (len(symbols) + _SUBSCRIBE_BATCH - 1) // _SUBSCRIBE_BATCH,
        )

    def _process_ws_ticker(self, data: dict[str, str]) -> None:
        """Parse a single Bybit WebSocket ticker update."""
        now = datetime.now()
        symbol = data.get("symbol", "")
        info = self._symbol_info.get(symbol)
        if not info:
            return

        price = safe_float(data.get("lastPrice"))
        prev_price = safe_float(data.get("prevPrice24h"))
        pct_raw = safe_float(data.get("price24hPcnt"))

        self.tickers[symbol] = Ticker(
            symbol=symbol,
            base_asset=info["base"],
            quote_asset=info["quote"],
            price=price,
            price_change=price - prev_price if prev_price else 0.0,
            price_change_pct=pct_raw * 100,
            high_24h=safe_float(data.get("highPrice24h")),
            low_24h=safe_float(data.get("lowPrice24h")),
            volume_base=safe_float(data.get("volume24h")),
            volume_quote=safe_float(data.get("turnover24h")),
            bid=safe_float(data.get("bid1Price")),
            ask=safe_float(data.get("ask1Price")),
            open_price=prev_price,
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
                logger.warning("Bybit REST fallback error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        """Fetch all spot tickers via REST (fallback)."""
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
