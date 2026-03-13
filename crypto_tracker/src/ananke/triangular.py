"""Triangular arbitrage detection via Bellman-Ford negative cycle search.

Triangular arbitrage exploits pricing inconsistencies WITHIN a single
exchange.  Example cycle: USDT → BTC → ETH → USDT.

Graph construction:
  - Each currency = node
  - Each trading pair = 2 directed edges (buy and sell)
  - Edge weight A→B: -log(rate_A_to_B * (1 - taker_fee))
    so a negative-weight cycle = profitable loop

Bellman-Ford runs from hub currencies (USDT, BTC, ETH, USDC) and
detects negative cycles (up to 4 legs max).
"""

import logging
import math
from dataclasses import dataclass

from ananke.models import Ticker

logger = logging.getLogger(__name__)

# Hub currencies: high connectivity → most likely to form triangles.
_HUB_CURRENCIES = ("USDT", "BTC", "ETH", "USDC")

_MAX_LEGS = 4  # cycles longer than this are impractical (fees eat profit)

_INF = float("inf")


@dataclass(frozen=True)
class TriangularOpportunity:
    """A detected triangular arbitrage cycle."""

    exchange: str
    path: list[str]           # e.g. ["USDT", "BTC", "ETH", "USDT"]
    profit_pct: float         # net of fees
    legs: list[dict]          # per-leg details: {from, to, pair, rate, side}
    min_volume_quote: float   # bottleneck liquidity in quote


@dataclass
class _Edge:
    """Directed edge in the currency graph."""

    src: int       # node index
    dst: int       # node index
    weight: float  # -log(rate * (1 - fee))
    rate: float    # raw conversion rate (after fee)
    pair: str      # original trading pair symbol
    side: str      # "buy" or "sell"
    volume_quote: float  # 24h volume in quote currency


def build_graph(
    tickers: list[Ticker],
    taker_fee: float,
) -> tuple[list[str], list[_Edge]]:
    """Build directed graph from tickers for a single exchange.

    Each ticker (e.g. BTC/USDT with bid/ask) produces two edges:
      - USDT → BTC: rate = 1/ask (buying BTC with USDT)
      - BTC → USDT: rate = bid  (selling BTC for USDT)

    Rates include taker fee: rate_net = rate * (1 - taker_fee).

    Returns (nodes, edges) where nodes is a list of currency names
    and edges have src/dst as indices into nodes.
    """
    node_idx: dict[str, int] = {}
    nodes: list[str] = []
    edges: list[_Edge] = []

    fee_mult = 1.0 - taker_fee

    for t in tickers:
        if t.bid <= 0 or t.ask <= 0:
            continue

        base = t.base_asset
        quote = t.quote_asset

        # Assign node indices
        for currency in (base, quote):
            if currency not in node_idx:
                node_idx[currency] = len(nodes)
                nodes.append(currency)

        bi = node_idx[base]
        qi = node_idx[quote]

        # Edge: quote → base (buy base with quote at ask price)
        buy_rate = (1.0 / t.ask) * fee_mult
        if buy_rate > 0:
            edges.append(_Edge(
                src=qi, dst=bi,
                weight=-math.log(buy_rate),
                rate=buy_rate,
                pair=t.symbol,
                side="buy",
                volume_quote=t.volume_quote,
            ))

        # Edge: base → quote (sell base for quote at bid price)
        sell_rate = t.bid * fee_mult
        if sell_rate > 0:
            edges.append(_Edge(
                src=bi, dst=qi,
                weight=-math.log(sell_rate),
                rate=sell_rate,
                pair=t.symbol,
                side="sell",
                volume_quote=t.volume_quote,
            ))

    return nodes, edges


def _find_negative_cycles(
    num_nodes: int,
    edges: list[_Edge],
    source: int,
) -> list[list[int]]:
    """Run Bellman-Ford from source and extract negative cycles.

    Returns list of cycles (each cycle = list of node indices).
    Only returns cycles that include the source node and have
    at most _MAX_LEGS edges.
    """
    dist = [_INF] * num_nodes
    pred = [-1] * num_nodes
    pred_edge: list[int] = [-1] * num_nodes
    dist[source] = 0.0

    # V-1 relaxations
    n = num_nodes
    for _ in range(n - 1):
        updated = False
        for ei, e in enumerate(edges):
            if dist[e.src] < _INF and dist[e.src] + e.weight < dist[e.dst] - 1e-12:
                dist[e.dst] = dist[e.src] + e.weight
                pred[e.dst] = e.src
                pred_edge[e.dst] = ei
                updated = True
        if not updated:
            break

    # Check for negative cycles
    cycles: list[list[int]] = []
    visited_in_cycle: set[int] = set()

    for e in edges:
        if dist[e.src] < _INF and dist[e.src] + e.weight < dist[e.dst] - 1e-12:
            # Found a node in a negative cycle — trace it
            node = e.dst
            if node in visited_in_cycle:
                continue

            # Walk back n steps to ensure we're in the cycle
            v = node
            for _ in range(n):
                v = pred[v]

            # Extract cycle starting from v
            cycle = []
            u = v
            while True:
                cycle.append(u)
                visited_in_cycle.add(u)
                u = pred[u]
                if u == v:
                    break
                if len(cycle) > _MAX_LEGS:
                    break

            if u != v or len(cycle) > _MAX_LEGS:
                continue

            cycle.append(v)  # close the cycle
            cycle.reverse()

            # Only keep cycles containing source and with 3-4 legs
            if source in cycle[:-1] and 3 <= len(cycle) - 1 <= _MAX_LEGS:
                cycles.append(cycle)

    return cycles


