"""In-memory metrics collector for arbitrage opportunity tracking.

Ring buffer of the last 60 minutes of arb snapshots (1-second granularity).
Calculates frequency, duration, average spread, and exchange rankings
from the buffered history.  No database, no persistence — restarts wipe data.
"""

import time
from collections import deque
from dataclasses import dataclass, field

_BUFFER_SIZE = 3600  # 60 min × 60 slots/min
_WINDOW_5M = 300     # 5 minutes in seconds


@dataclass(frozen=True, slots=True)
class _OppSnapshot:
    """Minimal representation of an active opportunity at a point in time."""

    key: str      # "BTC_USDT_Binance_Bybit" (base_quote_askEx_bidEx)
    profit: float


@dataclass(slots=True)
class _BufferEntry:
    """One second's worth of active opportunities."""

    ts: float                          # monotonic timestamp
    opps: frozenset[_OppSnapshot] = field(default_factory=frozenset)


def _opp_key(opp: dict) -> str:
    """Build a unique key for an arb opportunity dict."""
    return f"{opp['b']}_{opp['q']}_{opp['ax']}_{opp['bx']}"


def _opp_label(key: str) -> dict:
    """Parse key back into display components."""
    parts = key.split("_", 3)
    if len(parts) == 4:
        return {"b": parts[0], "q": parts[1], "ax": parts[2], "bx": parts[3]}
    return {"b": key, "q": "", "ax": "", "bx": ""}


