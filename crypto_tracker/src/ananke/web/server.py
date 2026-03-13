"""
ANANKE Web Server — aiohttp backend for multi-exchange crypto tracker.

Each client sends its active filters (exchange, quote) via WebSocket.
The server broadcasts only matching tickers per client, minimizing payload.
Supports two views: tickers (per-exchange) and arbitrage (cross-exchange).
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from aiohttp.web_ws import WebSocketResponse

from ananke.coin_registry import CoinRegistry, build_registry
from ananke.config import ArbitrageConfig, WebConfig
from ananke.exchanges.manager import ExchangeManager
from ananke.fee_registry import FeeRegistry, build_fee_registry
from ananke.models import Ticker

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass
class ClientState:
    """Per-client filter state."""

    ws: WebSocketResponse
    view: str = "tickers"
    # Ticker view filters
    exchange: str = ""
    quote: str = "USDT"
    # Arbitrage view filters (multi-select: empty = ALL)
    arb_exchanges: list[str] = field(default_factory=list)
    arb_quote: str = "ALL"


def _serialize_ticker(t: Ticker) -> dict[str, str | int | float]:
    """Convert Ticker to a compact JSON-safe dict with short keys."""
    return {
        "s": t.symbol,
        "b": t.base_asset,
        "q": t.quote_asset,
        "p": t.price,
        "pc": t.price_change,
        "pp": t.price_change_pct,
        "h": t.high_24h,
        "l": t.low_24h,
        "vb": t.volume_base,
        "vq": t.volume_quote,
        "bi": t.bid,
        "ak": t.ask,
        "sp": round(t.spread, 4),
        "am": round(t.amplitude, 2),
        "tc": t.trades_count,
        "ex": t.exchange,
    }


def _filter_tickers(
    tickers: list[Ticker],
    exchange: str,
    quote: str,
) -> list[dict[str, str | int | float]]:
    """Filter and serialize tickers for a client's active filters."""
    result: list[dict[str, str | int | float]] = []
    for t in tickers:
        if t.exchange != exchange:
            continue
        if quote != "ALL" and t.quote_asset != quote:
            continue
        result.append(_serialize_ticker(t))
    return result


# ---------------------------------------------------------------------------
# Arbitrage engine — O(n) cross-exchange opportunity scanner
# ---------------------------------------------------------------------------