def detect_triangular(
    tickers: list[Ticker],
    exchange: str,
    taker_fee: float,
) -> list[TriangularOpportunity]:
    """Detect triangular arbitrage opportunities for a single exchange.

    Args:
        tickers: all tickers from one exchange
        exchange: exchange name
        taker_fee: taker fee rate (e.g. 0.001 for 0.1%)

    Returns:
        List of TriangularOpportunity sorted by profit descending.
    """
    if not tickers:
        return []

    nodes, edges = build_graph(tickers, taker_fee)
    if not edges:
        return []

    # Build edge lookup for cycle → leg detail mapping
    edge_map: dict[tuple[int, int], list[_Edge]] = {}
    for e in edges:
        edge_map.setdefault((e.src, e.dst), []).append(e)

    seen_paths: set[tuple[str, ...]] = set()
    opportunities: list[TriangularOpportunity] = []

    # Run from hub currencies only
    for hub in _HUB_CURRENCIES:
        if hub not in [n for n in nodes]:
            continue
        source = nodes.index(hub)
        cycles = _find_negative_cycles(len(nodes), edges, source)

        for cycle in cycles:
            # Build path as currency names
            path = [nodes[i] for i in cycle]
            # Deduplicate: normalize cycle to canonical form
            inner = tuple(path[:-1])
            # Rotate to smallest element first for dedup
            min_idx = inner.index(min(inner))
            canonical = inner[min_idx:] + inner[:min_idx]
            if canonical in seen_paths:
                continue
            seen_paths.add(canonical)

            # Calculate actual profit by multiplying rates along the cycle
            product = 1.0
            legs: list[dict] = []
            min_vol = _INF

            valid = True
            for j in range(len(cycle) - 1):
                src_i, dst_i = cycle[j], cycle[j + 1]
                candidates = edge_map.get((src_i, dst_i))
                if not candidates:
                    valid = False
                    break
                # Use the best rate edge
                best = max(candidates, key=lambda e: e.rate)
                product *= best.rate
                min_vol = min(min_vol, best.volume_quote)
                legs.append({
                    "from": nodes[src_i],
                    "to": nodes[dst_i],
                    "pair": best.pair,
                    "rate": best.rate,
                    "side": best.side,
                })

            if not valid:
                continue

            profit_pct = (product - 1.0) * 100
            if profit_pct <= 0:
                continue

            opportunities.append(TriangularOpportunity(
                exchange=exchange,
                path=path,
                profit_pct=round(profit_pct, 4),
                legs=legs,
                min_volume_quote=round(min_vol, 2),
            ))

    opportunities.sort(key=lambda o: o.profit_pct, reverse=True)
    return opportunities


def compute_triangular_all(
    all_tickers: list[Ticker],
    taker_fees: dict[str, float] | None = None,
    exchange_filter: str = "",
) -> list[dict]:
    """Compute triangular arbitrage across all exchanges (or filtered).

    Groups tickers by exchange, runs detection per exchange,
    returns serialized results for the frontend.

    Args:
        all_tickers: tickers from all exchanges
        taker_fees: {exchange_name: fee_rate} map (uses 0.001 default)
        exchange_filter: if set, only compute for this exchange

    Returns:
        List of dicts ready for JSON serialization, sorted by profit desc.
    """
    default_fee = 0.001

    # Group tickers by exchange
    by_exchange: dict[str, list[Ticker]] = {}
    for t in all_tickers:
        if exchange_filter and t.exchange != exchange_filter:
            continue
        by_exchange.setdefault(t.exchange, []).append(t)

    results: list[dict] = []

    for exchange, tickers in by_exchange.items():
        fee = (taker_fees or {}).get(exchange, default_fee)
        opps = detect_triangular(tickers, exchange, fee)

        for opp in opps:
            path_str = " → ".join(opp.path)
            legs_info = []
            for leg in opp.legs:
                legs_info.append({
                    "f": leg["from"],
                    "t": leg["to"],
                    "p": leg["pair"],
                    "r": round(leg["rate"] / (1.0 - fee), 8),  # raw rate w/o fee
                    "sd": leg["side"],
                })

            results.append({
                "ex": exchange,
                "path": path_str,
                "pf": opp.profit_pct,
                "legs": legs_info,
                "nlegs": len(opp.legs),
                "mvol": opp.min_volume_quote,
            })

    results.sort(key=lambda r: r["pf"], reverse=True)
    return results
