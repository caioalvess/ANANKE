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
                            if event != "update":
                                continue
                            if channel == "spot.tickers":
                                self._process_ws_ticker(msg.get("result", {}))
                            elif channel == "spot.book_ticker":
                                self._process_ws_book_ticker(msg.get("result", {}))
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
        """Subscribe to tickers (price/volume) and book_ticker (bid/ask)."""
        symbols = list(self._symbol_info.keys())
        n_batches = (len(symbols) + _SUBSCRIBE_BATCH - 1) // _SUBSCRIBE_BATCH

        for i in range(0, len(symbols), _SUBSCRIBE_BATCH):
            batch = symbols[i:i + _SUBSCRIBE_BATCH]
            # spot.tickers for price, volume, high, low, change
            await ws.send(json.dumps({
                "time": int(time.time()),
                "channel": "spot.tickers",
                "event": "subscribe",
                "payload": batch,
            }))
            # spot.book_ticker for real-time top-of-book bid/ask
            await ws.send(json.dumps({
                "time": int(time.time()),
                "channel": "spot.book_ticker",
                "event": "subscribe",
                "payload": batch,
            }))

        logger.info(
            "Gate.io: subscribed to %d symbols (tickers + book_ticker) in %d batches",
            len(symbols), n_batches,
        )

    def _process_ws_ticker(self, data: dict[str, str]) -> None:
        """Parse a Gate.io WebSocket ticker update (price, volume, change).

        Bid/ask are NOT taken from this channel — they come from
        spot.book_ticker which provides real-time top-of-book data.
        """
        pair_id = data.get("currency_pair", "")
        info = self._symbol_info.get(pair_id)
        if not info:
            return

        symbol = pair_id.replace("_", "")
        existing = self.tickers.get(symbol)
        price = safe_float(data.get("last"))
        change_pct = safe_float(data.get("change_percentage"))
        denom = 1 + change_pct / 100
        open_price = price / denom if denom != 0 else price
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
            bid=existing.bid if existing else 0.0,
            ask=existing.ask if existing else 0.0,
            open_price=open_price,
            trades_count=0,
            last_update=datetime.now(),
            exchange=self.name,
        )
        self._notify()

    def _process_ws_book_ticker(self, data: dict[str, str]) -> None:
        """Parse a Gate.io spot.book_ticker update (real-time top-of-book).

        Fields: s=pair, b=best_bid, B=bid_size, a=best_ask, A=ask_size.
        Updates only bid/ask on the existing ticker.
        """
        pair_id = data.get("s", "")
        info = self._symbol_info.get(pair_id)
        if not info:
            return

        symbol = pair_id.replace("_", "")
        bid = safe_float(data.get("b"))
        ask = safe_float(data.get("a"))

        existing = self.tickers.get(symbol)
        if existing:
            self.tickers[symbol] = Ticker(
                symbol=existing.symbol,
                base_asset=existing.base_asset,
                quote_asset=existing.quote_asset,
                price=existing.price,
                price_change=existing.price_change,
                price_change_pct=existing.price_change_pct,
                high_24h=existing.high_24h,
                low_24h=existing.low_24h,
                volume_base=existing.volume_base,
                volume_quote=existing.volume_quote,
                bid=bid,
                ask=ask,
                open_price=existing.open_price,
                trades_count=existing.trades_count,
                last_update=datetime.now(),
                exchange=self.name,
            )
        else:
            self.tickers[symbol] = Ticker(
                symbol=symbol,
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=0.0,
                price_change=0.0,
                price_change_pct=0.0,
                high_24h=0.0,
                low_24h=0.0,
                volume_base=0.0,
                volume_quote=0.0,
                bid=bid,
                ask=ask,
                open_price=0.0,
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
        """REST fallback — only active when WS is down.

        Uses highest_bid/lowest_ask from ticker endpoint as approximate
        bid/ask.  Accurate bid/ask come from the WS spot.book_ticker
        channel during normal operation.
        """
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

            denom = 1 + change_pct / 100
            open_price = price / denom if denom != 0 else price
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
