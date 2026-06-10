"""Tests for replay config loading."""

import pytest
from decimal import Decimal
from pydantic import ValidationError
from replay.config import (
    FillSimulatorConfig,
    ReplayConfig,
    ReplayStrategyConfig,
    load_config,
)


class TestReplayStrategyConfig:
    """Tests for ReplayStrategyConfig."""

    def test_defaults(self):
        config = ReplayStrategyConfig(tick_size=Decimal("0.1"))
        assert config.grid_count == 50
        assert config.grid_step == 0.2
        assert config.amount == "x0.001"
        assert config.commission_rate == Decimal("0.0002")

    def test_tick_size_string_conversion(self):
        config = ReplayStrategyConfig(tick_size="0.01")
        assert config.tick_size == Decimal("0.01")

    def test_grid_count_minimum(self):
        with pytest.raises(ValidationError):
            ReplayStrategyConfig(tick_size=Decimal("0.1"), grid_count=2)

    def test_legacy_long_koef_rejected_with_migration_message(self):
        """Reject legacy `long_koef` so migrating users do not silently lose
        the multiplier (Pydantic ignores unknown fields by default)."""
        with pytest.raises(ValidationError, match="renamed to 'early_imbalance_multiplier'"):
            ReplayStrategyConfig(tick_size=Decimal("0.1"), long_koef=1.5)

    def test_risk_field_defaults_match_backtest(self):
        """Feature 0071 — risk-field defaults match BacktestStrategyConfig so
        existing replay YAMLs keep today's behaviour (backwards-compat)."""
        config = ReplayStrategyConfig(tick_size=Decimal("0.1"))
        assert config.min_liq_ratio == 0.8
        assert config.max_liq_ratio == 1.2
        assert config.min_total_margin == 0.15
        assert config.increase_same_position_on_low_margin is False
        assert config.leverage == 10

    @pytest.mark.parametrize("leverage", [0, 126])
    def test_leverage_bounds_rejected(self, leverage):
        """Mirrors BacktestStrategyConfig leverage bounds (ge=1, le=125)."""
        with pytest.raises(ValidationError):
            ReplayStrategyConfig(tick_size=Decimal("0.1"), leverage=leverage)


class TestReplayConfig:
    """Tests for ReplayConfig."""

    def test_defaults(self):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
        )
        assert config.database_url == "sqlite:///recorder.db"
        assert config.run_id is None
        assert config.start_ts is None
        assert config.end_ts is None
        assert config.initial_balance == Decimal("10000")
        assert config.enable_funding is True
        assert config.wind_down_mode == "leave_open"
        assert config.output_dir == "results/replay"
        assert config.price_tolerance == Decimal("0")
        assert config.qty_tolerance == Decimal("0.001")
        assert config.fill_simulator.mode == "last_cross"

    def test_initial_balance_string(self):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
            initial_balance="5000",
        )
        assert config.initial_balance == Decimal("5000")

    def test_initial_balance_int(self):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
            initial_balance=5000,
        )
        assert config.initial_balance == Decimal("5000")

    def test_fill_simulator_omitted_defaults_to_last_cross(self):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
        )

        assert config.fill_simulator == FillSimulatorConfig(mode="last_cross")

    def test_fill_simulator_empty_block_defaults_to_last_cross(self):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
            fill_simulator={},
        )

        assert config.fill_simulator.mode == "last_cross"

    @pytest.mark.parametrize(
        "mode",
        ["strict_cross", "trade_through_at_limit", "book_touch", "last_cross"],
    )
    def test_fill_simulator_explicit_modes(self, mode):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
            fill_simulator={"mode": mode},
        )

        assert config.fill_simulator.mode == mode

    def test_fill_simulator_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            ReplayConfig(
                symbol="BTCUSDT",
                strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
                fill_simulator={"mode": "invalid"},
            )


