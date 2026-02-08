"""Tests for backtest config."""

from decimal import Decimal
from pathlib import Path
import tempfile

import pytest
import yaml
from pydantic import ValidationError

from backtest.config import (
    BacktestConfig,
    BacktestStrategyConfig,
    WindDownMode,
    load_config,
)


class TestBacktestStrategyConfig:
    """Tests for BacktestStrategyConfig."""

    def test_minimal_config(self):
        """Config with only required fields."""
        config = BacktestStrategyConfig(
            strat_id="test",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )

        assert config.strat_id == "test"
        assert config.grid_count == 50  # Default
        assert config.grid_step == 0.2  # Default

    def test_tick_size_from_string(self):
        """tick_size can be provided as string."""
        config = BacktestStrategyConfig(
            strat_id="test",
            symbol="BTCUSDT",
            tick_size="0.01",  # String
        )

        assert config.tick_size == Decimal("0.01")

    def test_commission_rate_from_string(self):
        """commission_rate can be provided as string."""
        config = BacktestStrategyConfig(
            strat_id="test",
            symbol="BTCUSDT",
            tick_size="0.1",
            commission_rate="0.0001",
        )

        assert config.commission_rate == Decimal("0.0001")


class TestBacktestConfig:
    """Tests for BacktestConfig."""

    def test_default_values(self):
        """Config has sensible defaults."""
        config = BacktestConfig()

        assert config.initial_balance == Decimal("10000")
        assert config.enable_funding is True
        assert config.wind_down_mode == WindDownMode.LEAVE_OPEN

    def test_initial_balance_from_int(self):
        """initial_balance can be provided as int."""
        config = BacktestConfig(initial_balance=5000)

        assert config.initial_balance == Decimal("5000")

    def test_wind_down_mode_validation(self):
        """Invalid wind_down_mode raises error."""
        with pytest.raises(ValidationError, match="wind_down_mode"):
            BacktestConfig(wind_down_mode="invalid")

    def test_get_strategy(self):
        """Get strategy by ID."""
        strategy = BacktestStrategyConfig(
            strat_id="test",
            symbol="BTCUSDT",
            tick_size="0.1",
        )
        config = BacktestConfig(strategies=[strategy])

        found = config.get_strategy("test")
        assert found == strategy

        not_found = config.get_strategy("nonexistent")
        assert not_found is None

    def test_get_strategies_for_symbol(self):
        """Get strategies for a symbol."""
        s1 = BacktestStrategyConfig(strat_id="btc1", symbol="BTCUSDT", tick_size="0.1")
        s2 = BacktestStrategyConfig(strat_id="btc2", symbol="BTCUSDT", tick_size="0.1")
        s3 = BacktestStrategyConfig(strat_id="eth1", symbol="ETHUSDT", tick_size="0.01")

        config = BacktestConfig(strategies=[s1, s2, s3])

        btc_strategies = config.get_strategies_for_symbol("BTCUSDT")
        assert len(btc_strategies) == 2

        eth_strategies = config.get_strategies_for_symbol("ETHUSDT")
        assert len(eth_strategies) == 1

        non_strategies = config.get_strategies_for_symbol("NONUSDT")
        assert len(non_strategies) == 0


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_from_yaml(self):
        """Load config from YAML file."""
        config_data = {
            "strategies": [
                {
                    "strat_id": "test",
                    "symbol": "BTCUSDT",
                    "tick_size": "0.1",
                    "grid_count": 30,
                }
            ],
            "initial_balance": 5000,
            "enable_funding": False,
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(config_path)

            assert len(config.strategies) == 1
            assert config.strategies[0].strat_id == "test"
            assert config.strategies[0].grid_count == 30
            assert config.initial_balance == Decimal("5000")
            assert config.enable_funding is False
        finally:
            Path(config_path).unlink()

    def test_load_missing_file_raises(self):
        """Missing config file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path.yaml")
