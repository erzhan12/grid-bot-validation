"""Test fixtures for live_check package."""

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from grid_db import DatabaseFactory, DatabaseSettings
from grid_db.models import BybitAccount, Run, Strategy, User

from live_check.config import LiveCheckConfig, StratCheckConfig

RUN_ID = "test-run-id"


@pytest.fixture
def ts():
    """Base timestamp: naive UTC, safely AFTER the 0080 cutoff."""
    return datetime(2026, 7, 1, 12, 0, 0)


@pytest.fixture
def db():
    """Create fresh in-memory database for each test."""
    database = DatabaseFactory(
        DatabaseSettings(db_type="sqlite", db_name=":memory:", echo_sql=False)
    )
    database.create_tables()
    yield database
    database.drop_tables()


@pytest.fixture
def seeded_run_account(db, ts):
    """Insert User → BybitAccount → Strategy → recording Run for RUN_ID.

    Returns a namespace with the account_id string so callers can reach the
    UUID without holding an ORM session.
    """
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
            symbol="LTCUSDT",
            config_json={},
        )
        session.add(strategy)
        session.flush()
        run = Run(
            run_id=RUN_ID,
            user_id=user.user_id,
            account_id=account_id,
            strategy_id=strategy.strategy_id,
            run_type="recording",
            start_ts=ts - timedelta(days=1),
        )
        session.add(run)
        session.commit()
    return SimpleNamespace(account_id=account_id, run_id=RUN_ID)


@pytest.fixture
def strat():
    """One strat mirroring the live LTC geometry."""
    return StratCheckConfig(
        strat_id="ltcusdt_test",
        symbol="LTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.4,
        amount="x0.0005",
        min_total_margin=3.0,
        max_margin=5.0,
    )


@pytest.fixture
def live_check_config(strat):
    """Minimal LiveCheckConfig with the single test strat."""
    return LiveCheckConfig(strats=[strat], run_id=RUN_ID)
