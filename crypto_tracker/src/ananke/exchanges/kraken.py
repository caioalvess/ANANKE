"""Kraken exchange implementation — WebSocket v2 primary, REST polling fallback."""

import asyncio
import contextlib
import json
import logging
from datetime import datetime

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from ananke.config import KrakenConfig
from ananke.exchanges.base import Exchange
from ananke.exchanges.utils import safe_float
from ananke.models import Ticker

logger = logging.getLogger(__name__)


def _val(src: dict, field: str, idx: int) -> float:
    """Safe array access for Kraken's array-based ticker fields."""
    arr = src.get(field, [])
    return safe_float(arr[idx]) if idx < len(arr) else 0.0


# Kraken legacy assets that need normalization.
_ASSET_REMAP: dict[str, str] = {
    "XXBT": "BTC",
    "XBT": "BTC",
    "XETH": "ETH",
    "XLTC": "LTC",
    "XXLM": "XLM",
    "XXMR": "XMR",
    "XXRP": "XRP",
    "XETC": "ETC",
    "XXTZ": "XTZ",
    "XMLN": "MLN",
    "XREP": "REP",
    "XXDG": "DOGE",
    "ZUSD": "USD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZJPY": "JPY",
    "ZCAD": "CAD",
    "ZAUD": "AUD",
    "ZCHF": "CHF",
}


def _normalize_asset(asset: str) -> str:
    """Normalize Kraken's legacy asset names to standard tickers."""
    if asset in _ASSET_REMAP:
        return _ASSET_REMAP[asset]
    return asset


