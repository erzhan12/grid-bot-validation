"""Test fixtures for gridbot tests."""

import pytest
from decimal import Decimal

from gridbot.config import AccountConfig, StrategyConfig, GridbotConfig


@pytest.fixture
def sample_account_config():
    """Sample account configuration."""
    return AccountConfig(
        name="test_account",
        api_key="test_key",
        api_secret="test_secret",
        testnet=True,
    )


@pytest.fixture
def sample_strategy_config():
    """Sample strategy configuration."""
    return StrategyConfig(
        strat_id="btcusdt_test",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=50,
        grid_step=0.2,
        amount="x0.001",
        max_margin=8.0,
        shadow_mode=False,
    )


@pytest.fixture
def sample_gridbot_config(sample_account_config, sample_strategy_config):
    """Sample gridbot configuration."""
    return GridbotConfig(
        accounts=[sample_account_config],
        strategies=[sample_strategy_config],
        database_url="sqlite:///:memory:",
    )
