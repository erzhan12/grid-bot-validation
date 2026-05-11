"""Test fixtures for replay package."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from gridcore import TickerEvent, EventType
from grid_db import DatabaseFactory, DatabaseSettings
from grid_db.models import BybitAccount, Run, Strategy, User

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
def seeded_run_account(db, ts):
    """Insert User → BybitAccount → Strategy → Run for the standard test-run-id.

    Required by engine tests after feature 0034 — `_resolve_run` and the
    0034 backtest writer require a real `Run.account_id`. Returns a simple
    namespace with the account_id string so callers can reach the UUID
    without holding an ORM session.
    """
    from types import SimpleNamespace

    with db.get_session() as session:
        user = User(username="testuser", email="t@example.com")
        session.add(user)
        session.flush()
        account = BybitAccount(
            user_id=user.user_id,
            account_name="test_account",
            environment="testnet",
        )
        session.add(account)
        session.flush()
        account_id = str(account.account_id)
        strategy = Strategy(
            account_id=account.account_id,
            strategy_type="GridStrategy",
            symbol="BTCUSDT",
            config_json={},
        )
        session.add(strategy)
        session.flush()
        run = Run(
            run_id="test-run-id",
            user_id=user.user_id,
            account_id=account_id,
            strategy_id=strategy.strategy_id,
            run_type="live",
            start_ts=ts,
        )
        session.add(run)
        session.commit()
    return SimpleNamespace(account_id=account_id)


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
