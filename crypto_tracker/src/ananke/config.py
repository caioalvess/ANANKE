"""Centralized configuration for ANANKE Crypto Tracker."""

from dataclasses import dataclass, field
from os import environ


@dataclass(frozen=True)
class BinanceConfig:
    """Binance exchange connection settings."""

    rest_url: str = "https://api.binance.com"
    ws_url: str = "wss://stream.binance.com:9443/ws/!ticker@arr"
    rest_timeout_sec: int = 15
    ws_ping_interval: int = 20
    ws_ping_timeout: int = 10
    ws_close_timeout: int = 5
    ws_reconnect_delay: int = 3


@dataclass(frozen=True)
class BybitConfig:
    """Bybit exchange connection settings (REST polling)."""

    rest_url: str = "https://api.bybit.com"
    poll_interval_sec: float = 2.0
    rest_timeout_sec: int = 15


@dataclass(frozen=True)
class OkxConfig:
    """OKX exchange connection settings (REST polling)."""

    rest_url: str = "https://www.okx.com"
    poll_interval_sec: float = 2.0
    rest_timeout_sec: int = 15


@dataclass(frozen=True)
class KrakenConfig:
    """Kraken exchange connection settings (REST polling)."""

    rest_url: str = "https://api.kraken.com"
    poll_interval_sec: float = 2.0
    rest_timeout_sec: int = 15


@dataclass(frozen=True)
class KucoinConfig:
    """KuCoin exchange connection settings (REST polling)."""

    rest_url: str = "https://api.kucoin.com"
    poll_interval_sec: float = 2.0
    rest_timeout_sec: int = 15


@dataclass(frozen=True)
class GateioConfig:
    """Gate.io exchange connection settings (REST polling)."""

    rest_url: str = "https://api.gateio.ws"
    poll_interval_sec: float = 2.0
    rest_timeout_sec: int = 15


@dataclass(frozen=True)
class WebConfig:
    """Web server settings."""

    host: str = "0.0.0.0"
    port: int = 8080
    broadcast_interval: float = 1.0
    ws_heartbeat: int = 30


@dataclass(frozen=True)
class DisplayConfig:
    """Terminal display settings."""

    page_size: int = 40
    refresh_ms: int = 500


@dataclass(frozen=True)
class AppConfig:
    """Root configuration, assembled from environment variables and defaults."""

    binance: BinanceConfig = field(default_factory=BinanceConfig)
    bybit: BybitConfig = field(default_factory=BybitConfig)
    okx: OkxConfig = field(default_factory=OkxConfig)
    kraken: KrakenConfig = field(default_factory=KrakenConfig)
    kucoin: KucoinConfig = field(default_factory=KucoinConfig)
    gateio: GateioConfig = field(default_factory=GateioConfig)
    enabled_exchanges: tuple[str, ...] = ("binance", "bybit", "okx", "kraken", "kucoin", "gateio")
    web: WebConfig = field(default_factory=WebConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    log_level: str = "WARNING"
    log_file: str = "ananke.log"


def _env(key: str, default: str) -> str:
    """Read an environment variable with a default."""
    return environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    """Read an integer environment variable, falling back to default on error."""
    raw = environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    """Read a float environment variable, falling back to default on error."""
    raw = environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_config() -> AppConfig:
    """Build AppConfig from environment variables with sensible defaults."""
    bin_d = BinanceConfig()
    byb_d = BybitConfig()
    okx_d = OkxConfig()
    krk_d = KrakenConfig()
    kuc_d = KucoinConfig()
    gio_d = GateioConfig()
    web_d = WebConfig()
    disp_d = DisplayConfig()

    enabled_raw = _env(
        "ANANKE_ENABLED_EXCHANGES",
        ",".join(AppConfig.enabled_exchanges),
    )

    return AppConfig(
        binance=BinanceConfig(
            rest_url=_env("ANANKE_BINANCE_REST_URL", bin_d.rest_url),
            ws_url=_env("ANANKE_BINANCE_WS_URL", bin_d.ws_url),
            rest_timeout_sec=_env_int("ANANKE_BINANCE_REST_TIMEOUT", bin_d.rest_timeout_sec),
        ),
        bybit=BybitConfig(
            rest_url=_env("ANANKE_BYBIT_REST_URL", byb_d.rest_url),
            poll_interval_sec=_env_float("ANANKE_BYBIT_POLL_INTERVAL", byb_d.poll_interval_sec),
            rest_timeout_sec=_env_int("ANANKE_BYBIT_REST_TIMEOUT", byb_d.rest_timeout_sec),
        ),
        okx=OkxConfig(
            rest_url=_env("ANANKE_OKX_REST_URL", okx_d.rest_url),
            poll_interval_sec=_env_float("ANANKE_OKX_POLL_INTERVAL", okx_d.poll_interval_sec),
            rest_timeout_sec=_env_int("ANANKE_OKX_REST_TIMEOUT", okx_d.rest_timeout_sec),
        ),
        kraken=KrakenConfig(
            rest_url=_env("ANANKE_KRAKEN_REST_URL", krk_d.rest_url),
            poll_interval_sec=_env_float("ANANKE_KRAKEN_POLL_INTERVAL", krk_d.poll_interval_sec),
            rest_timeout_sec=_env_int("ANANKE_KRAKEN_REST_TIMEOUT", krk_d.rest_timeout_sec),
        ),
        kucoin=KucoinConfig(
            rest_url=_env("ANANKE_KUCOIN_REST_URL", kuc_d.rest_url),
            poll_interval_sec=_env_float("ANANKE_KUCOIN_POLL_INTERVAL", kuc_d.poll_interval_sec),
            rest_timeout_sec=_env_int("ANANKE_KUCOIN_REST_TIMEOUT", kuc_d.rest_timeout_sec),
        ),
        gateio=GateioConfig(
            rest_url=_env("ANANKE_GATEIO_REST_URL", gio_d.rest_url),
            poll_interval_sec=_env_float("ANANKE_GATEIO_POLL_INTERVAL", gio_d.poll_interval_sec),
            rest_timeout_sec=_env_int("ANANKE_GATEIO_REST_TIMEOUT", gio_d.rest_timeout_sec),
        ),
        enabled_exchanges=tuple(
            name.strip() for name in enabled_raw.split(",") if name.strip()
        ),
        web=WebConfig(
            host=_env("ANANKE_WEB_HOST", web_d.host),
            port=_env_int("ANANKE_WEB_PORT", web_d.port),
            broadcast_interval=_env_float("ANANKE_BROADCAST_INTERVAL", web_d.broadcast_interval),
        ),
        display=DisplayConfig(
            page_size=_env_int("ANANKE_PAGE_SIZE", disp_d.page_size),
            refresh_ms=_env_int("ANANKE_REFRESH_MS", disp_d.refresh_ms),
        ),
        log_level=_env("ANANKE_LOG_LEVEL", AppConfig.log_level),
        log_file=_env("ANANKE_LOG_FILE", AppConfig.log_file),
    )
