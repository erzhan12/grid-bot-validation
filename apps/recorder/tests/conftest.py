"""Shared test fixtures for recorder tests."""

import pytest

from grid_db import (
    BybitAccount,
    DatabaseFactory,
    DatabaseSettings,
    Strategy,
    User,
)
from grid_db.identity import account_id_for, strategy_id_for, user_id_for
from recorder.config import AccountConfig, RecorderConfig


@pytest.fixture
def db():
    """In-memory database for tests."""
    settings = DatabaseSettings(db_type="sqlite", db_name=":memory:")
    factory = DatabaseFactory(settings)
    factory.create_tables()
    return factory


@pytest.fixture
def basic_config():
    """Config with public streams only (default: trades disabled)."""
    return RecorderConfig(
        symbols=["BTCUSDT"],
        capture_public_trades=False,
        database_url="sqlite:///:memory:",
        testnet=True,
        batch_size=10,
        flush_interval=1.0,
        health_log_interval=60.0,
    )


@pytest.fixture
def config_with_trades_enabled():
    """Config with public trades capture enabled."""
    return RecorderConfig(
        symbols=["BTCUSDT"],
        capture_public_trades=True,
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
        capture_public_trades=False,
        database_url="sqlite:///:memory:",
        testnet=True,
        batch_size=10,
        flush_interval=1.0,
        health_log_interval=60.0,
        account=AccountConfig(
            name="test_account",
            strat_id="test_strat",
            api_key="test_key",
            api_secret="test_secret",
        ),
    )


@pytest.fixture
def db_with_gridbot_seed(db):
    """Same `db` instance, pre-seeded with gridbot-style parent rows.

    Shared-DB-mode recorder tests need User/BybitAccount/Strategy rows to
    already exist (with the uuid5 IDs gridbot would write) so that the
    recorder's verify-only `_seed_db_records` branch finds them and the Run
    FK insert succeeds. Account-mode tests declare both `db` and
    `db_with_gridbot_seed` in their signature — pytest evaluates this
    fixture as a side-effect on the same instance, body keeps using `db`.
    """
    with db.get_session() as session:
        session.add(
            User(user_id=user_id_for("test_account"), username="test_account")
        )
        session.add(
            BybitAccount(
                account_id=account_id_for("test_account"),
                user_id=user_id_for("test_account"),
                account_name="test_account",
                environment="testnet",
            )
        )
        session.add(
            Strategy(
                strategy_id=strategy_id_for("test_strat"),
                account_id=account_id_for("test_account"),
                strategy_type="GridStrategy",
                symbol="BTCUSDT",
                config_json={},
            )
        )
    return db
