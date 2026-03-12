"""Data models for cryptocurrency ticker information."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Ticker:
    """Represents a single cryptocurrency ticker with market data."""

    symbol: str
    base_asset: str
    quote_asset: str
    price: float = 0.0
    price_change: float = 0.0
    price_change_pct: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    volume_base: float = 0.0
    volume_quote: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    open_price: float = 0.0
    trades_count: int = 0
    last_update: datetime = field(default_factory=datetime.now)
    exchange: str = ""

    @property
    def spread(self) -> float:
        """Bid-ask spread as percentage."""
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        return ((self.ask - self.bid) / self.bid) * 100

    @property
    def amplitude(self) -> float:
        """24h price amplitude as percentage."""
        if self.low_24h <= 0:
            return 0.0
        return ((self.high_24h - self.low_24h) / self.low_24h) * 100
