"""Test fixtures for live_check package."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from grid_db import DatabaseFactory, DatabaseSettings
from grid_db.models import (
    BybitAccount,
    PositionSnapshot,
    PrivateStreamSession,
    Run,
    Strategy,
    User,
)

from live_check.config import LiveCheckConfig, StratCheckConfig

RUN_ID = "test-run-id"
# Checkpoint far enough ahead to cover windows computed from the real clock.
_FAR_FUTURE = datetime(2099, 1, 1)


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
def private_coverage(db, seeded_run_account, ts):
    """A private-stream session covering every test window (0110 B2b)."""
    with db.get_session() as session:
        session.add(PrivateStreamSession(
            run_id=RUN_ID,
            account_id=seeded_run_account.account_id,
            connected_at=ts - timedelta(days=1),
            last_checkpoint_ts=_FAR_FUTURE,
        ))


@pytest.fixture
def fit_anchors(db, seeded_run_account):
    """Factory: fit end-of-window anchors — both legs flat, received at
    ``at`` with Bybit updatedTime ``at`` (0110 B2c). Place ``at`` after the
    test's executions and before its window end."""

    def _add(at):
        updated = str(int(at.replace(tzinfo=UTC).timestamp() * 1000))
        with db.get_session() as session:
            for side in ("Buy", "Sell"):
                session.add(PositionSnapshot(
                    run_id=RUN_ID,
                    account_id=seeded_run_account.account_id,
                    symbol="LTCUSDT",
                    exchange_ts=at,
                    local_ts=at,
                    side=side,
                    size=Decimal("0"),
                    entry_price=Decimal("0"),
                    source="live",
                    raw_json={"updatedTime": updated},
                ))

    return _add


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
