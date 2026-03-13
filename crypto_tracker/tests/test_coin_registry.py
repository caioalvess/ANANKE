"""Tests for CoinRegistry builder and dominant coin tier."""

from ananke.coin_registry import CoinRegistry, _build_mappings, _names_match


def _coins(*specs: tuple[str, str, str]) -> list[dict]:
    """Build a CoinGecko-style coins list.

    Each spec is (id, symbol, name).
    """
    return [{"id": cid, "symbol": sym, "name": name} for cid, sym, name in specs]


# ---------------------------------------------------------------------------
# CoinRegistry.resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_global_confirmed(self) -> None:
        reg = CoinRegistry({"BTC": "bitcoin"}, {}, frozenset())
        assert reg.resolve("BTC", "Binance") == "bitcoin"
        assert reg.resolve("btc", "Bybit") == "bitcoin"

    def test_kucoin_confirmed_on_kucoin(self) -> None:
        reg = CoinRegistry({}, {"LUNA": "terra-luna-2"}, frozenset({"LUNA"}))
        assert reg.resolve("LUNA", "KuCoin") == "terra-luna-2"

    def test_kucoin_confirmed_blocked_elsewhere(self) -> None:
        reg = CoinRegistry({}, {"LUNA": "terra-luna-2"}, frozenset({"LUNA"}))
        assert reg.resolve("LUNA", "Binance") is None

    def test_ambiguous_blocked(self) -> None:
        reg = CoinRegistry({}, {}, frozenset({"XYZ"}))
        assert reg.resolve("XYZ", "Binance") is None

    def test_unknown_blocked(self) -> None:
        reg = CoinRegistry({}, {}, frozenset())
        assert reg.resolve("NOPE", "Binance") is None

    def test_exchange_blocked(self) -> None:
        reg = CoinRegistry(
            {"VRA": "verasity"}, {}, frozenset(),
            exchange_blocked=frozenset({("Gate.io", "VRA")}),
        )
        assert reg.resolve("VRA", "Gate.io") is None
        assert reg.resolve("VRA", "Binance") == "verasity"

    def test_empty_registry(self) -> None:
        reg = CoinRegistry.empty()
        assert reg.has_data() is False
        assert reg.resolve("BTC", "Binance") is None


# ---------------------------------------------------------------------------
# _names_match
# ---------------------------------------------------------------------------


class TestNamesMatch:
    def test_exact(self) -> None:
        assert _names_match("Bitcoin", "Bitcoin") is True

    def test_case_insensitive(self) -> None:
        assert _names_match("Bitcoin Cash", "bitcoin cash") is True

    def test_spaces_ignored(self) -> None:
        assert _names_match("Bitcoin Cash", "BitcoinCash") is True

    def test_substring(self) -> None:
        assert _names_match("USDC", "USD Coin") is True

    def test_no_match(self) -> None:
        assert _names_match("Bitcoin", "Ethereum") is False

    def test_empty_assumes_match(self) -> None:
        assert _names_match("", "Bitcoin") is True


# ---------------------------------------------------------------------------
# _build_mappings — unique symbol tier
# ---------------------------------------------------------------------------


class TestBuildMappingsUnique:
    def test_unique_symbol(self) -> None:
        coins = _coins(("bitcoin", "btc", "Bitcoin"))
        reg = _build_mappings(coins, {}, {})
        assert reg.resolve("BTC", "Binance") == "bitcoin"

    def test_unique_counted(self) -> None:
        coins = _coins(("bitcoin", "btc", "Bitcoin"), ("ethereum", "eth", "Ethereum"))
        reg = _build_mappings(coins, {}, {})
        assert reg.global_count == 2


# ---------------------------------------------------------------------------
# _build_mappings — blue chip tier
# ---------------------------------------------------------------------------