def _compute_arbitrage(
    all_tickers: list[Ticker],
    registry: CoinRegistry | None = None,
    fees: FeeRegistry | None = None,
    arb_config: ArbitrageConfig | None = None,
) -> list[dict[str, str | float]]:
    """
    Single-pass O(n) scan: group tickers by canonical identity + quote,
    track best bid and best ask across exchanges. Emit opportunities
    where best_bid > best_ask on different exchanges.

    Symbol verification: tickers are grouped by their CoinGecko canonical
    ID (resolved via the registry) to prevent phantom arbitrage from
    same-symbol-different-token collisions.  Ambiguous or unknown symbols
    are excluded from cross-exchange grouping.
    """
    best: dict[tuple[str, str], dict[str, str | float | bool]] = {}
    has_registry = registry is not None and registry.has_data()
    min_vol = arb_config.min_volume_quote if arb_config else 0.0
    max_spread = arb_config.max_pair_spread_pct / 100 if arb_config else 0.0
    min_profit = arb_config.min_profit_pct if arb_config else 0.0

    for t in all_tickers:
        if t.bid <= 0 or t.ask <= 0:
            continue
        # Pre-grouping liquidity filters
        if min_vol > 0 and t.volume_quote < min_vol:
            continue
        if max_spread > 0 and (t.ask - t.bid) / t.bid > max_spread:
            continue

        if has_registry:
            assert registry is not None
            canonical_id = registry.resolve(t.base_asset, t.exchange)
            if canonical_id is None:
                continue  # ambiguous, unknown, or unconfirmed — skip
            key = (canonical_id, t.quote_asset)
        else:
            # Graceful degradation — no registry available
            key = (t.base_asset, t.quote_asset)

        entry = best.get(key)
        if entry is None:
            best[key] = {
                "symbol": t.symbol,
                "base": t.base_asset,
                "quote": t.quote_asset,
                "max_bid": t.bid,
                "max_bid_ex": t.exchange,
                "max_bid_vol": t.volume_quote,
                "min_ask": t.ask,
                "min_ask_ex": t.exchange,
                "min_ask_vol": t.volume_quote,
                "first_ex": t.exchange,
                "multi": False,
            }
        else:
            if t.exchange != entry["first_ex"]:
                entry["multi"] = True
            if t.bid > entry["max_bid"]:
                entry["max_bid"] = t.bid
                entry["max_bid_ex"] = t.exchange
                entry["max_bid_vol"] = t.volume_quote
            if t.ask < entry["min_ask"]:
                entry["min_ask"] = t.ask
                entry["min_ask_ex"] = t.exchange
                entry["min_ask_vol"] = t.volume_quote

    results: list[dict[str, str | float]] = []
    for entry in best.values():
        if not entry["multi"]:
            continue
        if entry["max_bid_ex"] == entry["min_ask_ex"]:
            continue
        if entry["max_bid"] <= entry["min_ask"]:
            continue
        # Filter non-executable arbs (transfers blocked)
        if fees and not fees.can_execute_arb(
            bid_exchange=entry["max_bid_ex"],
            ask_exchange=entry["min_ask_ex"],
            symbol=entry["base"],
        ):
            continue
        bid = entry["max_bid"]
        ask = entry["min_ask"]
        profit = (bid - ask) / ask * 100

        if min_profit > 0 and profit < min_profit:
            continue

        # Net profit after taker fees + withdrawal cost in quote
        # Withdrawal is from ask_exchange (where we buy)
        if fees:
            net_pf = fees.net_profit_after_taker(
                bid=bid,
                ask=ask,
                bid_exchange=entry["max_bid_ex"],
                ask_exchange=entry["min_ask_ex"],
            )
            wf = round(fees.withdrawal_cost_quote(
                entry["base"], bid, exchange=entry["min_ask_ex"],
            ), 8)
        else:
            net_pf = profit
            wf = 0.0

        # True net profit: accounts for withdrawal fee relative to trade size
        ref_size = arb_config.ref_trade_size if arb_config else 1000.0
        tnpf = net_pf - (wf / ref_size) * 100 if ref_size > 0 and wf > 0 else net_pf

        results.append({
            "s": entry["symbol"],
            "b": entry["base"],
            "q": entry["quote"],
            "bx": entry["max_bid_ex"],
            "ax": entry["min_ask_ex"],
            "bi": round(bid, 8),
            "ak": round(ask, 8),
            "pf": round(profit, 4),
            "npf": round(net_pf, 4),
            "tnpf": round(tnpf, 4),
            "wf": wf,
            "bv": entry["max_bid_vol"],
            "av": entry["min_ask_vol"],
        })

    return results


def _filter_arbitrage(
    arb: list[dict[str, str | float]],
    exchanges: list[str],
    quote: str,
) -> list[dict[str, str | float]]:
    """Filter arbitrage results by exchange(s) and/or quote.

    Exchange filter semantics:
    - empty list: no filter (ALL)
    - 1 exchange: show opps where either side matches (any involvement)
    - 2+ exchanges: show opps where BOTH sides are in the set (pair filter)
    """
    result: list[dict[str, str | float]] = []
    ex_set = set(exchanges) if exchanges else None
    single = len(exchanges) == 1
    for opp in arb:
        if quote != "ALL" and opp["q"] != quote:
            continue
        if ex_set is not None:
            if single:
                ex = next(iter(ex_set))
                if opp["bx"] != ex and opp["ax"] != ex:
                    continue
            else:
                if opp["bx"] not in ex_set or opp["ax"] not in ex_set:
                    continue
        result.append(opp)
    return result


