"""Exchange implementations for cryptocurrency data feeds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ananke.exchanges.base import Exchange
from ananke.exchanges.binance import BinanceExchange
from ananke.exchanges.bybit import BybitExchange
from ananke.exchanges.gateio import GateioExchange
from ananke.exchanges.kraken import KrakenExchange
from ananke.exchanges.kucoin import KucoinExchange
from ananke.exchanges.manager import ExchangeManager
from ananke.exchanges.okx import OkxExchange

if TYPE_CHECKING:
    from ananke.config import AppConfig

__all__ = [
    "Exchange",
    "BinanceExchange",
    "BybitExchange",
    "GateioExchange",
    "KrakenExchange",
    "KucoinExchange",
    "OkxExchange",
    "ExchangeManager",
    "create_exchanges",
]

_REGISTRY: dict[str, type] = {
    "binance": BinanceExchange,
    "bybit": BybitExchange,
    "okx": OkxExchange,
    "kraken": KrakenExchange,
    "kucoin": KucoinExchange,
    "gateio": GateioExchange,
}


def create_exchanges(config: AppConfig) -> list[Exchange]:
    """
    Factory: instantiate enabled exchanges from config.

    To add a new exchange:
    1. Create the Exchange subclass
    2. Add its config dataclass to AppConfig
    3. Add one entry to _REGISTRY
    """
    config_map: dict[str, object] = {
        "binance": config.binance,
        "bybit": config.bybit,
        "okx": config.okx,
        "kraken": config.kraken,
        "kucoin": config.kucoin,
        "gateio": config.gateio,
    }

    exchanges: list[Exchange] = []
    for name in config.enabled_exchanges:
        cls = _REGISTRY.get(name)
        cfg = config_map.get(name)
        if cls and cfg:
            exchanges.append(cls(cfg))  # type: ignore[call-arg]

    return exchanges