class MetricsCollector:
    """Collects arb opportunity snapshots and computes aggregate metrics.

    Call `record(arb_results)` once per broadcast cycle.
    Call `get_metrics()` to retrieve computed metrics.
    Call `get_pair_metrics(key)` for freq/duration of a specific pair.
    """

    def __init__(self, buffer_size: int = _BUFFER_SIZE) -> None:
        self._buffer: deque[_BufferEntry] = deque(maxlen=buffer_size)
        self._active_since: dict[str, float] = {}  # key → first-seen monotonic
        self._peak_count: int = 0
        self._prev_count: int = 0

    def record(self, arb_results: list[dict]) -> None:
        """Record a snapshot of currently active opportunities.

        Should be called once per broadcast cycle (~1s).
        O(n) over the number of active opportunities.
        """
        now = time.monotonic()

        current_keys: set[str] = set()
        snapshots: list[_OppSnapshot] = []

        for opp in arb_results:
            key = _opp_key(opp)
            current_keys.add(key)
            snapshots.append(_OppSnapshot(key=key, profit=opp.get("pf", 0)))

            # Track when opportunity first appeared
            if key not in self._active_since:
                self._active_since[key] = now

        # Remove keys that are no longer active
        gone = set(self._active_since) - current_keys
        for k in gone:
            del self._active_since[k]

        self._prev_count = len(self._buffer[-1].opps) if self._buffer else 0
        self._buffer.append(_BufferEntry(ts=now, opps=frozenset(snapshots)))
        self._peak_count = max(self._peak_count, len(snapshots))

    def _window_entries(self, window_sec: float = _WINDOW_5M) -> list[_BufferEntry]:
        """Get buffer entries within the last `window_sec` seconds."""
        if not self._buffer:
            return []
        now = self._buffer[-1].ts
        cutoff = now - window_sec
        # deque is ordered by time, walk from end
        result: list[_BufferEntry] = []
        for entry in reversed(self._buffer):
            if entry.ts < cutoff:
                break
            result.append(entry)
        result.reverse()
        return result

    def get_pair_stats(self, window_sec: float = _WINDOW_5M) -> dict[str, dict]:
        """Compute per-pair statistics over the time window.

        Returns {key: {occurrences, spread_avg, spread_max, first_seen, last_seen}}.
        """
        entries = self._window_entries(window_sec)
        if not entries:
            return {}

        stats: dict[str, dict] = {}

        for entry in entries:
            for snap in entry.opps:
                s = stats.get(snap.key)
                if s is None:
                    s = {
                        "count": 0,
                        "profit_sum": 0.0,
                        "profit_max": 0.0,
                        "first_ts": entry.ts,
                        "last_ts": entry.ts,
                    }
                    stats[snap.key] = s
                s["count"] += 1
                s["profit_sum"] += snap.profit
                s["profit_max"] = max(s["profit_max"], snap.profit)
                s["last_ts"] = entry.ts

        # Compute averages
        for s in stats.values():
            s["profit_avg"] = s["profit_sum"] / s["count"] if s["count"] > 0 else 0.0

        return stats

    def get_pair_freq(self, key: str, window_sec: float = _WINDOW_5M) -> int:
        """How many snapshots included this pair in the window."""
        entries = self._window_entries(window_sec)
        count = 0
        for entry in entries:
            for snap in entry.opps:
                if snap.key == key:
                    count += 1
                    break
        return count

    def get_active_duration(self, key: str) -> float:
        """How long the pair has been continuously active (seconds)."""
        since = self._active_since.get(key)
        if since is None:
            return 0.0
        return time.monotonic() - since

    def get_history(self, window_sec: float = _WINDOW_5M, resolution: float = 5.0) -> list[dict]:
        """Downsampled time series of {t, count, avg_spread} for charting.

        t is seconds ago (0 = now, negative = past).
        resolution: seconds per data point (default 5s -> 60 points for 5min).
        """
        entries = self._window_entries(window_sec)
        if not entries:
            return []

        now = entries[-1].ts
        result = []

        # Group entries into buckets of `resolution` seconds
        bucket_start = entries[0].ts
        bucket_counts: list[int] = []
        bucket_spreads: list[float] = []

        for entry in entries:
            if entry.ts - bucket_start >= resolution and bucket_counts:
                avg_count = sum(bucket_counts) / len(bucket_counts)
                avg_spread = sum(bucket_spreads) / len(bucket_spreads) if bucket_spreads else 0
                result.append({
                    "t": round(bucket_start - now, 1),
                    "c": round(avg_count, 1),
                    "s": round(avg_spread, 3),
                })
                bucket_start = entry.ts
                bucket_counts = []
                bucket_spreads = []

            bucket_counts.append(len(entry.opps))
            spreads = [snap.profit for snap in entry.opps]
            bucket_spreads.append(sum(spreads) / len(spreads) if spreads else 0)

        # Last bucket
        if bucket_counts:
            avg_count = sum(bucket_counts) / len(bucket_counts)
            avg_spread = sum(bucket_spreads) / len(bucket_spreads) if bucket_spreads else 0
            result.append({
                "t": round(bucket_start - now, 1),
                "c": round(avg_count, 1),
                "s": round(avg_spread, 3),
            })

        return result

    def get_spread_distribution(self) -> list[dict]:
        """Histogram of current active opportunity spreads.

        Buckets: 0-0.5%, 0.5-1%, 1-2%, 2-5%, 5%+
        Returns [{label, min, max, count}, ...]
        """
        buckets = [
            {"label": "0-0.5%", "min": 0, "max": 0.5, "count": 0},
            {"label": "0.5-1%", "min": 0.5, "max": 1, "count": 0},
            {"label": "1-2%", "min": 1, "max": 2, "count": 0},
            {"label": "2-5%", "min": 2, "max": 5, "count": 0},
            {"label": "5%+", "min": 5, "max": 999999, "count": 0},
        ]

        if not self._buffer:
            return buckets

        for snap in self._buffer[-1].opps:
            for b in buckets:
                if b["min"] <= snap.profit < b["max"]:
                    b["count"] += 1
                    break

        return buckets

    def get_metrics(self, window_sec: float = _WINDOW_5M) -> dict:
        """Compute global + top-pair metrics.

        Returns:
            {
                "global": {total_now, total_5m, avg_spread, top_exchanges},
                "pairs": [top 20 pairs by frequency],
                "window_sec": window_sec,
                "buffer_sec": total seconds of data in buffer,
            }
        """
        pair_stats = self.get_pair_stats(window_sec)

        # Current active count
        total_now = 0
        active_spreads: list[float] = []
        if self._buffer:
            latest = self._buffer[-1]
            total_now = len(latest.opps)
            for snap in latest.opps:
                active_spreads.append(snap.profit)

        avg_spread = (
            sum(active_spreads) / len(active_spreads)
            if active_spreads else 0.0
        )

        # Peak count over window
        peak_count = 0
        for entry in self._window_entries(window_sec):
            peak_count = max(peak_count, len(entry.opps))

        trend = total_now - self._prev_count

        # Exchange frequency ranking
        ex_counts: dict[str, int] = {}
        for key, s in pair_stats.items():
            label = _opp_label(key)
            for ex in (label["ax"], label["bx"]):
                if ex:
                    ex_counts[ex] = ex_counts.get(ex, 0) + s["count"]
        top_exchanges = sorted(ex_counts.items(), key=lambda x: x[1], reverse=True)

        # Top pairs by occurrence count
        top_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["count"], reverse=True)[:50]

        pairs_out = []
        for key, s in top_pairs:
            label = _opp_label(key)
            dur = self.get_active_duration(key)
            pairs_out.append({
                "key": key,
                "b": label["b"],
                "q": label["q"],
                "ax": label["ax"],
                "bx": label["bx"],
                "freq": s["count"],
                "spread_avg": round(s["profit_avg"], 4),
                "spread_max": round(s["profit_max"], 4),
                "dur": round(dur, 1),
                "active": key in self._active_since,
            })

        buffer_sec = 0.0
        if len(self._buffer) >= 2:
            buffer_sec = self._buffer[-1].ts - self._buffer[0].ts

        return {
            "global": {
                "total_now": total_now,
                "total_5m": len(pair_stats),
                "avg_spread": round(avg_spread, 4),
                "best_spread": round(max(active_spreads) if active_spreads else 0, 4),
                "peak_5m": peak_count,
                "trend": trend,
                "top_exchanges": [
                    {"ex": ex, "count": c} for ex, c in top_exchanges[:10]
                ],
            },
            "pairs": pairs_out,
            "window_sec": window_sec,
            "buffer_sec": round(buffer_sec, 1),
            "history": self.get_history(window_sec),
            "spread_dist": self.get_spread_distribution(),
        }

    def enrich_arb_results(self, results: list[dict]) -> None:
        """Add freq and dur fields to arb results in-place.

        - freq: number of snapshots this pair appeared in last 5 min
        - dur: seconds the pair has been continuously active
        """
        for r in results:
            key = _opp_key(r)
            r["freq"] = self.get_pair_freq(key)
            r["dur"] = round(self.get_active_duration(key), 1)