class KrakenExchange(Exchange):
    """
    Kraken spot market implementation.

    Primary: WebSocket v2 wss://ws.kraken.com/v2
      - Per-symbol subscribe using wsname (BASE/QUOTE format)
      - Ping {"method": "ping"} every 25s

    Fallback: REST polling /0/public/Ticker
      - Activates after ws_max_failures consecutive WS failures
    """

    def __init__(self, config: KrakenConfig | None = None) -> None:
        super().__init__("Kraken")
        self.config = config or KrakenConfig()
        self._ws_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._symbol_info: dict[str, dict[str, str]] = {}
        # wsname → pair_key mapping for WS message routing
        self._ws_symbols: list[str] = []
        self._wsname_to_key: dict[str, str] = {}
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
        url = f"{self.config.rest_url}/0/public/AssetPairs"
        self._symbol_info.clear()
        self._ws_symbols.clear()
        self._wsname_to_key.clear()

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if data.get("error"):
            logger.error("Kraken AssetPairs error: %s", data["error"])
            return

        for pair_key, info in data.get("result", {}).items():
            if pair_key.endswith(".d"):
                continue
            if info.get("status") != "online":
                continue

            base = _normalize_asset(info["base"])
            quote = _normalize_asset(info["quote"])
            wsname = info.get("wsname", "")

            self._symbol_info[pair_key] = {
                "base": base,
                "quote": quote,
                "symbol": f"{base}{quote}",
                "wsname": wsname,
            }

            if wsname:
                self._ws_symbols.append(wsname)
                self._wsname_to_key[wsname] = pair_key

        logger.info(
            "Kraken: loaded %d spot symbols (%d with wsname)",
            len(self._symbol_info), len(self._ws_symbols),
        )

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

    # --- WebSocket v2 ---

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
                    logger.info("Kraken WebSocket v2 connected")
                    self._ws_connected = True
                    self._ws_failures = 0

                    # Subscribe to ticker in batches to avoid oversized messages
                    if self._ws_symbols:
                        batch_size = 100
                        for i in range(0, len(self._ws_symbols), batch_size):
                            batch = self._ws_symbols[i:i + batch_size]
                            await ws.send(json.dumps({
                                "method": "subscribe",
                                "params": {
                                    "channel": "ticker",
                                    "symbol": batch,
                                },
                            }))
                        logger.info(
                            "Kraken: subscribed to %d ticker symbols in %d batches",
                            len(self._ws_symbols),
                            (len(self._ws_symbols) + batch_size - 1) // batch_size,
                        )

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            channel = msg.get("channel")
                            msg_type = msg.get("type")
                            if channel == "ticker" and msg_type in ("snapshot", "update"):
                                self._process_ws_tickers(msg.get("data", []))
                        except json.JSONDecodeError:
                            logger.warning("Kraken: invalid JSON from WS")

            except (ConnectionClosed, InvalidURI, OSError) as e:
                self._ws_connected = False
                self._ws_failures += 1
                delay = min(cfg.ws_reconnect_delay * self._ws_failures, 30)
                logger.warning(
                    "Kraken WS disconnected (%d/%d): %s — reconnecting in %ds",
                    self._ws_failures, cfg.ws_max_failures, e, delay,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        self._ws_connected = False

    def _process_ws_tickers(self, data: list[dict]) -> None:
        """Parse Kraken WS v2 ticker data.

        Each item has: symbol (wsname like "BTC/USD"), bid, ask, last,
        volume, vwap, high, low, change, change_pct.
        """
        now = datetime.now()
        for item in data:
            wsname = item.get("symbol", "")
            pair_key = self._wsname_to_key.get(wsname)
            if not pair_key:
                continue
            info = self._symbol_info.get(pair_key)
            if not info:
                continue

            price = safe_float(item.get("last"))
            change = safe_float(item.get("change"))
            change_pct = safe_float(item.get("change_pct"))
            volume_base = safe_float(item.get("volume"))
            vwap = safe_float(item.get("vwap"))
            volume_quote = vwap * volume_base if vwap else 0.0
            open_price = price - change if change else 0.0

            self.tickers[info["symbol"]] = Ticker(
                symbol=info["symbol"],
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=price,
                price_change=change,
                price_change_pct=change_pct,
                high_24h=safe_float(item.get("high")),
                low_24h=safe_float(item.get("low")),
                volume_base=volume_base,
                volume_quote=volume_quote,
                bid=safe_float(item.get("bid")),
                ask=safe_float(item.get("ask")),
                open_price=open_price,
                trades_count=0,
                last_update=now,
                exchange=self.name,
            )
        self._notify()

    # --- REST fallback ---

    async def _poll_fallback(self) -> None:
        """REST polling — always active as primary data source.

        Kraken WS v2 with 1400+ symbols can be unstable. REST ensures
        data availability regardless of WS connection state.
        """
        while self._running:
            try:
                await self._fetch_tickers()
            except aiohttp.ClientError as e:
                logger.warning("Kraken REST fallback error: %s", e)
            except asyncio.CancelledError:
                break

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_tickers(self) -> None:
        session = await self._get_session()
        url = f"{self.config.rest_url}/0/public/Ticker"

        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if data.get("error"):
            logger.warning("Kraken Ticker error: %s", data["error"])
            return

        now = datetime.now()

        for pair_key, item in data.get("result", {}).items():
            info = self._symbol_info.get(pair_key)
            if not info:
                continue

            price = _val(item, "c", 0)
            open_price = safe_float(item.get("o"))
            price_change = price - open_price if open_price else 0.0
            price_change_pct = (price_change / open_price * 100) if open_price else 0.0

            volume_base = _val(item, "v", 1)
            vwap_24h = _val(item, "p", 1)
            volume_quote = vwap_24h * volume_base
            rest_bid = _val(item, "b", 0)
            rest_ask = _val(item, "a", 0)
            sym = info["symbol"]

            existing = self.tickers.get(sym)
            if existing:
                existing.price = price
                existing.price_change = price_change
                existing.price_change_pct = price_change_pct
                existing.high_24h = _val(item, "h", 1)
                existing.low_24h = _val(item, "l", 1)
                existing.volume_base = volume_base
                existing.volume_quote = volume_quote
                existing.open_price = open_price
                existing.trades_count = int(_val(item, "t", 1))
                if rest_bid > 0:
                    existing.bid = rest_bid
                if rest_ask > 0:
                    existing.ask = rest_ask
                existing.last_update = now
                continue

            self.tickers[sym] = Ticker(
                symbol=sym,
                base_asset=info["base"],
                quote_asset=info["quote"],
                price=price,
                price_change=price_change,
                price_change_pct=price_change_pct,
                high_24h=_val(item, "h", 1),
                low_24h=_val(item, "l", 1),
                volume_base=volume_base,
                volume_quote=volume_quote,
                bid=rest_bid,
                ask=rest_ask,
                open_price=open_price,
                trades_count=int(_val(item, "t", 1)),
                last_update=now,
                exchange=self.name,
            )

        self._notify()
