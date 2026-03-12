"""Rich-based live terminal display for crypto tickers."""

from datetime import datetime

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ananke.models import Ticker

QUOTE_FILTERS = ["USDT", "BTC", "ETH", "BNB", "FDUSD", "USDC", "ALL"]

SORT_OPTIONS = [
    ("volume_quote", True, "Vol ($) ↓"),
    ("price_change_pct", True, "Var% ↓"),
    ("price_change_pct", False, "Var% ↑"),
    ("price", True, "Preço ↓"),
    ("price", False, "Preço ↑"),
    ("trades_count", True, "Trades ↓"),
    ("symbol", False, "A-Z"),
]

EXCHANGE_ABBREV: dict[str, str] = {
    "Binance": "BIN",
    "Bybit": "BYB",
    "OKX": "OKX",
    "Kraken": "KRK",
    "KuCoin": "KUC",
    "Gate.io": "GIO",
}


def fmt_price(value: float) -> str:
    """Format price with adaptive decimal places."""
    if value == 0:
        return "—"
    if value >= 1_000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    if value >= 0.01:
        return f"{value:.6f}"
    return f"{value:.8f}"


def fmt_volume(value: float) -> str:
    """Format volume with K/M/B suffixes."""
    if value == 0:
        return "—"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:,.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:,.2f}K"
    return f"{value:,.2f}"


def fmt_change(value: float) -> Text:
    """Format percentage change with color."""
    if value > 0:
        return Text(f"+{value:.2f}%", style="bold green")
    if value < 0:
        return Text(f"{value:.2f}%", style="bold red")
    return Text(f"{value:.2f}%", style="dim")


def fmt_int(value: int) -> str:
    if value == 0:
        return "—"
    return f"{value:,}"


def build_table(
    tickers: list[Ticker],
    quote_filter: str,
    sort_key: str,
    sort_reverse: bool,
    search: str,
    page: int,
    page_size: int,
    show_exchange: bool = True,
) -> tuple[Table, int]:
    """Build a Rich Table from ticker data. Returns (table, total_filtered)."""
    if quote_filter != "ALL":
        tickers = [t for t in tickers if t.quote_asset == quote_filter]

    if search:
        s = search.upper()
        tickers = [t for t in tickers if s in t.symbol or s in t.base_asset]

    tickers.sort(key=lambda t: getattr(t, sort_key, 0) or 0, reverse=sort_reverse)
    total = len(tickers)

    start = page * page_size
    page_tickers = tickers[start : start + page_size]

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        row_styles=["", "dim"],
        expand=True,
        padding=(0, 1),
    )

    table.add_column("#", style="dim", width=5, justify="right")
    if show_exchange:
        table.add_column("Ex", style="bold cyan", width=4)
    table.add_column("Par", style="bold white", min_width=12)
    table.add_column("Preço", justify="right", min_width=14)
    table.add_column("Var 24h", justify="right", min_width=10)
    table.add_column("Var $", justify="right", min_width=12)
    table.add_column("High 24h", justify="right", min_width=14)
    table.add_column("Low 24h", justify="right", min_width=14)
    table.add_column("Amplitude", justify="right", min_width=8)
    table.add_column("Vol (Base)", justify="right", min_width=12)
    table.add_column("Vol (Quote)", justify="right", min_width=12)
    table.add_column("Bid", justify="right", min_width=12)
    table.add_column("Ask", justify="right", min_width=12)
    table.add_column("Spread", justify="right", min_width=8)
    table.add_column("Trades", justify="right", min_width=9)

    for i, t in enumerate(page_tickers, start=start + 1):
        spread_text = Text(
            f"{t.spread:.3f}%", style="yellow" if t.spread > 0.1 else "dim"
        )
        row: list[str | Text] = [str(i)]
        if show_exchange:
            abbrev = EXCHANGE_ABBREV.get(t.exchange, t.exchange[:3].upper())
            row.append(abbrev)
        row.extend([
            f"{t.base_asset}/{t.quote_asset}",
            fmt_price(t.price),
            fmt_change(t.price_change_pct),
            fmt_price(t.price_change),
            fmt_price(t.high_24h),
            fmt_price(t.low_24h),
            Text(f"{t.amplitude:.2f}%", style="magenta"),
            fmt_volume(t.volume_base),
            fmt_volume(t.volume_quote),
            fmt_price(t.bid),
            fmt_price(t.ask),
            spread_text,
            fmt_int(t.trades_count),
        ])
        table.add_row(*row)

    return table, total


def build_header(
    exchange_names: list[str],
    exchange_filter: str,
    total_symbols: int,
    filtered: int,
    quote_filter: str,
    sort_label: str,
    search: str,
    page: int,
    total_pages: int,
) -> Panel:
    """Build the header panel with stats and controls."""
    now = datetime.now().strftime("%H:%M:%S")
    exchanges_str = " + ".join(exchange_names)

    info = Text()
    info.append(f"  {exchanges_str}", style="bold yellow")
    info.append(f"  |  {total_symbols} pares spot", style="white")
    info.append(f"  |  {filtered} exibidos", style="white")
    info.append(f"  |  {now}", style="green")
    info.append(f"  |  Pág {page + 1}/{max(total_pages, 1)}", style="cyan")

    controls = Text()
    controls.append("\n  [Q]", style="bold red")
    controls.append(" Sair  ", style="dim")
    controls.append("[E]", style="bold yellow")
    controls.append(f" Exchange: {exchange_filter}  ", style="white")
    controls.append("[F]", style="bold cyan")
    controls.append(f" Quote: {quote_filter}  ", style="white")
    controls.append("[S]", style="bold cyan")
    controls.append(f" Ordem: {sort_label}  ", style="white")
    controls.append("[/]", style="bold cyan")
    controls.append(f" Busca: {search or '—'}  ", style="white")
    controls.append("[←→]", style="bold cyan")
    controls.append(" Pág  ", style="dim")
    controls.append("[R]", style="bold cyan")
    controls.append(" Reset", style="dim")

    content = Text()
    content.append_text(info)
    content.append_text(controls)

    return Panel(
        content,
        title="[bold white] ANANKE Crypto Tracker [/]",
        border_style="bright_blue",
        padding=(0, 1),
    )


def build_layout(
    tickers: list[Ticker],
    exchange_names: list[str],
    exchange_filter: str,
    quote_filter: str,
    sort_idx: int,
    search: str,
    page: int,
    page_size: int,
) -> Layout:
    """Compose the full terminal layout."""
    sort_key, sort_reverse, sort_label = SORT_OPTIONS[sort_idx]
    show_exchange = exchange_filter == "ALL" and len(exchange_names) > 1

    table, total_filtered = build_table(
        tickers, quote_filter, sort_key, sort_reverse, search, page, page_size,
        show_exchange=show_exchange,
    )

    total_pages = max(1, (total_filtered + page_size - 1) // page_size)

    header = build_header(
        exchange_names,
        exchange_filter,
        len(tickers),
        total_filtered,
        quote_filter,
        sort_label,
        search,
        page,
        total_pages,
    )

    layout = Layout()
    layout.split_column(
        Layout(header, name="header", size=5),
        Layout(table, name="body"),
    )
    return layout
