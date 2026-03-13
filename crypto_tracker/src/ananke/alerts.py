"""Telegram alert engine for arbitrage opportunities.

Monitors arbitrage results each broadcast cycle and sends alerts
via Telegram Bot API when opportunities meet configured thresholds.
Cooldown per pair prevents alert spam.

Setup:
  1. Create bot via @BotFather → get token
  2. Send /start to the bot
  3. Get chat_id via https://api.telegram.org/bot{token}/getUpdates
  4. Set env vars: ANANKE_TELEGRAM_TOKEN, ANANKE_TELEGRAM_CHAT_ID
"""

import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_SEND_TIMEOUT = 5.0  # seconds

# ---------------------------------------------------------------------------
# Trade URL builder (mirrors frontend tradeUrl function)
# ---------------------------------------------------------------------------

_TRADE_URLS: dict[str, str] = {
    "Binance": "https://www.binance.com/en/trade/{B}_{Q}",
    "Bybit": "https://www.bybit.com/trade/spot/{B}/{Q}",
    "OKX": "https://www.okx.com/trade-spot/{b}-{q}",
    "Kraken": "https://pro.kraken.com/app/trade/{b}-{q}",
    "KuCoin": "https://www.kucoin.com/trade/{B}-{Q}",
    "Gate.io": "https://www.gate.io/trade/{B}_{Q}",
}


def _trade_url(exchange: str, base: str, quote: str) -> str:
    """Build exchange trading page URL."""
    template = _TRADE_URLS.get(exchange)
    if not template:
        return "#"
    return template.format(
        B=base.upper(), Q=quote.upper(),
        b=base.lower(), q=quote.lower(),
    )


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _fmt_price(v: float) -> str:
    """Format a price for display."""
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    if v >= 0.01:
        return f"{v:.6f}"
    return f"{v:.8f}"


def _fmt_vol(v: float) -> str:
    """Format volume for display."""
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.2f}K"
    return f"${v:.2f}"


def format_alert(opp: dict) -> str:
    """Format an arbitrage opportunity as a Telegram Markdown message."""
    base = opp["b"]
    quote = opp["q"]
    ask_ex = opp["ax"]
    bid_ex = opp["bx"]
    profit = opp["pf"]
    net_profit = opp["npf"]
    ask_price = opp["ak"]
    bid_price = opp["bi"]
    ask_vol = opp.get("av", 0)
    bid_vol = opp.get("bv", 0)
    wf = opp.get("wf", 0)

    buy_url = _trade_url(ask_ex, base, quote)
    sell_url = _trade_url(bid_ex, base, quote)

    wf_line = f"*Withdrawal Fee:* ~${_fmt_price(wf)}" if wf > 0 else ""

    lines = [
        "\U0001f514 *ARBITRAGEM DETECTADA*",
        "",
        f"*Par:* {base}/{quote}",
        f"*Profit:* +{profit:.2f}% (net: +{net_profit:.2f}%)",
        f"*Compra:* {ask_ex} @ {_fmt_price(ask_price)}",
        f"*Venda:* {bid_ex} @ {_fmt_price(bid_price)}",
        f"*Vol Compra:* {_fmt_vol(ask_vol)}",
        f"*Vol Venda:* {_fmt_vol(bid_vol)}",
    ]
    if wf_line:
        lines.append(wf_line)

    lines.append("")
    lines.append(
        f"[Comprar na {ask_ex}]({buy_url})"
        f" | [Vender na {bid_ex}]({sell_url})",
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AlertEngine
# ---------------------------------------------------------------------------


class AlertEngine:
    """Monitors arb results and sends Telegram alerts.

    Zero overhead when not configured (token/chat_id missing).
    Cooldown tracking per pair prevents alert spam.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        min_profit_pct: float = 0.5,
        min_volume_quote: float = 50_000.0,
        cooldown_minutes: float = 5.0,
        alert_mode: str = "transfer",
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._min_profit = min_profit_pct
        self._min_volume = min_volume_quote
        self._cooldown_sec = cooldown_minutes * 60
        self._alert_mode = alert_mode
        self._last_alert: dict[str, float] = {}  # pair_key → timestamp
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def alert_mode(self) -> str:
        return self._alert_mode

    def _profit_field(self) -> str:
        """Which profit field to evaluate based on alert_mode."""
        return "npf" if self._alert_mode == "hedge" else "tnpf"

    def _is_eligible(self, opp: dict) -> bool:
        """Check if an opportunity meets alert criteria."""
        pf_key = self._profit_field()
        profit = opp.get(pf_key, opp.get("pf", 0))
        if profit < self._min_profit:
            return False

        min_vol = min(opp.get("bv", 0), opp.get("av", 0))
        if min_vol < self._min_volume:
            return False

        # Cooldown check
        pair_key = f"{opp['b']}_{opp['q']}_{opp['ax']}_{opp['bx']}"
        now = time.monotonic()
        last = self._last_alert.get(pair_key, 0)
        return not now - last < self._cooldown_sec

    def _mark_alerted(self, opp: dict) -> None:
        """Record that an alert was sent for this pair."""
        pair_key = f"{opp['b']}_{opp['q']}_{opp['ax']}_{opp['bx']}"
        self._last_alert[pair_key] = time.monotonic()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=_SEND_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _send_telegram(self, text: str) -> bool:
        """Send a message via Telegram Bot API. Returns True on success."""
        url = _TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                logger.warning(
                    "Telegram API returned %d: %s", resp.status, body[:200],
                )
                return False
        except Exception:
            logger.debug("Telegram send failed", exc_info=True)
            return False

    async def check_and_alert(
        self, arb_results: list[dict],
    ) -> int:
        """Check arb results and send alerts for eligible opportunities.

        Returns the number of alerts sent.
        """
        if not self.enabled or not arb_results:
            return 0

        sent = 0
        for opp in arb_results:
            if not self._is_eligible(opp):
                continue

            text = format_alert(opp)
            success = await self._send_telegram(text)
            if success:
                self._mark_alerted(opp)
                sent += 1

        return sent

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