async def _index_handler(request: web.Request) -> web.Response:
    """Serve the single-page HTML frontend."""
    template = TEMPLATES_DIR / "index.html"
    if not template.exists():
        raise web.HTTPNotFound(text="Template not found")
    html = template.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle browser WebSocket connections with per-client filtering."""
    clients: dict[int, ClientState] = request.app["clients"]
    manager: ExchangeManager = request.app["manager"]
    config: WebConfig = request.app["web_config"]

    ws = WebSocketResponse(heartbeat=config.ws_heartbeat)
    await ws.prepare(request)

    default_exchange = manager.exchange_names[0] if manager.exchange_names else ""
    state = ClientState(ws=ws, exchange=default_exchange)
    client_id = id(ws)
    clients[client_id] = state
    logger.info("Client connected (%d total)", len(clients))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    valid_exchanges = set(manager.exchange_names)

                    if "view" in data:
                        v = str(data["view"])
                        if v in ("tickers", "arbitrage"):
                            state.view = v

                    # Ticker view filters
                    if "exchange" in data and data["exchange"]:
                        ex = str(data["exchange"])
                        if ex in valid_exchanges:
                            state.exchange = ex
                    if "quote" in data and data["quote"]:
                        state.quote = str(data["quote"])

                    # Arbitrage view filters (multi-select)
                    if "arb_exchanges" in data:
                        raw = data["arb_exchanges"]
                        if isinstance(raw, list):
                            state.arb_exchanges = [
                                str(e) for e in raw
                                if str(e) in valid_exchanges
                            ]
                        elif raw == "" or raw is None:
                            state.arb_exchanges = []
                    if "arb_quote" in data:
                        state.arb_quote = str(data["arb_quote"])

                except (json.JSONDecodeError, TypeError):
                    pass
    finally:
        clients.pop(client_id, None)
        logger.info("Client disconnected (%d total)", len(clients))

    return ws


async def _broadcast_loop(app: web.Application) -> None:
    """Broadcast filtered data to each client based on their view and filters."""
    clients: dict[int, ClientState] = app["clients"]
    manager: ExchangeManager = app["manager"]
    config: WebConfig = app["web_config"]
    registry: CoinRegistry = app["coin_registry"]
    fees: FeeRegistry = app["fee_registry"]
    arb_config: ArbitrageConfig = app["arb_config"]

    while True:
        if clients and manager.has_data():
            all_tickers = manager.get_all_tickers()
            exchanges = manager.exchange_names

            # Split clients by view
            ticker_groups: dict[tuple[str, str], list[ClientState]] = {}
            arb_groups: dict[tuple[frozenset[str], str], list[ClientState]] = {}

            for state in list(clients.values()):
                if state.view == "arbitrage":
                    key = (frozenset(state.arb_exchanges), state.arb_quote)
                    arb_groups.setdefault(key, []).append(state)
                else:
                    key = (state.exchange, state.quote)
                    ticker_groups.setdefault(key, []).append(state)

            dead: list[int] = []

            # --- Ticker broadcasts ---
            for (exchange, quote), group_clients in ticker_groups.items():
                filtered = _filter_tickers(all_tickers, exchange, quote)
                payload = json.dumps(
                    {
                        "view": "tickers",
                        "tickers": filtered,
                        "exchanges": exchanges,
                        "active": {"exchange": exchange, "quote": quote},
                    },
                    separators=(",", ":"),
                )
                for state in group_clients:
                    try:
                        await state.ws.send_str(payload)
                    except (ConnectionError, OSError):
                        dead.append(id(state.ws))

            # --- Arbitrage broadcasts (compute once, filter per group) ---
            if arb_groups:
                arb_all = _compute_arbitrage(
                    all_tickers, registry, fees, arb_config,
                )

                for (arb_exs, arb_q), group_clients in arb_groups.items():
                    filtered = _filter_arbitrage(arb_all, list(arb_exs), arb_q)
                    payload = json.dumps(
                        {
                            "view": "arbitrage",
                            "arb": filtered,
                            "exchanges": exchanges,
                            "active": {
                                "arb_exchanges": sorted(arb_exs),
                                "arb_quote": arb_q,
                            },
                        },
                        separators=(",", ":"),
                    )
                    for state in group_clients:
                        try:
                            await state.ws.send_str(payload)
                        except (ConnectionError, OSError):
                            dead.append(id(state.ws))

            for cid in dead:
                clients.pop(cid, None)

        await asyncio.sleep(config.broadcast_interval)


async def _on_startup(app: web.Application) -> None:
    app["broadcast_task"] = asyncio.create_task(_broadcast_loop(app))


async def _on_cleanup(app: web.Application) -> None:
    task: asyncio.Task[None] = app["broadcast_task"]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def start_web(
    manager: ExchangeManager,
    config: WebConfig | None = None,
    arb_config: ArbitrageConfig | None = None,
) -> web.AppRunner:
    """Create and start the aiohttp web server."""
    config = config or WebConfig()
    arb_config = arb_config or ArbitrageConfig()

    registry = await build_registry()
    fees = await build_fee_registry()

    app = web.Application()
    app["manager"] = manager
    app["clients"] = {}
    app["web_config"] = config
    app["arb_config"] = arb_config
    app["coin_registry"] = registry
    app["fee_registry"] = fees
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_get("/", _index_handler)
    app.router.add_get("/ws", _ws_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()

    logger.info("Web server started on http://%s:%d", config.host, config.port)
    return runner
