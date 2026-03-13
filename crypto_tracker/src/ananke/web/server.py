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
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from aiohttp.web_ws import WebSocketResponse

from ananke.alerts import AlertEngine
from ananke.coin_registry import CoinRegistry, build_registry
from ananke.config import AlertConfig, ArbitrageConfig, WebConfig
from ananke.exchanges.manager import ExchangeManager
from ananke.fee_registry import FeeRegistry, build_fee_registry
from ananke.metrics import MetricsCollector
from ananke.models import Ticker
from ananke.orderbook import OrderBookProbe
from ananke.triangular import compute_triangular_all

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
    arb_mode: str = "transfer"  # "transfer" or "hedge"
    # Triangular view filters
    tri_exchange: str = ""  # single exchange (triangular is intra-exchange)


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
        "ts": int(t.last_update.timestamp() * 1000),
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
    *,
    mode: str = "transfer",
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

        ts = int(t.last_update.timestamp() * 1000)

        entry = best.get(key)
        if entry is None:
            best[key] = {
                "symbol": t.symbol,
                "base": t.base_asset,
                "quote": t.quote_asset,
                "max_bid": t.bid,
                "max_bid_ex": t.exchange,
                "max_bid_vol": t.volume_quote,
                "max_bid_ts": ts,
                "min_ask": t.ask,
                "min_ask_ex": t.exchange,
                "min_ask_vol": t.volume_quote,
                "min_ask_ts": ts,
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
                entry["max_bid_ts"] = ts
            if t.ask < entry["min_ask"]:
                entry["min_ask"] = t.ask
                entry["min_ask_ex"] = t.exchange
                entry["min_ask_vol"] = t.volume_quote
                entry["min_ask_ts"] = ts

    is_hedge = mode == "hedge"

    results: list[dict[str, str | float]] = []
    for entry in best.values():
        if not entry["multi"]:
            continue
        if entry["max_bid_ex"] == entry["min_ask_ex"]:
            continue
        if entry["max_bid"] <= entry["min_ask"]:
            continue
        # Transfer mode: filter non-executable arbs (transfers blocked)
        if not is_hedge and fees and not fees.can_execute_arb(
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

        # Net profit after taker fees
        if fees:
            net_pf = fees.net_profit_after_taker(
                bid=bid,
                ask=ask,
                bid_exchange=entry["max_bid_ex"],
                ask_exchange=entry["min_ask_ex"],
            )
            # Rebal cost: withdrawal fee from ask exchange (informational)
            rc = round(fees.withdrawal_cost_quote(
                entry["base"], bid, exchange=entry["min_ask_ex"],
            ), 8)
        else:
            net_pf = profit
            rc = 0.0

        if is_hedge:
            wf = 0.0
            tnpf = net_pf
        else:
            wf = rc
            ref_size = arb_config.ref_trade_size if arb_config else 1000.0
            tnpf = net_pf - (wf / ref_size) * 100 if ref_size > 0 and wf > 0 else net_pf

        now_ms = int(time.time() * 1000)
        bts = entry["max_bid_ts"]
        ats = entry["min_ask_ts"]
        age = max(now_ms - bts, now_ms - ats)
        msv = min(entry["max_bid_vol"], entry["min_ask_vol"])

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
            "rc": rc,
            "msv": msv,
            "bv": entry["max_bid_vol"],
            "av": entry["min_ask_vol"],
            "bts": bts,
            "ats": ats,
            "age": age,
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


async def _metrics_handler(request: web.Request) -> web.Response:
    """REST endpoint for metrics data."""
    metrics: MetricsCollector = request.app["metrics"]
    data = metrics.get_metrics()
    return web.json_response(data)


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
                        if v in ("tickers", "arbitrage", "triangular", "metrics"):
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
                    if "arb_mode" in data:
                        m = str(data["arb_mode"])
                        if m in ("transfer", "hedge"):
                            state.arb_mode = m
                    if "tri_exchange" in data:
                        tex = str(data["tri_exchange"])
                        if tex in valid_exchanges or tex == "":
                            state.tri_exchange = tex

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
    depth_probe: OrderBookProbe | None = app.get("depth_probe")
    alert_engine: AlertEngine | None = app.get("alert_engine")
    metrics: MetricsCollector = app["metrics"]

    while True:
        if clients and manager.has_data():
            all_tickers = manager.get_all_tickers()
            exchanges = manager.exchange_names

            # Split clients by view
            ticker_groups: dict[tuple[str, str], list[ClientState]] = {}
            arb_groups: dict[
                tuple[str, frozenset[str], str], list[ClientState]
            ] = {}
            tri_groups: dict[str, list[ClientState]] = {}
            metrics_clients: list[ClientState] = []

            for state in list(clients.values()):
                if state.view == "metrics":
                    metrics_clients.append(state)
                elif state.view == "arbitrage":
                    key = (
                        state.arb_mode,
                        frozenset(state.arb_exchanges),
                        state.arb_quote,
                    )
                    arb_groups.setdefault(key, []).append(state)
                elif state.view == "triangular":
                    tri_groups.setdefault(
                        state.tri_exchange, [],
                    ).append(state)
                else:
                    key = (state.exchange, state.quote)
                    ticker_groups.setdefault(key, []).append(state)

            dead: list[int] = []

            server_ts = int(time.time() * 1000)

            # --- Ticker broadcasts ---
            for (exchange, quote), group_clients in ticker_groups.items():
                filtered = _filter_tickers(all_tickers, exchange, quote)
                payload = json.dumps(
                    {
                        "view": "tickers",
                        "tickers": filtered,
                        "exchanges": exchanges,
                        "active": {"exchange": exchange, "quote": quote},
                        "server_ts": server_ts,
                    },
                    separators=(",", ":"),
                )
                for state in group_clients:
                    try:
                        await state.ws.send_str(payload)
                    except (ConnectionError, OSError):
                        dead.append(id(state.ws))

            # --- Arbitrage broadcasts (compute once per mode, filter per group) ---
            arb_by_mode: dict[str, list[dict[str, str | float]]] = {}
            if arb_groups or metrics_clients or (alert_engine and alert_engine.enabled):
                for (arb_mode, _arb_exs, _arb_q), _group_clients in arb_groups.items():
                    if arb_mode not in arb_by_mode:
                        arb_by_mode[arb_mode] = _compute_arbitrage(
                            all_tickers, registry, fees, arb_config,
                            mode=arb_mode,
                        )
                        if depth_probe:
                            try:
                                await depth_probe.enrich_arb_results(
                                    arb_by_mode[arb_mode],
                                    top_n=arb_config.depth_top_n,
                                    trade_size=arb_config.ref_trade_size,
                                    fees=fees,
                                )
                            except Exception:
                                logger.debug(
                                    "Depth enrichment failed", exc_info=True,
                                )

                # Record metrics from the default mode (transfer)
                default_arb = arb_by_mode.get("transfer")
                if default_arb is None:
                    default_arb = _compute_arbitrage(
                        all_tickers, registry, fees, arb_config,
                        mode="transfer",
                    )
                    arb_by_mode["transfer"] = default_arb
                metrics.record(default_arb)

                # Enrich arb results with freq/dur
                for mode_results in arb_by_mode.values():
                    metrics.enrich_arb_results(mode_results)

                for (arb_mode, arb_exs, arb_q), group_clients in arb_groups.items():
                    filtered = _filter_arbitrage(
                        arb_by_mode[arb_mode], list(arb_exs), arb_q,
                    )
                    payload = json.dumps(
                        {
                            "view": "arbitrage",
                            "arb": filtered,
                            "exchanges": exchanges,
                            "active": {
                                "arb_exchanges": sorted(arb_exs),
                                "arb_quote": arb_q,
                                "arb_mode": arb_mode,
                            },
                            "server_ts": server_ts,
                        },
                        separators=(",", ":"),
                    )
                    for state in group_clients:
                        try:
                            await state.ws.send_str(payload)
                        except (ConnectionError, OSError):
                            dead.append(id(state.ws))

            # --- Alert check (fire-and-forget) ---
            if alert_engine and alert_engine.enabled:
                alert_mode = alert_engine._alert_mode
                if alert_mode not in arb_by_mode:
                    arb_by_mode[alert_mode] = _compute_arbitrage(
                        all_tickers, registry, fees, arb_config,
                        mode=alert_mode,
                    )
                asyncio.create_task(
                    alert_engine.check_and_alert(arb_by_mode[alert_mode]),
                )

            # --- Triangular broadcasts ---
            if tri_groups:
                taker_fees = {
                    ex: fees.taker_fee(ex) for ex in exchanges
                } if fees else None
                tri_cache: dict[str, list[dict]] = {}

                for tri_ex, group_clients in tri_groups.items():
                    if tri_ex not in tri_cache:
                        tri_cache[tri_ex] = compute_triangular_all(
                            all_tickers,
                            taker_fees=taker_fees,
                            exchange_filter=tri_ex,
                        )
                    payload = json.dumps(
                        {
                            "view": "triangular",
                            "tri": tri_cache[tri_ex],
                            "exchanges": exchanges,
                            "active": {"tri_exchange": tri_ex},
                            "server_ts": server_ts,
                        },
                        separators=(",", ":"),
                    )
                    for state in group_clients:
                        try:
                            await state.ws.send_str(payload)
                        except (ConnectionError, OSError):
                            dead.append(id(state.ws))

            # --- Metrics broadcasts ---
            if metrics_clients:
                metrics_data = metrics.get_metrics()
                payload = json.dumps(
                    {
                        "view": "metrics",
                        "metrics": metrics_data,
                        "exchanges": exchanges,
                        "server_ts": server_ts,
                    },
                    separators=(",", ":"),
                )
                for state in metrics_clients:
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
    probe: OrderBookProbe | None = app.get("depth_probe")
    if probe:
        await probe.close()
    alert: AlertEngine | None = app.get("alert_engine")
    if alert:
        await alert.close()


async def start_web(
    manager: ExchangeManager,
    config: WebConfig | None = None,
    arb_config: ArbitrageConfig | None = None,
    alert_config: AlertConfig | None = None,
) -> web.AppRunner:
    """Create and start the aiohttp web server."""
    config = config or WebConfig()
    arb_config = arb_config or ArbitrageConfig()
    alert_config = alert_config or AlertConfig()

    registry = await build_registry()
    fees = await build_fee_registry()

    depth_probe = OrderBookProbe() if arb_config.depth_enabled else None
    metrics_collector = MetricsCollector()

    # Alert engine — zero overhead if token/chat_id not set
    alert_engine: AlertEngine | None = None
    if alert_config.enabled and alert_config.telegram_token and alert_config.telegram_chat_id:
        alert_engine = AlertEngine(
            token=alert_config.telegram_token,
            chat_id=alert_config.telegram_chat_id,
            min_profit_pct=alert_config.min_profit_pct,
            min_volume_quote=alert_config.min_volume_quote,
            cooldown_minutes=alert_config.cooldown_minutes,
            alert_mode=alert_config.alert_mode,
        )
        logger.info("Telegram alerts enabled (cooldown=%dm)", alert_config.cooldown_minutes)

    app = web.Application()
    app["manager"] = manager
    app["clients"] = {}
    app["web_config"] = config
    app["arb_config"] = arb_config
    app["coin_registry"] = registry
    app["fee_registry"] = fees
    app["depth_probe"] = depth_probe
    app["alert_engine"] = alert_engine
    app["metrics"] = metrics_collector
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_get("/", _index_handler)
    app.router.add_get("/ws", _ws_handler)
    app.router.add_get("/api/metrics", _metrics_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()

    logger.info("Web server started on http://%s:%d", config.host, config.port)
    return runner
