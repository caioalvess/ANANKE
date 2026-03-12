"""Orchestrates multiple exchange connections."""

import asyncio
import contextlib
import logging

from ananke.exchanges.base import Exchange
from ananke.models import Ticker

logger = logging.getLogger(__name__)


class ExchangeManager:
    """
    Aggregates multiple Exchange instances.

    Provides concurrent connect/disconnect and a unified ticker view
    keyed by '{exchange}:{symbol}' for cross-exchange analysis.
    """

    def __init__(self, exchanges: list[Exchange]) -> None:
        self.exchanges: dict[str, Exchange] = {ex.name: ex for ex in exchanges}

    @property
    def exchange_names(self) -> list[str]:
        return list(self.exchanges.keys())

    async def fetch_all_info(self) -> dict[str, Exception | None]:
        """Fetch exchange info concurrently. Returns {name: error_or_None}."""
        results: dict[str, Exception | None] = {}

        async def _fetch(name: str, ex: Exchange) -> None:
            try:
                await ex.fetch_exchange_info()
                results[name] = None
                logger.info("%s: exchange info loaded (%d symbols)", name, len(ex.tickers))
            except Exception as e:
                results[name] = e
                logger.error("%s: failed to fetch exchange info: %s", name, e)

        await asyncio.gather(
            *(_fetch(name, ex) for name, ex in self.exchanges.items())
        )
        return results

    async def connect_all(self) -> None:
        """Connect all exchanges concurrently."""
        await asyncio.gather(
            *(ex.connect() for ex in self.exchanges.values())
        )

    async def disconnect_all(self) -> None:
        """Disconnect all exchanges, suppressing individual failures."""
        for name, ex in self.exchanges.items():
            with contextlib.suppress(Exception):
                await ex.disconnect()
                logger.info("%s: disconnected", name)

    def get_all_tickers(self) -> list[Ticker]:
        """Aggregate tickers from all exchanges into a flat list."""
        result: list[Ticker] = []
        for ex in self.exchanges.values():
            result.extend(ex.tickers.values())
        return result

    def get_exchange_tickers(self, name: str) -> list[Ticker]:
        """Get tickers from a specific exchange."""
        ex = self.exchanges.get(name)
        if not ex:
            return []
        return list(ex.tickers.values())

    def has_data(self) -> bool:
        """True if at least one exchange has received ticker data."""
        return any(ex.tickers for ex in self.exchanges.values())

    def total_symbols(self) -> int:
        """Total number of symbols across all exchanges."""
        return sum(len(ex.tickers) for ex in self.exchanges.values())

    def get_exchange(self, name: str) -> Exchange | None:
        return self.exchanges.get(name)
