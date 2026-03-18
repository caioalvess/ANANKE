"""Binance exchange implementation using WebSocket streams."""

import asyncio
import contextlib
import json
import logging
from datetime import datetime

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from ananke.config import BinanceConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


class BinanceExchange(Exchange):
    """
    Binance spot market implementation.

    Uses the combined WebSocket stream `!ticker@arr` which pushes
    24h rolling ticker stats for ALL symbols every ~1 second.
    """

    def __init__(self, config: BinanceConfig | None = None) -> None:
        super().__init__("Binance")
        self.config = config or BinanceConfig()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task[None] | None = None
        self._book_task: asyncio.Task[None] | None = None
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
        """Fetch symbol metadata from Binance REST API."""
        session = await self._get_session()
        url = f"{self.config.rest_url}/api/v3/exchangeInfo"

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        for s in data.get("symbols", []):
            if s["status"] == "TRADING" and s.get("isSpotTradingAllowed", False):
                self._symbol_info[s["symbol"]] = {
                    "base": s["baseAsset"],
                    "quote": s["quoteAsset"],
                }

        logger.info("Binance: loaded %d spot symbols", len(self._symbol_info))

    async def connect(self) -> None:
        """Connect to Binance WebSocket streams and start processing."""
        self._running = True
        self._task = asyncio.create_task(self._listen())
        self._book_task = asyncio.create_task(self._poll_book_ticker())

    async def disconnect(self) -> None:
        """Stop the WebSocket listeners and close HTTP session."""
        self._running = False
        if self._ws:
            await self._ws.close()
        for task in (self._task, self._book_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._session and not self._session.closed:
            await self._session.close()

    async def _listen(self) -> None:
        """Main WebSocket listener loop with auto-reconnect."""
        cfg = self.config
        while self._running:
            try:
                async with websockets.connect(
                    cfg.ws_url,
                    ping_interval=cfg.ws_ping_interval,
                    ping_timeout=cfg.ws_ping_timeout,
                    close_timeout=cfg.ws_close_timeout,
                ) as ws:
                    self._ws = ws
                    logger.info("Binance WebSocket connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            self._process_ticker_array(data)
                        except json.JSONDecodeError:
                            logger.warning("Binance: invalid JSON received")
            except (ConnectionClosed, InvalidURI, OSError) as e:
                logger.warning(
                    "Binance WS disconnected: %s — reconnecting in %ds",
                    e,
                    cfg.ws_reconnect_delay,
                )
                await asyncio.sleep(cfg.ws_reconnect_delay)
            except asyncio.CancelledError:
                break

    async def _poll_book_ticker(self) -> None:
        """Poll REST /api/v3/ticker/bookTicker for accurate bid/ask.

        The !ticker@arr WS stream can lag on bid/ask for less liquid pairs.
        This REST endpoint returns real-time best bid/ask for ALL symbols
        in a single fast call (~0.3s for 3500+ symbols).
        """
        while self._running:
            try:
                session = await self._get_session()
                url = f"{self.config.rest_url}/api/v3/ticker/bookTicker"
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                now = datetime.now()
                for item in data:
                    symbol = str(item.get("symbol", ""))
                    existing = self.tickers.get(symbol)
                    if not existing:
                        continue
                    bid = safe_float(item.get("bidPrice"))
                    ask = safe_float(item.get("askPrice"))
                    if bid > 0:
                        existing.bid = bid
                    if ask > 0:
                        existing.ask = ask
                    existing.last_update = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Binance bookTicker REST error: %s", e)

            await asyncio.sleep(0.5)  # Poll every 0.5 seconds

    def _process_ticker_array(self, data: list[dict[str, str | int | float]]) -> None:
        """Parse the 24h ticker array and update internal state.

        Updates price, volume, change etc. from the WS stream.
        Bid/ask are only set here as initial values for new symbols;
        the bookTicker REST poll overwrites them with accurate data.
        """
        now = datetime.now()
        for item in data:
            symbol = str(item.get("s", ""))
            info = self._symbol_info.get(symbol)
            if not info:
                continue

            existing = self.tickers.get(symbol)
            if existing:
                # Update fields but KEEP bid/ask from bookTicker poll
                existing.price = safe_float(item.get("c"))
                existing.price_change = safe_float(item.get("p"))
                existing.price_change_pct = safe_float(item.get("P"))
                existing.high_24h = safe_float(item.get("h"))
                existing.low_24h = safe_float(item.get("l"))
                existing.volume_base = safe_float(item.get("v"))
                existing.volume_quote = safe_float(item.get("q"))
                existing.open_price = safe_float(item.get("o"))
                existing.trades_count = int(safe_float(item.get("n")))
                existing.last_update = now
            else:
                # New symbol — use WS bid/ask as initial values
                self.tickers[symbol] = Ticker(
                    symbol=symbol,
                    base_asset=info["base"],
                    quote_asset=info["quote"],
                    price=safe_float(item.get("c")),
                    price_change=safe_float(item.get("p")),
                    price_change_pct=safe_float(item.get("P")),
                    high_24h=safe_float(item.get("h")),
                    low_24h=safe_float(item.get("l")),
                    volume_base=safe_float(item.get("v")),
                    volume_quote=safe_float(item.get("q")),
                    bid=safe_float(item.get("b")),
                    ask=safe_float(item.get("a")),
                    open_price=safe_float(item.get("o")),
                    trades_count=int(safe_float(item.get("n"))),
                    last_update=now,
                    exchange=self.name,
                )
        self._notify()
