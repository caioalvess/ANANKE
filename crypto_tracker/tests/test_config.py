"""Tests for ananke.config module."""

import pytest

from ananke.config import (
    AppConfig,
    ArbitrageConfig,
    BinanceConfig,
    BybitConfig,
    KrakenConfig,
    KucoinConfig,
    WebConfig,
    _env_float,
    _env_int,
    load_config,
)


def test_default_config() -> None:
    cfg = AppConfig()
    assert cfg.binance.rest_timeout_sec == 15
    assert cfg.bybit.poll_interval_sec == 2.0
    assert cfg.web.port == 8080
    assert cfg.display.page_size == 40
    assert cfg.log_level == "WARNING"
    assert "binance" in cfg.enabled_exchanges
    assert "bybit" in cfg.enabled_exchanges


def test_load_config_defaults() -> None:
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert isinstance(cfg.binance, BinanceConfig)
    assert isinstance(cfg.bybit, BybitConfig)
    assert isinstance(cfg.web, WebConfig)
    assert isinstance(cfg.kraken, KrakenConfig)
    assert isinstance(cfg.kucoin, KucoinConfig)


def test_config_immutable() -> None:
    cfg = AppConfig()
    with pytest.raises(AttributeError):
        cfg.log_level = "DEBUG"  # type: ignore[misc]


def test_enabled_exchanges_default() -> None:
    cfg = AppConfig()
    assert cfg.enabled_exchanges == ("binance", "bybit", "okx", "kraken", "kucoin", "gateio")


def test_env_int_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INT", "42")
    assert _env_int("TEST_INT", 0) == 42


def test_env_int_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INT", "not_a_number")
    assert _env_int("TEST_INT", 99) == 99


def test_env_int_missing() -> None:
    assert _env_int("NONEXISTENT_VAR_XYZ", 7) == 7


def test_env_float_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FLOAT", "3.14")
    assert _env_float("TEST_FLOAT", 0.0) == 3.14


def test_env_float_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FLOAT", "abc")
    assert _env_float("TEST_FLOAT", 1.5) == 1.5


def test_arbitrage_config_defaults() -> None:
    cfg = ArbitrageConfig()
    assert cfg.min_volume_quote == 10_000.0
    assert cfg.max_pair_spread_pct == 5.0
    assert cfg.min_profit_pct == 0.0


def test_arbitrage_config_in_app_config() -> None:
    cfg = AppConfig()
    assert isinstance(cfg.arbitrage, ArbitrageConfig)
    assert cfg.arbitrage.min_volume_quote == 10_000.0


def test_arbitrage_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANANKE_ARB_MIN_VOLUME", "25000")
    monkeypatch.setenv("ANANKE_ARB_MAX_SPREAD", "3.5")
    monkeypatch.setenv("ANANKE_ARB_MIN_PROFIT", "0.5")
    monkeypatch.setenv("ANANKE_ARB_REF_TRADE_SIZE", "5000")
    cfg = load_config()
    assert cfg.arbitrage.min_volume_quote == 25000.0
    assert cfg.arbitrage.max_pair_spread_pct == 3.5
    assert cfg.arbitrage.min_profit_pct == 0.5
    assert cfg.arbitrage.ref_trade_size == 5000.0


def test_arbitrage_config_ref_trade_size_default() -> None:
    cfg = ArbitrageConfig()
    assert cfg.ref_trade_size == 1000.0
