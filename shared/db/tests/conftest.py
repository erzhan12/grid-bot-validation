"""Test fixtures for database tests."""

import pytest
from datetime import datetime, UTC

from grid_db.database import DatabaseFactory
from grid_db.settings import DatabaseSettings
from grid_db.models import User, BybitAccount, ApiCredential, Strategy, Run


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
    """Create fresh database for each test."""
    database = DatabaseFactory(db_settings)
    database.create_tables()
    yield database
    database.drop_tables()


@pytest.fixture
def session(db):
    """Provide a session for each test.

    Note: Uses manual session management to handle tests that expect errors.
    Tests that raise IntegrityError should call session.rollback() after.
    """
    session = db.session_factory()
    try:
        yield session
    finally:
        session.rollback()  # Always rollback at end to clean up any pending state
        session.close()


@pytest.fixture
def sample_user(session):
    """Create a sample user for testing."""
    user = User(username="testuser", email="test@example.com")
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def sample_account(session, sample_user):
    """Create a sample Bybit account for testing."""
    account = BybitAccount(
        user_id=sample_user.user_id,
        account_name="test_account",
        environment="testnet",
    )
    session.add(account)
    session.flush()
    return account


@pytest.fixture
def sample_credential(session, sample_account):
    """Create a sample API credential for testing."""
    credential = ApiCredential(
        account_id=sample_account.account_id,
        api_key_id="test_api_key",
        api_secret="test_api_secret",
    )
    session.add(credential)
    session.flush()
    return credential


@pytest.fixture
def sample_strategy(session, sample_account):
    """Create a sample strategy for testing."""
    strategy = Strategy(
        account_id=sample_account.account_id,
        strategy_type="GridStrategy",
        symbol="BTCUSDT",
        config_json={"grid_count": 50, "grid_step": 0.2},
    )
    session.add(strategy)
    session.flush()
    return strategy


@pytest.fixture
def sample_run(session, sample_user, sample_account, sample_strategy):
    """Create a sample run for testing."""
    run = Run(
        user_id=sample_user.user_id,
        account_id=sample_account.account_id,
        strategy_id=sample_strategy.strategy_id,
        run_type="live",
        status="running",
        start_ts=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    return run
