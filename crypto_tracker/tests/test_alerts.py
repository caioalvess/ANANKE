"""Tests for Telegram alert engine."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ananke.alerts import AlertEngine, _trade_url, format_alert
from ananke.config import AlertConfig


def _opp(
    base: str = "BTC",
    quote: str = "USDT",
    profit: float = 1.5,
    net_profit: float = 1.2,
    tnpf: float = 1.1,
    ask_ex: str = "Bybit",
    bid_ex: str = "Binance",
    ask_price: float = 99_500,
    bid_price: float = 100_700,
    ask_vol: float = 2_300_000,
    bid_vol: float = 4_100_000,
    wf: float = 5.20,
) -> dict:
    """Build a test arb opportunity dict."""
    return {
        "s": f"{base}{quote}",
        "b": base,
        "q": quote,
        "pf": profit,
        "npf": net_profit,
        "tnpf": tnpf,
        "ax": ask_ex,
        "bx": bid_ex,
        "ak": ask_price,
        "bi": bid_price,
        "av": ask_vol,
        "bv": bid_vol,
        "wf": wf,
        "rc": wf,
    }


# ---------------------------------------------------------------------------
# Trade URL
# ---------------------------------------------------------------------------


class TestTradeUrl:
    def test_binance(self) -> None:
        assert _trade_url("Binance", "BTC", "USDT") == "https://www.binance.com/en/trade/BTC_USDT"

    def test_okx(self) -> None:
        assert _trade_url("OKX", "ETH", "USDT") == "https://www.okx.com/trade-spot/eth-usdt"

    def test_unknown_exchange(self) -> None:
        assert _trade_url("Unknown", "BTC", "USD") == "#"


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


class TestFormatAlert:
    def test_contains_key_info(self) -> None:
        msg = format_alert(_opp())
        assert "ARBITRAGEM DETECTADA" in msg
        assert "BTC/USDT" in msg
        assert "Bybit" in msg
        assert "Binance" in msg
        assert "+1.50%" in msg
        assert "+1.20%" in msg

    def test_contains_trade_links(self) -> None:
        msg = format_alert(_opp())
        assert "Comprar na Bybit" in msg
        assert "Vender na Binance" in msg
        assert "https://" in msg

    def test_withdrawal_fee_shown(self) -> None:
        msg = format_alert(_opp(wf=5.20))
        assert "Withdrawal Fee" in msg

    def test_no_withdrawal_fee_when_zero(self) -> None:
        msg = format_alert(_opp(wf=0))
        assert "Withdrawal Fee" not in msg

    def test_volume_formatting(self) -> None:
        msg = format_alert(_opp(ask_vol=2_300_000, bid_vol=4_100_000))
        assert "$2.30M" in msg
        assert "$4.10M" in msg


# ---------------------------------------------------------------------------
# AlertEngine
# ---------------------------------------------------------------------------


class TestAlertEngine:
    def test_enabled_when_configured(self) -> None:
        engine = AlertEngine("token123", "chat456")
        assert engine.enabled is True

    def test_disabled_when_no_token(self) -> None:
        engine = AlertEngine("", "chat456")
        assert engine.enabled is False

    def test_disabled_when_no_chat_id(self) -> None:
        engine = AlertEngine("token123", "")
        assert engine.enabled is False

    @pytest.mark.asyncio
    async def test_check_and_alert_sends_on_eligible(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=1.0,
            min_volume_quote=10_000,
        )
        engine._send_telegram = AsyncMock(return_value=True)

        opp = _opp(profit=2.0, tnpf=1.5, ask_vol=100_000, bid_vol=200_000)
        sent = await engine.check_and_alert([opp])

        assert sent == 1
        engine._send_telegram.assert_called_once()
        msg = engine._send_telegram.call_args[0][0]
        assert "BTC/USDT" in msg

    @pytest.mark.asyncio
    async def test_below_min_profit_not_alerted(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=2.0,
            min_volume_quote=10_000,
        )
        engine._send_telegram = AsyncMock(return_value=True)

        opp = _opp(profit=1.0, tnpf=0.8)
        sent = await engine.check_and_alert([opp])

        assert sent == 0
        engine._send_telegram.assert_not_called()

    @pytest.mark.asyncio
    async def test_below_min_volume_not_alerted(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=0.5,
            min_volume_quote=500_000,
        )
        engine._send_telegram = AsyncMock(return_value=True)

        opp = _opp(tnpf=1.0, ask_vol=100_000, bid_vol=200_000)
        sent = await engine.check_and_alert([opp])

        assert sent == 0

    @pytest.mark.asyncio
    async def test_cooldown_prevents_repeat(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=0.5,
            min_volume_quote=10_000,
            cooldown_minutes=5.0,
        )
        engine._send_telegram = AsyncMock(return_value=True)

        opp = _opp(tnpf=1.0)
        sent1 = await engine.check_and_alert([opp])
        assert sent1 == 1

        # Second call — same pair, within cooldown
        sent2 = await engine.check_and_alert([opp])
        assert sent2 == 0
        assert engine._send_telegram.call_count == 1

    @pytest.mark.asyncio
    async def test_cooldown_expires(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=0.5,
            min_volume_quote=10_000,
            cooldown_minutes=0.01,  # ~0.6s cooldown
        )
        engine._send_telegram = AsyncMock(return_value=True)

        opp = _opp(tnpf=1.0)
        await engine.check_and_alert([opp])

        # Manually expire the cooldown
        pair_key = f"{opp['b']}_{opp['q']}_{opp['ax']}_{opp['bx']}"
        engine._last_alert[pair_key] = time.monotonic() - 60

        sent = await engine.check_and_alert([opp])
        assert sent == 1
        assert engine._send_telegram.call_count == 2

    @pytest.mark.asyncio
    async def test_different_pairs_independent_cooldown(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=0.5,
            min_volume_quote=10_000,
        )
        engine._send_telegram = AsyncMock(return_value=True)

        opp1 = _opp(base="BTC", tnpf=1.0)
        opp2 = _opp(base="ETH", tnpf=1.0)

        sent = await engine.check_and_alert([opp1, opp2])
        assert sent == 2

    @pytest.mark.asyncio
    async def test_telegram_failure_does_not_mark_cooldown(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=0.5,
            min_volume_quote=10_000,
        )
        engine._send_telegram = AsyncMock(return_value=False)

        opp = _opp(tnpf=1.0)
        sent = await engine.check_and_alert([opp])
        assert sent == 0

        # Should retry since send failed (no cooldown marked)
        engine._send_telegram = AsyncMock(return_value=True)
        sent = await engine.check_and_alert([opp])
        assert sent == 1

    @pytest.mark.asyncio
    async def test_disabled_engine_noop(self) -> None:
        engine = AlertEngine("", "")
        sent = await engine.check_and_alert([_opp()])
        assert sent == 0

    @pytest.mark.asyncio
    async def test_empty_results_noop(self) -> None:
        engine = AlertEngine("token", "chat")
        engine._send_telegram = AsyncMock()
        sent = await engine.check_and_alert([])
        assert sent == 0
        engine._send_telegram.assert_not_called()

    @pytest.mark.asyncio
    async def test_hedge_mode_uses_npf(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=1.0,
            min_volume_quote=10_000,
            alert_mode="hedge",
        )
        engine._send_telegram = AsyncMock(return_value=True)

        # npf=1.5 (above threshold), tnpf=0.5 (below threshold)
        opp = _opp(tnpf=0.5, net_profit=1.5)
        sent = await engine.check_and_alert([opp])
        assert sent == 1

    @pytest.mark.asyncio
    async def test_transfer_mode_uses_tnpf(self) -> None:
        engine = AlertEngine(
            "token", "chat",
            min_profit_pct=1.0,
            min_volume_quote=10_000,
            alert_mode="transfer",
        )
        engine._send_telegram = AsyncMock(return_value=True)

        # tnpf=0.5 (below threshold), npf=1.5 (above but irrelevant)
        opp = _opp(tnpf=0.5, net_profit=1.5)
        sent = await engine.check_and_alert([opp])
        assert sent == 0

    @pytest.mark.asyncio
    async def test_send_telegram_mock_http(self) -> None:
        """Test _send_telegram with mocked aiohttp session."""
        engine = AlertEngine("fake_token", "12345")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post.return_value = mock_resp

        engine._session = mock_session

        result = await engine._send_telegram("test message")
        assert result is True
        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert "fake_token" in call_kwargs[0][0]
        assert call_kwargs[1]["json"]["chat_id"] == "12345"
        assert call_kwargs[1]["json"]["text"] == "test message"

    @pytest.mark.asyncio
    async def test_send_telegram_http_error(self) -> None:
        engine = AlertEngine("fake_token", "12345")

        mock_resp = AsyncMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(return_value="Bad Request")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post.return_value = mock_resp

        engine._session = mock_session

        result = await engine._send_telegram("test")
        assert result is False

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        engine = AlertEngine("token", "chat")
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        engine._session = mock_session

        await engine.close()
        mock_session.close.assert_called_once()


# ---------------------------------------------------------------------------
# AlertConfig
# ---------------------------------------------------------------------------


class TestAlertConfig:
    def test_defaults(self) -> None:
        cfg = AlertConfig()
        assert cfg.enabled is False
        assert cfg.telegram_token == ""
        assert cfg.telegram_chat_id == ""
        assert cfg.min_profit_pct == 0.5
        assert cfg.min_volume_quote == 50_000.0
        assert cfg.cooldown_minutes == 5.0
        assert cfg.alert_mode == "transfer"

    def test_from_env(self) -> None:
        env = {
            "ANANKE_ALERT_ENABLED": "true",
            "ANANKE_TELEGRAM_TOKEN": "123:ABC",
            "ANANKE_TELEGRAM_CHAT_ID": "-100123",
            "ANANKE_ALERT_MIN_PROFIT": "1.0",
            "ANANKE_ALERT_MIN_VOLUME": "100000",
            "ANANKE_ALERT_COOLDOWN_MIN": "10",
            "ANANKE_ALERT_MODE": "hedge",
        }
        with patch.dict("os.environ", env, clear=False):
            from ananke.config import load_config
            cfg = load_config()
            assert cfg.alert.enabled is True
            assert cfg.alert.telegram_token == "123:ABC"
            assert cfg.alert.telegram_chat_id == "-100123"
            assert cfg.alert.min_profit_pct == 1.0
            assert cfg.alert.min_volume_quote == 100_000.0
            assert cfg.alert.cooldown_minutes == 10.0
            assert cfg.alert.alert_mode == "hedge"

    def test_disabled_by_default_env(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            from ananke.config import load_config
            cfg = load_config()
            assert cfg.alert.enabled is False
