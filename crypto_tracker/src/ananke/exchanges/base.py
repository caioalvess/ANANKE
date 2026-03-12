"""Abstract base class for exchange implementations."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from ananke.models import Ticker


class Exchange(ABC):
    """
    Interface base para todas as exchanges.

    Para adicionar uma nova exchange:
    1. Crie uma classe que herde de Exchange
    2. Implemente connect(), disconnect() e fetch_exchange_info()
    3. Registre em exchanges/__init__.py
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.tickers: dict[str, Ticker] = {}
        self._on_update: Callable[[], None] | None = None

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection and start receiving data."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the connection."""

    @abstractmethod
    async def fetch_exchange_info(self) -> None:
        """Fetch static exchange metadata (symbol info, asset pairs)."""

    def on_update(self, callback: Callable[[], None]) -> None:
        """Register a callback to be invoked when ticker data updates."""
        self._on_update = callback

    def _notify(self) -> None:
        if self._on_update:
            self._on_update()

    def get_tickers(self, quote_asset: str | None = None) -> list[Ticker]:
        """Return tickers, optionally filtered by quote asset."""
        tickers = list(self.tickers.values())
        if quote_asset:
            tickers = [t for t in tickers if t.quote_asset == quote_asset]
        return tickers
