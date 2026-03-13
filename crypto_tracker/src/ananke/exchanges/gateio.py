"""Gate.io exchange implementation — WebSocket primary, REST polling fallback."""

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from ananke.config import GateioConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)

# ETF leveraged tokens: symbols ending in 3L, 3S, 5L, 5S, etc.
_ETF_SUFFIX = re.compile(r"\d+[LS]$")

_SUBSCRIBE_BATCH = 50  # Gate.io accepts large payload arrays


class GateioExchange(Exchange):
    """
    Gate.io spot market implementation.

    Primary: WebSocket wss://api.gateio.ws/ws/v4/
      - No "all tickers" stream; subscribes per-symbol in batches
      - Server sends ping, client responds pong

    Fallback: REST polling /api/v4/spot/tickers
      - Activates after ws_max_failures consecutive WS failures
    """

    def __init__(self, config: GateioConfig | None = None) -> None:
        super().__init__("Gate.io")
        self.config = config or GateioConfig()
        self._ws_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._symbol_info: dict[str, dict[str, str]] = {}
        self._running = False
        self._ws_failures = 0
        self._ws_connected = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.rest_timeout_sec)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def fetch_exchange_info(self) -> None:
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v4/spot/currency_pairs"
        self._symbol_info.clear()

        req_timeout = aiohttp.ClientTimeout(total=60)
        async with session.get(url, timeout=req_timeout) as resp:
            resp.raise_for_status()
            data = await resp.json()

        for s in data:
            if s.get("trade_status") != "tradable":
                continue
            base = s.get("base", "")
            if _ETF_SUFFIX.search(base):
                continue
            pair_id = s.get("id", "")
            self._symbol_info[pair_id] = {
                "base": base,
                "quote": s.get("quote", ""),
            }

        logger.info("Gate.io: loaded %d spot symbols", len(self._symbol_info))

    async def connect(self) -> None:
        self._running = True
        self._ws_failures = 0
        self._ws_task = asyncio.create_task(self._ws_listen())
        self._poll_task = asyncio.create_task(self._poll_fallback())

    async def disconnect(self) -> None:
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
        cfg = self.config
        while self._running:
            try:
                async with websockets.connect(
                    cfg.ws_url,
                    ping_interval=cfg.ws_ping_interval,
                    ping_timeout=cfg.ws_ping_timeout,
                    close_timeout=cfg.ws_close_timeout,
                ) as ws:
                    logger.info("Gate.io WebSocket connected")
                    self._ws_connected = True
                    self._ws_failures = 0

                    await self._subscribe_all(ws)

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            channel = msg.get("channel", "")
                            event = msg.get("event", "")
                            if channel == "spot.tickers" and event == "update":
                                self._process_ws_ticker(msg.get("result", {}))
                        except json.JSONDecodeError:
                            logger.warning("Gate.io: invalid JSON from WS")

            except (ConnectionClosed, InvalidURI, OSError) as e:
                self._ws_connected = False
                self._ws_failures += 1
                delay = min(cfg.ws_reconnect_delay * self._ws_failures, 30)
                logger.warning(
                    "Gate.io WS disconnected (%d/%d): %s — reconnecting in %ds",
                    self._ws_failures, cfg.ws_max_failures, e, delay,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        self._ws_connected = False

    async def _subscribe_all(self, ws: object) -> None:
        """Subscribe to all known symbols in batches."""
        symbols = list(self._symbol_info.keys())
        for i in range(0, len(symbols), _SUBSCRIBE_BATCH):
            batch = symbols[i:i + _SUBSCRIBE_BATCH]
            await ws.send(json.dumps({
                "time": int(time.time()),
                "channel": "spot.tickers",
                "event": "subscribe",
                "payload": batch,
            }))

        logger.info(
            "Gate.io: subscribed to %d symbols in %d batches",
            len(symbols),
            (len(symbols) + _SUBSCRIBE_BATCH - 1) // _SUBSCRIBE_BATCH,
        )

    def _process_ws_ticker(self, data: dict[str, str]) -> None:
        """Parse a Gate.io WebSocket ticker update."""
        pair_id = data.get("currency_pair", "")
        info = self._symbol_info.get(pair_id)
        if not info:
            return

        symbol = pair_id.replace("_", "")
        price = safe_float(data.get("last"))
        change_pct = safe_float(data.get("change_percentage"))
        open_price = price / (1 + change_pct / 100) if change_pct != 0 else price
        price_change = price - open_price

        self.tickers[symbol] = Ticker(
            symbol=symbol,
            base_asset=info["base"],
            quote_asset=info["quote"],
            price=price,
            price_change=price_change,
            price_change_pct=change_pct,
            high_24h=safe_float(data.get("high_24h")),
            low_24h=safe_float(data.get("low_24h")),
            volume_base=safe_float(data.get("base_volume")),
            volume_quote=safe_float(data.get("quote_volume")),
            bid=safe_float(data.get("highest_bid")),
            ask=safe_float(data.get("lowest_ask")),
            open_price=open_price,
            trades_count=0,
            last_update=datetime.now(),
            exchange=self.name,
        )
        self._notify()

    # --- REST fallback ---

    async def _poll_fallback(self) -> None:
        while self._running:
            try:
                if self._ws_failures >= self.config.ws_max_failures and not self._ws_connected:
                    await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("Gate.io REST fallback error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
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

            symbol = pair_id.replace("_", "")
            price = safe_float(item.get("last"))
            change_pct = safe_float(item.get("change_percentage"))
            high = safe_float(item.get("high_24h"))
            low = safe_float(item.get("low_24h"))

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
