"""Shared fixtures for integration tests."""

import pytest
from decimal import Decimal

from gridcore.config import GridConfig


@pytest.fixture
def grid_config():
    """Standard GridConfig for integration tests."""
    return GridConfig(grid_step=0.2, grid_count=50)


@pytest.fixture
def btcusdt_tick_size():
    """BTCUSDT tick size."""
    return Decimal("0.1")
