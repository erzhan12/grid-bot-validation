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
        assert config.fill_simulator.mode == "book_touch"

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

    def test_fill_simulator_omitted_defaults_to_book_touch(self):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
        )

        assert config.fill_simulator == FillSimulatorConfig(mode="book_touch")

    def test_fill_simulator_empty_block_defaults_to_book_touch(self):
        config = ReplayConfig(
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
            fill_simulator={},
        )

        assert config.fill_simulator.mode == "book_touch"

    @pytest.mark.parametrize(
        "mode",
        ["strict_cross", "trade_through_at_limit", "book_touch"],
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
