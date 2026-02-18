"""Shared test fixtures for recorder tests."""

import pytest

from recorder.config import RecorderConfig, AccountConfig


@pytest.fixture
def basic_config():
    """Config with public streams only."""
    return RecorderConfig(
        symbols=["BTCUSDT"],
        database_url="sqlite:///:memory:",
        testnet=True,
        batch_size=10,
        flush_interval=1.0,
        health_log_interval=60.0,
    )


@pytest.fixture
def config_with_account():
    """Config with public + private streams."""
    return RecorderConfig(
        symbols=["BTCUSDT"],
        database_url="sqlite:///:memory:",
        testnet=True,
        batch_size=10,
        flush_interval=1.0,
        health_log_interval=60.0,
        account=AccountConfig(
            api_key="test_key",
            api_secret="test_secret",
        ),
    )
