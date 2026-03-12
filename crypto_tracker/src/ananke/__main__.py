#!/usr/bin/env python3
"""
ANANKE Crypto Tracker — Entry point.

Usage:
    ananke              Terminal UI (Rich)
    ananke --web        Web UI (browser)

Controls (terminal mode):
    Q       Quit
    F       Cycle quote filter (USDT → BTC → ETH → BNB → FDUSD → USDC → ALL)
    S       Cycle sort mode
    E       Cycle exchange filter
    /       Toggle search mode
    ← →    Previous / Next page
    R       Reset filters
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import replace

from rich.live import Live

from ananke.config import AppConfig, WebConfig, load_config
from ananke.display import QUOTE_FILTERS, SORT_OPTIONS, build_layout
from ananke.exchanges import ExchangeManager, create_exchanges


def _build_manager(config: AppConfig) -> ExchangeManager:
    """Create ExchangeManager from config."""
    exchanges = create_exchanges(config)
    if not exchanges:
        print("  No exchanges enabled. Check ANANKE_ENABLED_EXCHANGES.")
        sys.exit(1)
    return ExchangeManager(exchanges)


async def _init_manager(manager: ExchangeManager) -> None:
    """Fetch info and connect all exchanges, reporting per-exchange status."""
    names = ", ".join(manager.exchange_names)
    print(f"\n  Connecting to: {names}\n")

    results = await manager.fetch_all_info()
    for name, err in results.items():
        if err:
            print(f"  [{name}] FAILED: {err}")
        else:
            ex = manager.get_exchange(name)
            sym_count = len(getattr(ex, "_symbol_info", {})) if ex else 0
            print(f"  [{name}] OK — {sym_count} spot symbols loaded")

    if all(err is not None for err in results.values()):
        print("\n  All exchanges failed. Check connectivity.\n")
        return

    await manager.connect_all()

    for _ in range(50):
        if manager.has_data():
            break
        await asyncio.sleep(0.1)


class App:
    """Terminal UI application controller."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.manager = _build_manager(config)
        self.quote_idx = 0
        self.sort_idx = 0
        self.exchange_idx = 0
        self.search = ""
        self.search_mode = False
        self.page = 0
        self._running = True

    @property
    def quote_filter(self) -> str:
        return QUOTE_FILTERS[self.quote_idx]

    @property
    def exchange_filters(self) -> list[str]:
        return ["ALL", *self.manager.exchange_names]

    @property
    def exchange_filter(self) -> str:
        return self.exchange_filters[self.exchange_idx]

    async def run(self) -> None:
        """Main entry point."""
        await _init_manager(self.manager)

        if not self.manager.has_data():
            print("\n  No data received.\n")
            await self.manager.disconnect_all()
            return

        await self._display_loop()

    async def _display_loop(self) -> None:
        """Rich Live display loop with keyboard input."""
        input_task = asyncio.create_task(self._input_listener())
        refresh_ms = self.config.display.refresh_ms

        try:
            with Live(
                self._render(),
                refresh_per_second=1000 // refresh_ms,
                screen=True,
                transient=True,
            ) as live:
                while self._running:
                    live.update(self._render())
                    await asyncio.sleep(refresh_ms / 1000)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            input_task.cancel()
            await self.manager.disconnect_all()

    def _render(self) -> object:
        """Build current layout."""
        if self.exchange_filter == "ALL":
            tickers = self.manager.get_all_tickers()
        else:
            tickers = self.manager.get_exchange_tickers(self.exchange_filter)

        return build_layout(
            tickers=tickers,
            exchange_names=self.manager.exchange_names,
            exchange_filter=self.exchange_filter,
            quote_filter=self.quote_filter,
            sort_idx=self.sort_idx,
            search=self.search,
            page=self.page,
            page_size=self.config.display.page_size,
        )

    async def _input_listener(self) -> None:
        """Listen for keyboard input in raw terminal mode."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()

        try:
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
            )
        except OSError:
            return

        import termios
        import tty

        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())

            while self._running:
                try:
                    char = await asyncio.wait_for(reader.read(1), timeout=0.1)
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                if not char:
                    break

                key = char.decode("utf-8", errors="ignore").lower()

                if self.search_mode:
                    if char == b"\x1b" or key == "\n":
                        self.search_mode = False
                    elif char in (b"\x7f", b"\x08"):
                        self.search = self.search[:-1]
                        self.page = 0
                    elif key.isprintable():
                        self.search += key.upper()
                        self.page = 0
                    continue

                if key == "q":
                    self._running = False
                    break
                elif key == "f":
                    self.quote_idx = (self.quote_idx + 1) % len(QUOTE_FILTERS)
                    self.page = 0
                elif key == "s":
                    self.sort_idx = (self.sort_idx + 1) % len(SORT_OPTIONS)
                    self.page = 0
                elif key == "e":
                    self.exchange_idx = (
                        (self.exchange_idx + 1) % len(self.exchange_filters)
                    )
                    self.page = 0
                elif key == "/":
                    self.search_mode = True
                    self.search = ""
                elif key == "r":
                    self.quote_idx = 0
                    self.sort_idx = 0
                    self.exchange_idx = 0
                    self.search = ""
                    self.search_mode = False
                    self.page = 0
                elif char == b"\x1b":
                    try:
                        seq = await asyncio.wait_for(reader.read(2), timeout=0.05)
                    except TimeoutError:
                        continue
                    if seq == b"[C":
                        self.page += 1
                    elif seq == b"[D":
                        self.page = max(0, self.page - 1)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            with contextlib.suppress(AttributeError, OSError):
                transport.close()


async def run_web(config: AppConfig) -> None:
    """Run the web UI server."""
    from ananke.web import start_web

    manager = _build_manager(config)
    await _init_manager(manager)

    if not manager.has_data():
        print("\n  No data received.\n")
        await manager.disconnect_all()
        return

    runner = await start_web(manager, config.web)
    print(f"\n  Web UI: http://{config.web.host}:{config.web.port}")
    print(f"  {manager.total_symbols()} spot pairs across {len(manager.exchange_names)} exchanges")
    print("  Press Ctrl+C to stop.\n")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
        await manager.disconnect_all()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="ananke",
        description="ANANKE Crypto Tracker — Real-time multi-exchange spot prices",
    )
    parser.add_argument(
        "--web", action="store_true", help="Launch web UI instead of terminal"
    )
    parser.add_argument("--host", default=None, help="Web server host")
    parser.add_argument("--port", type=int, default=None, help="Web server port")
    parser.add_argument(
        "--exchanges",
        default=None,
        help="Comma-separated exchanges to enable (e.g. binance,bybit)",
    )
    args = parser.parse_args()

    config = load_config()

    if args.host or args.port:
        config = replace(
            config,
            web=WebConfig(
                host=args.host or config.web.host,
                port=args.port or config.web.port,
            ),
        )

    if args.exchanges:
        config = replace(
            config,
            enabled_exchanges=tuple(
                e.strip() for e in args.exchanges.split(",") if e.strip()
            ),
        )

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[logging.FileHandler(config.log_file, mode="a")],
    )

    try:
        if args.web:
            asyncio.run(run_web(config))
        else:
            signal.signal(signal.SIGINT, lambda *_: None)
            asyncio.run(App(config).run())
    except KeyboardInterrupt:
        pass

    print("\n  ANANKE Crypto Tracker encerrado.\n")


if __name__ == "__main__":
    main()