class TestBuildMappingsBlueChip:
    def test_blue_chip_resolves(self) -> None:
        coins = _coins(
            ("matic-network", "matic", "Polygon"),
            ("matic-fake", "matic", "Matic Fake Token"),
        )
        # matic-network in top 50
        market_caps = {"matic-network": 5_000_000_000}
        reg = _build_mappings(coins, market_caps, {})
        assert reg.resolve("MATIC", "Binance") == "matic-network"

    def test_two_blue_chips_not_resolved(self) -> None:
        coins = _coins(
            ("coin-a", "x", "Coin A"),
            ("coin-b", "x", "Coin B"),
        )
        # Both are top 50 — can't disambiguate
        caps = {}
        for i, cid in enumerate(
            [f"filler-{j}" for j in range(48)] + ["coin-a", "coin-b"]
        ):
            caps[cid] = 1_000_000_000 - i * 1_000
        reg = _build_mappings(coins, caps, {})
        assert reg.resolve("X", "Binance") is None


# ---------------------------------------------------------------------------
# _build_mappings — DOMINANT COIN TIER (new)
# ---------------------------------------------------------------------------


class TestBuildMappingsDominant:
    def test_100x_ratio_resolves(self) -> None:
        """Market cap ≥100x runner-up → dominant, regardless of absolute cap."""
        coins = _coins(
            ("pepe", "pepe", "Pepe"),
            ("pepe-fake", "pepe", "Pepe Fake"),
        )
        # pepe at $500M, fake at $4M → ratio 125x > 100x
        market_caps = {"pepe": 500_000_000, "pepe-fake": 4_000_000}
        reg = _build_mappings(coins, market_caps, {})
        assert reg.resolve("PEPE", "Binance") == "pepe"
        assert reg.resolve("PEPE", "Bybit") == "pepe"

    def test_10x_above_10m_resolves(self) -> None:
        """Market cap ≥10x AND >$10M → dominant."""
        coins = _coins(
            ("arbitrum", "arb", "Arbitrum"),
            ("arb-fake", "arb", "Arb Protocol"),
        )
        # arbitrum at $2B, fake at $50M → ratio 40x > 10x, cap > $10M
        market_caps = {"arbitrum": 2_000_000_000, "arb-fake": 50_000_000}
        reg = _build_mappings(coins, market_caps, {})
        assert reg.resolve("ARB", "Binance") == "arbitrum"

    def test_10x_below_10m_stays_ambiguous(self) -> None:
        """Market cap ≥10x but <$10M → NOT dominant."""
        coins = _coins(
            ("small-coin", "sml", "Small Coin"),
            ("small-fake", "sml", "Small Fake"),
        )
        # small-coin at $5M, fake at $400K → ratio 12.5x, but cap < $10M
        market_caps = {"small-coin": 5_000_000, "small-fake": 400_000}
        reg = _build_mappings(coins, market_caps, {})
        assert reg.resolve("SML", "Binance") is None

    def test_single_entry_with_cap_above_10m(self) -> None:
        """Only one entry has market cap data in top 300, cap ≥$10M → dominant."""
        coins = _coins(
            ("bonk", "bonk", "Bonk"),
            ("bonk-fake", "bonk", "Bonk Fake"),
            ("bonk-scam", "bonk", "Bonk Scam"),
        )
        # Only bonk has cap data, others not in top 300
        market_caps = {"bonk": 800_000_000}
        reg = _build_mappings(coins, market_caps, {})
        assert reg.resolve("BONK", "Binance") == "bonk"

    def test_single_entry_below_10m_stays_ambiguous(self) -> None:
        """Only one entry with cap data but <$10M → stays ambiguous."""
        coins = _coins(
            ("tiny", "tiny", "Tiny Token"),
            ("tiny-fake", "tiny", "Tiny Fake"),
        )
        # Need 50+ filler entries so "tiny" at $5M doesn't land in top-50
        market_caps = {f"filler-{i}": 1_000_000_000 - i for i in range(50)}
        market_caps["tiny"] = 5_000_000
        reg = _build_mappings(coins, market_caps, {})
        assert reg.resolve("TINY", "Binance") is None

    def test_close_ratio_stays_ambiguous(self) -> None:
        """Ratio <10x → not dominant, stays ambiguous."""
        coins = _coins(
            ("coin-a", "dup", "Coin A"),
            ("coin-b", "dup", "Coin B"),
        )
        # 5x ratio, both above $10M — not enough dominance
        market_caps = {"coin-a": 100_000_000, "coin-b": 20_000_000}
        reg = _build_mappings(coins, market_caps, {})
        assert reg.resolve("DUP", "Binance") is None

    def test_no_cap_data_stays_ambiguous(self) -> None:
        """No entries have market cap data → ambiguous."""
        coins = _coins(
            ("obscure-a", "obs", "Obscure A"),
            ("obscure-b", "obs", "Obscure B"),
        )
        reg = _build_mappings(coins, {}, {})
        assert reg.resolve("OBS", "Binance") is None

    def test_dominant_gets_crossvalidated(self) -> None:
        """Dominant coin gets cross-validated against exchange names."""
        coins = _coins(
            ("atom", "atom", "Cosmos Hub"),
            ("atom-fake", "atom", "Atom Fake"),
        )
        market_caps = {"atom": 3_000_000_000, "atom-fake": 1_000_000}
        # KuCoin says ATOM is "Cosmos" — matches "Cosmos Hub" via substring
        kucoin_names = {"ATOM": "Cosmos"}
        reg = _build_mappings(coins, market_caps, kucoin_names)
        assert reg.resolve("ATOM", "KuCoin") == "atom"

    def test_dominant_blocked_on_name_mismatch(self) -> None:
        """Dominant coin blocked on exchange with name mismatch."""
        coins = _coins(
            ("real-token", "tok", "Real Token"),
            ("tok-fake", "tok", "Fake Token"),
        )
        market_caps = {"real-token": 500_000_000, "tok-fake": 100_000}
        # Gate.io says TOK is "Totally Other Koin" — no match with "Real Token"
        gateio_names = {"TOK": "Totally Other Koin"}
        reg = _build_mappings(coins, market_caps, {}, gateio_names)
        assert reg.resolve("TOK", "Gate.io") is None
        assert reg.resolve("TOK", "Binance") == "real-token"

    def test_dominant_falls_through_to_kucoin(self) -> None:
        """When dominant tier doesn't resolve, KuCoin tier still works."""
        coins = _coins(
            ("coin-a", "dup", "Coin A"),
            ("coin-b", "dup", "Coin B"),
        )
        # Close ratio, not dominant
        market_caps = {"coin-a": 50_000_000, "coin-b": 40_000_000}
        kucoin_names = {"DUP": "Coin A"}
        reg = _build_mappings(coins, market_caps, kucoin_names)
        # Not globally resolved, but KuCoin fullName matches
        assert reg.resolve("DUP", "KuCoin") == "coin-a"
        assert reg.resolve("DUP", "Binance") is None


