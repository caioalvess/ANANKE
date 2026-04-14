"""KuCoin exchange implementation — WebSocket primary, REST polling fallback."""

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from ananke.config import KucoinConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


class KucoinExchange(Exchange):
    """
    KuCoin spot market implementation.

    Primary: WebSocket via token from POST /api/v1/bullet-public
      - Single subscribe to /market/ticker:all for ALL tickers
      - Ping {"type": "ping"} at server-specified interval

    Fallback: REST polling /api/v1/market/allTickers
      - Activates after ws_max_failures consecutive WS failures
    """

    def __init__(self, config: KucoinConfig | None = None) -> None:
        super().__init__("KuCoin")
        self.config = config or KucoinConfig()
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

    async def _get_ws_token(self) -> tuple[str, int] | None:
        """POST bullet-public to get WS endpoint + token.

        Returns (ws_url, ping_interval_sec) or None on failure.
        """
        session = await self._get_session()
        try:
            async with session.post(self.config.ws_bullet_url) as resp:
                if resp.status != 200:
                    logger.warning("KuCoin bullet-public returned %d", resp.status)
                    return None
                data = await resp.json()
        except Exception:
            logger.warning("KuCoin bullet-public unreachable", exc_info=True)
            return None

        if str(data.get("code")) != "200000":
            logger.warning("KuCoin bullet-public error: %s", data.get("msg"))
            return None

        d = data.get("data", {})
        token = d.get("token", "")
        servers = d.get("instanceServers", [])
        if not token or not servers:
            return None

        srv = servers[0]
        endpoint = srv.get("endpoint", "")
        ping_ms = srv.get("pingInterval", 18000)
        connect_id = uuid.uuid4().hex[:12]
        ws_url = f"{endpoint}?token={token}&connectId={connect_id}"
        return ws_url, max(ping_ms // 1000 - 2, 5)

    async def _ws_listen(self) -> None:
        cfg = self.config
        while self._running:
            # Get fresh token each reconnect
            token_data = await self._get_ws_token()
            if token_data is None:
                self._ws_failures += 1
                delay = min(cfg.ws_reconnect_delay * self._ws_failures, 30)
                logger.warning(
                    "KuCoin WS token failed (%d/%d) — retry in %ds",
                    self._ws_failures, cfg.ws_max_failures, delay,
                )
                await asyncio.sleep(delay)
                continue

            ws_url, ping_sec = token_data

            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=None,  # We handle ping ourselves
                    close_timeout=cfg.ws_close_timeout,
                ) as ws:
                    logger.info("KuCoin WebSocket connected")
                    self._ws_connected = True
                    self._ws_failures = 0

                    # Subscribe to all tickers
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "topic": "/market/ticker:all",
                        "privateChannel": False,
                        "response": True,
                        "id": uuid.uuid4().hex[:8],
                    }))

                    # Run ping + listen concurrently
                    ping_task = asyncio.create_task(
                        self._ping_loop(ws, ping_sec),
                    )
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            try:
                                msg = json.loads(raw)
                                if msg.get("type") == "message":
                                    self._process_ws_ticker(msg)
                            except json.JSONDecodeError:
                                logger.warning("KuCoin: invalid JSON from WS")
                    finally:
                        ping_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await ping_task

            except (ConnectionClosed, InvalidURI, OSError) as e:
                self._ws_connected = False
                self._ws_failures += 1
                delay = min(cfg.ws_reconnect_delay * self._ws_failures, 30)
                logger.warning(
                    "KuCoin WS disconnected (%d/%d): %s — reconnecting in %ds",
                    self._ws_failures, cfg.ws_max_failures, e, delay,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        self._ws_connected = False

    async def _ping_loop(self, ws: object, interval: int) -> None:
        """Send KuCoin ping at the server-specified interval."""
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({
                    "type": "ping",
                    "id": uuid.uuid4().hex[:8],
                }))
            except (ConnectionClosed, OSError):
                break

    def _process_ws_ticker(self, msg: dict) -> None:
        """Parse KuCoin WS ticker message.

        topic: /market/ticker:BTC-USDT
        WS data fields: {bestAsk, bestAskSize, bestBid, bestBidSize, price,
                         sequence, size, time}

        WS lacks: high, low, vol, volValue, changePrice, changeRate.
        These are preserved from existing ticker (populated by REST).

        Preserves existing bid/ask when WS omits or zeros them, so REST
        fallback values aren't wiped by a partial push.
        """
        topic = msg.get("topic", "")
        # Extract symbol from topic: /market/ticker:BTC-USDT → BTC-USDT
        if ":" not in topic:
            return
        kc_symbol = topic.split(":", 1)[1]
        info = self._symbol_info.get(kc_symbol)
        if not info:
            return

        data = msg.get("data", {})
        symbol = kc_symbol.replace("-", "")
        existing = self.tickers.get(symbol)

        price = safe_float(data.get("price"))
        ws_bid = safe_float(data.get("bestBid"))
        ws_ask = safe_float(data.get("bestAsk"))
        now = datetime.now()

        if existing:
            if price > 0:
                existing.price = price
            if ws_bid > 0:
                existing.bid = ws_bid
            if ws_ask > 0:
                existing.ask = ws_ask
            existing.last_update = now
            self._notify()
            return

        self.tickers[symbol] = Ticker(
            symbol=symbol,
            base_asset=info["base"],
            quote_asset=info["quote"],
            price=price,
            price_change=0.0,
            price_change_pct=0.0,
            high_24h=0.0,
            low_24h=0.0,
            volume_base=0.0,
            volume_quote=0.0,
            bid=ws_bid,
            ask=ws_ask,
            open_price=0.0,
            trades_count=0,
            last_update=now,
            exchange=self.name,
        )
        self._notify()

    # --- REST fallback ---

    async def _poll_fallback(self) -> None:
        """REST polling — always active as primary data source.

        KuCoin WS /market/ticker:all only provides price/bid/ask.
        REST fills in high, low, volume, change data.
        """
        while self._running:
            try:
                await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("KuCoin REST fallback error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
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

            symbol = kc_symbol.replace("-", "")
            price = safe_float(item.get("last"))
            change_price = safe_float(item.get("changePrice"))
            change_rate = safe_float(item.get("changeRate"))

            existing = self.tickers.get(symbol)
            rest_bid = safe_float(item.get("buy"))
            rest_ask = safe_float(item.get("sell"))

            if existing:
                existing.price = price
                existing.price_change = change_price
                existing.price_change_pct = change_rate * 100
                existing.high_24h = safe_float(item.get("high"))
                existing.low_24h = safe_float(item.get("low"))
                existing.volume_base = safe_float(item.get("vol"))
                existing.volume_quote = safe_float(item.get("volValue"))
                existing.open_price = safe_float(item.get("open"))
                # KuCoin REST always provides bid/ask — use them
                if rest_bid > 0:
                    existing.bid = rest_bid
                if rest_ask > 0:
                    existing.ask = rest_ask
                existing.last_update = now
            else:
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
                    bid=rest_bid,
                    ask=rest_ask,
                    open_price=safe_float(item.get("open")),
                    trades_count=0,
                    last_update=now,
                    exchange=self.name,
                )

        self._notify()
