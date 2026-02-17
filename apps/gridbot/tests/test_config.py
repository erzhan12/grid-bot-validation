"""Tests for gridbot configuration module."""

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from gridbot.config import (
    AccountConfig,
    StrategyConfig,
    GridbotConfig,
    load_config,
)


class TestAccountConfig:
    """Tests for AccountConfig model."""

    def test_basic_account(self):
        """Test basic account creation."""
        account = AccountConfig(
            name="test",
            api_key="key123",
            api_secret="secret456",
        )
        assert account.name == "test"
        assert account.api_key == "key123"
        assert account.api_secret == "secret456"
        assert account.testnet is True  # default

    def test_mainnet_account(self):
        """Test account with testnet=False."""
        account = AccountConfig(
            name="prod",
            api_key="key",
            api_secret="secret",
            testnet=False,
        )
        assert account.testnet is False


class TestStrategyConfig:
    """Tests for StrategyConfig model."""

    def test_basic_strategy(self):
        """Test basic strategy creation."""
        strategy = StrategyConfig(
            strat_id="btc_main",
            account="main",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        assert strategy.strat_id == "btc_main"
        assert strategy.account == "main"
        assert strategy.symbol == "BTCUSDT"
        assert strategy.tick_size == Decimal("0.1")
        assert strategy.grid_count == 50  # default
        assert strategy.grid_step == 0.2  # default
        assert strategy.shadow_mode is False  # default

    def test_tick_size_from_string(self):
        """Test tick_size parsed from string."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="BTCUSDT",
            tick_size="0.01",
        )
        assert strategy.tick_size == Decimal("0.01")

    def test_custom_grid_params(self):
        """Test strategy with custom grid parameters."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="ETHUSDT",
            tick_size=Decimal("0.01"),
            grid_count=100,
            grid_step=0.5,
        )
        assert strategy.grid_count == 100
        assert strategy.grid_step == 0.5

    def test_shadow_mode(self):
        """Test shadow mode configuration."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            shadow_mode=True,
        )
        assert strategy.shadow_mode is True

    def test_invalid_grid_count(self):
        """Test validation rejects grid_count < 4."""
        with pytest.raises(ValueError):
            StrategyConfig(
                strat_id="test",
                account="test",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                grid_count=2,
            )

    def test_invalid_grid_step(self):
        """Test validation rejects grid_step <= 0."""
        with pytest.raises(ValueError):
            StrategyConfig(
                strat_id="test",
                account="test",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                grid_step=0,
            )


class TestGridbotConfig:
    """Tests for GridbotConfig model."""

    def test_basic_config(self, sample_account_config, sample_strategy_config):
        """Test basic config creation."""
        config = GridbotConfig(
            accounts=[sample_account_config],
            strategies=[sample_strategy_config],
        )
        assert len(config.accounts) == 1
        assert len(config.strategies) == 1

    def test_account_reference_validation(self):
        """Test validation catches invalid account references."""
        account = AccountConfig(
            name="real_account",
            api_key="key",
            api_secret="secret",
        )
        strategy = StrategyConfig(
            strat_id="test",
            account="nonexistent_account",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        with pytest.raises(ValueError, match="unknown account"):
            GridbotConfig(
                accounts=[account],
                strategies=[strategy],
            )

    def test_get_account(self, sample_gridbot_config):
        """Test get_account helper."""
        account = sample_gridbot_config.get_account("test_account")
        assert account is not None
        assert account.name == "test_account"

        missing = sample_gridbot_config.get_account("nonexistent")
        assert missing is None

    def test_get_strategies_for_account(self, sample_gridbot_config):
        """Test get_strategies_for_account helper."""
        strategies = sample_gridbot_config.get_strategies_for_account("test_account")
        assert len(strategies) == 1
        assert strategies[0].strat_id == "btcusdt_test"

        empty = sample_gridbot_config.get_strategies_for_account("other")
        assert len(empty) == 0

    def test_multiple_strategies_per_account(self):
        """Test multiple strategies for same account."""
        account = AccountConfig(
            name="multi",
            api_key="key",
            api_secret="secret",
        )
        strategies = [
            StrategyConfig(
                strat_id="btc",
                account="multi",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
            ),
            StrategyConfig(
                strat_id="eth",
                account="multi",
                symbol="ETHUSDT",
                tick_size=Decimal("0.01"),
            ),
        ]
        config = GridbotConfig(accounts=[account], strategies=strategies)
        assert len(config.get_strategies_for_account("multi")) == 2

    @pytest.mark.parametrize("field", ["wallet_cache_interval", "order_sync_interval"])
    def test_negative_interval_rejected(self, field):
        """Negative interval values are rejected by validator."""
        with pytest.raises(ValueError, match="must be >= 0"):
            GridbotConfig(**{field: -1.0})

    @pytest.mark.parametrize("field", ["wallet_cache_interval", "order_sync_interval"])
    def test_zero_interval_accepted(self, field):
        """Zero is valid (disables the feature)."""
        config = GridbotConfig(**{field: 0.0})
        assert getattr(config, field) == 0.0


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_from_yaml(self):
        """Test loading config from YAML file."""
        config_data = {
            "accounts": [
                {
                    "name": "test",
                    "api_key": "key",
                    "api_secret": "secret",
                    "testnet": True,
                }
            ],
            "strategies": [
                {
                    "strat_id": "btc_test",
                    "account": "test",
                    "symbol": "BTCUSDT",
                    "tick_size": "0.1",
                    "grid_count": 50,
                    "grid_step": 0.2,
                }
            ],
            "database_url": "sqlite:///test.db",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(config_path)
            assert len(config.accounts) == 1
            assert config.accounts[0].name == "test"
            assert len(config.strategies) == 1
            assert config.strategies[0].tick_size == Decimal("0.1")
            assert config.database_url == "sqlite:///test.db"
        finally:
            Path(config_path).unlink()

    def test_load_missing_file(self):
        """Test error when config file not found."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_load_invalid_yaml(self):
        """Test error when YAML is invalid."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("accounts:\n  - name: test\n    # missing required fields")
            config_path = f.name

        try:
            with pytest.raises(ValueError):
                load_config(config_path)
        finally:
            Path(config_path).unlink()
