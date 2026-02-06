"""Test fixtures for backtest package."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gridcore import TickerEvent, EventType

from backtest.config import BacktestConfig, BacktestStrategyConfig
from backtest.fill_simulator import TradeThroughFillSimulator
from backtest.order_manager import BacktestOrderManager, SimulatedOrder
from backtest.position_tracker import BacktestPositionTracker
from backtest.session import BacktestSession
from backtest.executor import BacktestExecutor


@pytest.fixture
def sample_strategy_config():
    """Sample strategy configuration."""
    return BacktestStrategyConfig(
        strat_id="test_btc",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=50,
        grid_step=0.2,
        amount="x0.001",
        max_margin=8.0,
        commission_rate=Decimal("0.0002"),
    )


@pytest.fixture
def sample_config(sample_strategy_config):
    """Sample backtest configuration."""
    return BacktestConfig(
        strategies=[sample_strategy_config],
        database_url="sqlite:///:memory:",
        initial_balance=Decimal("10000"),
        enable_funding=True,
        funding_rate=Decimal("0.0001"),
        wind_down_mode="leave_open",
    )


@pytest.fixture
def fill_simulator():
    """Fill simulator instance."""
    return TradeThroughFillSimulator()


@pytest.fixture
def order_manager(fill_simulator):
    """Order manager instance."""
    return BacktestOrderManager(
        fill_simulator=fill_simulator,
        commission_rate=Decimal("0.0002"),
    )


@pytest.fixture
def long_position_tracker():
    """Long position tracker instance."""
    return BacktestPositionTracker(
        direction="long",
        commission_rate=Decimal("0.0002"),
    )


@pytest.fixture
def short_position_tracker():
    """Short position tracker instance."""
    return BacktestPositionTracker(
        direction="short",
        commission_rate=Decimal("0.0002"),
    )


@pytest.fixture
def session():
    """Backtest session instance."""
    return BacktestSession(
        session_id="test_session",
        initial_balance=Decimal("10000"),
    )


@pytest.fixture
def executor(order_manager):
    """Backtest executor instance."""
    return BacktestExecutor(
        order_manager=order_manager,
        qty_calculator=None,  # Use intent.qty directly
    )


@pytest.fixture
def sample_timestamp():
    """Sample timestamp for tests."""
    return datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def sample_ticker_event(sample_timestamp):
    """Sample ticker event."""
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        exchange_ts=sample_timestamp,
        local_ts=sample_timestamp,
        last_price=Decimal("100000"),
        mark_price=Decimal("100000"),
        bid1_price=Decimal("99999"),
        ask1_price=Decimal("100001"),
        funding_rate=Decimal("0.0001"),
    )
