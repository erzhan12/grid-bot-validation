"""Shared fixtures for integration tests."""

import sys
from pathlib import Path

import pytest
from decimal import Decimal

# Ensure tests/integration is on sys.path so ``import integration_helpers``
# works regardless of how pytest is invoked (e.g. per-app test runs that
# don't inherit the root pyproject.toml pythonpath setting).
_INTEGRATION_DIR = str(Path(__file__).resolve().parent)
if _INTEGRATION_DIR not in sys.path:
    sys.path.insert(0, _INTEGRATION_DIR)

from gridcore.config import GridConfig


@pytest.fixture
def grid_config():
    """Standard GridConfig for integration tests."""
    return GridConfig(grid_step=0.2, grid_count=50)


@pytest.fixture
def btcusdt_tick_size():
    """BTCUSDT tick size."""
    return Decimal("0.1")