class TestLoadConfig:
    """Tests for load_config()."""

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path.yaml")

    def test_no_config_found_raises(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REPLAY_CONFIG_PATH", raising=False)
        with pytest.raises(FileNotFoundError, match="No config file found"):
            load_config()

    def test_loads_valid_yaml(self, tmp_path):
        config_file = tmp_path / "replay.yaml"
        config_file.write_text(
            "symbol: ETHUSDT\n"
            "strategy:\n"
            "  tick_size: 0.01\n"
            "  grid_count: 30\n"
        )
        config = load_config(str(config_file))
        assert config.symbol == "ETHUSDT"
        assert config.strategy.tick_size == Decimal("0.01")
        assert config.strategy.grid_count == 30

    def test_env_var_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "custom.yaml"
        config_file.write_text(
            "symbol: BTCUSDT\n"
            "strategy:\n"
            "  tick_size: 0.1\n"
        )
        monkeypatch.setenv("REPLAY_CONFIG_PATH", str(config_file))
        config = load_config()
        assert config.symbol == "BTCUSDT"

    def test_risk_fields_round_trip_to_backtest_config(self, tmp_path):
        """Feature 0071 — yaml → ReplayConfig → BacktestStrategyConfig carries
        non-default risk-mgmt values across the type boundary. This mirrors
        the engine's build; the real build site inside ReplayEngine.run() is
        covered by test_engine.py."""
        from backtest.config import BacktestStrategyConfig

        config_file = tmp_path / "replay.yaml"
        config_file.write_text(
            "symbol: LTCUSDT\n"
            "strategy:\n"
            "  tick_size: 0.01\n"
            "  min_liq_ratio: 0.7\n"
            "  max_liq_ratio: 1.3\n"
            "  min_total_margin: 3\n"
            "  increase_same_position_on_low_margin: true\n"
            "  leverage: 5\n"
        )
        config = load_config(str(config_file))

        # Mirror ReplayEngine.run()'s BacktestStrategyConfig build.
        bt_config = BacktestStrategyConfig(
            strat_id="replay_ltcusdt",
            symbol=config.symbol,
            tick_size=config.strategy.tick_size,
            grid_count=config.strategy.grid_count,
            grid_step=config.strategy.grid_step,
            amount=config.strategy.amount,
            max_margin=config.strategy.max_margin,
            early_imbalance_multiplier=config.strategy.early_imbalance_multiplier,
            commission_rate=config.strategy.commission_rate,
            enable_risk_multipliers=config.strategy.enable_risk_multipliers,
            min_liq_ratio=config.strategy.min_liq_ratio,
            max_liq_ratio=config.strategy.max_liq_ratio,
            min_total_margin=config.strategy.min_total_margin,
            increase_same_position_on_low_margin=(
                config.strategy.increase_same_position_on_low_margin
            ),
            leverage=config.strategy.leverage,
        )

        assert bt_config.min_liq_ratio == 0.7
        assert bt_config.max_liq_ratio == 1.3
        assert bt_config.min_total_margin == 3.0
        assert bt_config.increase_same_position_on_low_margin is True
        assert bt_config.leverage == 5

    def test_default_search_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("REPLAY_CONFIG_PATH", raising=False)
        conf_dir = tmp_path / "conf"
        conf_dir.mkdir()
        (conf_dir / "replay.yaml").write_text(
            "symbol: BTCUSDT\n"
            "strategy:\n"
            "  tick_size: 0.1\n"
        )
        config = load_config()
        assert config.symbol == "BTCUSDT"


class TestSeedConfigCollateral:
    """Feature 0065 — non-USDT collateral re-mark seed fields."""

    def test_collateral_defaults_empty(self):
        from datetime import timedelta
        from replay.config import SeedConfig

        cfg = SeedConfig()
        assert cfg.collateral_coins == []
        assert cfg.collateral_symbol_map == {}
        assert cfg.collateral_value_ratios == {}
        assert cfg.collateral_wallet_max_staleness == timedelta(seconds=60)

    def test_collateral_fields_populate(self):
        from replay.config import SeedConfig

        cfg = SeedConfig(
            enabled=True,
            at_ts="2026-06-01T17:42:00Z",
            account_id="9bdb9748-f9e0-5c13-b144-0ad6a8dbcaba",
            strat_id="solusdt_test",
            collateral_coins=["SOL"],
            collateral_symbol_map={"SOL": "SOLUSDT"},
            collateral_value_ratios={"SOL": "0.85"},
        )
        assert cfg.collateral_coins == ["SOL"]
        assert cfg.collateral_symbol_map == {"SOL": "SOLUSDT"}
        # value ratios coerced to Decimal.
        assert cfg.collateral_value_ratios == {"SOL": Decimal("0.85")}

    def test_collateral_wallet_max_staleness_seconds(self):
        from datetime import timedelta
        from replay.config import SeedConfig

        cfg = SeedConfig(collateral_wallet_max_staleness=120)
        assert cfg.collateral_wallet_max_staleness == timedelta(seconds=120)

    def test_collateral_coins_rejects_empty_string(self):
        from replay.config import SeedConfig

        with pytest.raises(ValidationError, match="collateral_coins"):
            SeedConfig(collateral_coins=["SOL", "  "])
