"""Test fixtures for replay package."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from gridcore import TickerEvent, EventType
from grid_db import DatabaseFactory, DatabaseSettings

from replay.config import ReplayConfig, ReplayStrategyConfig


@pytest.fixture
def db_settings():
    """In-memory SQLite settings for testing."""
    return DatabaseSettings(
        db_type="sqlite",
        db_name=":memory:",
        echo_sql=False,
    )


@pytest.fixture
def db(db_settings):
    """Create fresh in-memory database for each test."""
    database = DatabaseFactory(db_settings)
    database.create_tables()
    yield database
    database.drop_tables()


@pytest.fixture
def ts():
    """Base timestamp for tests."""
    return datetime(2025, 2, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def sample_strategy_config():
    """Sample strategy config for replay."""
    return ReplayStrategyConfig(
        tick_size=Decimal("0.1"),
        grid_count=50,
        grid_step=0.2,
        amount="x0.001",
        commission_rate=Decimal("0.0002"),
    )


@pytest.fixture
def sample_config(sample_strategy_config, ts):
    """Sample replay config."""
    return ReplayConfig(
        database_url="sqlite:///:memory:",
        run_id="test-run-id",
        symbol="BTCUSDT",
        start_ts=ts,
        end_ts=ts + timedelta(hours=1),
        strategy=sample_strategy_config,
        initial_balance=Decimal("10000"),
        enable_funding=False,
        output_dir="results/test_replay",
    )


def make_ticker_event(
    symbol: str = "BTCUSDT",
    price: Decimal = Decimal("100000"),
    ts: datetime = None,
) -> TickerEvent:
    """Create a TickerEvent for testing."""
    if ts is None:
        ts = datetime(2025, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol=symbol,
        exchange_ts=ts,
        local_ts=ts,
        last_price=price,
        mark_price=price,
        bid1_price=price - Decimal("1"),
        ask1_price=price + Decimal("1"),
        funding_rate=Decimal("0.0001"),
    )