# ---------------------------------------------------------------------------
# _build_mappings — KuCoin tier
# ---------------------------------------------------------------------------


class TestBuildMappingsKuCoin:
    def test_kucoin_fullname_match(self) -> None:
        coins = _coins(
            ("terra-luna-2", "luna", "Terra"),
            ("luna-fake", "luna", "Luna Fake"),
        )
        reg = _build_mappings(coins, {}, {"LUNA": "Terra"})
        assert reg.resolve("LUNA", "KuCoin") == "terra-luna-2"
        assert reg.resolve("LUNA", "Binance") is None

    def test_kucoin_no_match(self) -> None:
        coins = _coins(
            ("coin-a", "x", "Alpha"),
            ("coin-b", "x", "Beta"),
        )
        reg = _build_mappings(coins, {}, {"X": "Gamma"})
        assert reg.resolve("X", "KuCoin") is None


# ---------------------------------------------------------------------------
# _build_mappings — cross-validation
# ---------------------------------------------------------------------------


class TestBuildMappingsCrossval:
    def test_unique_symbol_crossval_pass(self) -> None:
        coins = _coins(("verasity", "vra", "Verasity"))
        reg = _build_mappings(coins, {}, {"VRA": "Verasity"})
        assert reg.resolve("VRA", "KuCoin") == "verasity"

    def test_unique_symbol_crossval_fail(self) -> None:
        coins = _coins(("verasity", "vra", "Verasity"))
        reg = _build_mappings(coins, {}, {"VRA": "Completely Different"})
        assert reg.resolve("VRA", "KuCoin") is None
        assert reg.resolve("VRA", "Binance") == "verasity"

    def test_gateio_crossval(self) -> None:
        coins = _coins(("verasity", "vra", "Verasity"))
        reg = _build_mappings(coins, {}, {}, {"VRA": "Wrong Name"})
        assert reg.resolve("VRA", "Gate.io") is None
        assert reg.resolve("VRA", "Binance") == "verasity"
