"""Tests for repository pattern with multi-tenant filtering."""

import pytest

from grid_db.repositories import (
    BaseRepository,
    UserRepository,
    BybitAccountRepository,
    ApiCredentialRepository,
    StrategyRepository,
    RunRepository,
)
from grid_db.models import User, BybitAccount, ApiCredential, Strategy, Run


class TestBaseRepository:
    """Tests for BaseRepository generic CRUD operations."""

    def test_create(self, session):
        """Create entity."""
        repo = BaseRepository(session, User)
        user = User(username="create_test")
        created = repo.create(user)

        assert created.user_id is not None
        assert created.username == "create_test"

    def test_update(self, session, sample_user):
        """Update entity."""
        repo = BaseRepository(session, User)
        sample_user.status = "suspended"
        repo.update(sample_user)

        # BaseRepository doesn't have get_by_id, verify with session
        found = session.get(User, sample_user.user_id)
        assert found.status == "suspended"

    def test_delete(self, session, sample_user):
        """Delete entity."""
        repo = BaseRepository(session, User)
        user_id = sample_user.user_id

        repo.delete(sample_user)

        assert session.get(User, user_id) is None


class TestUserRepository:
    """Tests for UserRepository."""

    def test_get_by_id(self, session, sample_user):
        """Get user by ID."""
        repo = UserRepository(session)
        found = repo.get_by_id(sample_user.user_id)

        assert found is not None
        assert found.username == sample_user.username

    def test_get_by_id_not_found(self, session):
        """Get user with non-existent ID returns None."""
        repo = UserRepository(session)
        found = repo.get_by_id("non-existent-id")

        assert found is None

    def test_get_all(self, session):
        """Get all users with pagination."""
        repo = UserRepository(session)

        # Create multiple users
        for i in range(5):
            repo.create(User(username=f"user_{i}"))

        all_users = repo.get_all(limit=3)
        assert len(all_users) == 3

        all_users = repo.get_all(limit=10, offset=3)
        assert len(all_users) == 2

    def test_get_by_username(self, session, sample_user):
        """Find user by username."""
        repo = UserRepository(session)
        found = repo.get_by_username(sample_user.username)

        assert found is not None
        assert found.user_id == sample_user.user_id

    def test_get_by_username_not_found(self, session):
        """Non-existent username returns None."""
        repo = UserRepository(session)
        found = repo.get_by_username("nonexistent")

        assert found is None

    def test_get_active_users(self, session):
        """Get only active users."""
        repo = UserRepository(session)

        active = User(username="active_user", status="active")
        suspended = User(username="suspended_user", status="suspended")
        deleted = User(username="deleted_user", status="deleted")
        session.add_all([active, suspended, deleted])
        session.flush()

        active_users = repo.get_active_users()
        usernames = [u.username for u in active_users]

        assert "active_user" in usernames
        assert "suspended_user" not in usernames
        assert "deleted_user" not in usernames


class TestBybitAccountRepository:
    """Tests for BybitAccountRepository."""

    def test_get_by_user_id(self, session, sample_user, sample_account):
        """Get all accounts for a user."""
        # Add another account
        account2 = BybitAccount(
            user_id=sample_user.user_id,
            account_name="second_account",
            environment="mainnet",
        )
        session.add(account2)
        session.flush()

        repo = BybitAccountRepository(session)
        accounts = repo.get_by_user_id(sample_user.user_id)

        assert len(accounts) == 2

    def test_get_enabled_accounts(self, session, sample_user, sample_account):
        """Get only enabled accounts."""
        # Add disabled account
        disabled = BybitAccount(
            user_id=sample_user.user_id,
            account_name="disabled_account",
            environment="mainnet",
            status="disabled",
        )
        session.add(disabled)
        session.flush()

        repo = BybitAccountRepository(session)
        enabled = repo.get_enabled_accounts(sample_user.user_id)

        assert len(enabled) == 1
        assert enabled[0].account_name == sample_account.account_name


class TestApiCredentialRepository:
    """Tests for ApiCredentialRepository."""

    def test_get_by_account_id(self, session, sample_user, sample_account, sample_credential):
        """Get all credentials for an account."""
        repo = ApiCredentialRepository(session)
        credentials = repo.get_by_account_id(sample_user.user_id, sample_account.account_id)

        assert len(credentials) == 1

    def test_get_by_account_id_security(self, session, sample_user, sample_account, sample_credential):
        """Cannot access credentials of another user."""
        repo = ApiCredentialRepository(session)
        # Try to access with wrong user_id
        credentials = repo.get_by_account_id("wrong-user-id", sample_account.account_id)

        assert len(credentials) == 0

    def test_get_active_credential(self, session, sample_user, sample_account, sample_credential):
        """Get active credential for an account."""
        # Add inactive credential
        inactive = ApiCredential(
            account_id=sample_account.account_id,
            api_key_id="old_key",
            api_secret="old_secret",
            is_active=False,
        )
        session.add(inactive)
        session.flush()

        repo = ApiCredentialRepository(session)
        active = repo.get_active_credential(sample_user.user_id, sample_account.account_id)

        assert active is not None
        assert active.api_key_id == sample_credential.api_key_id

    def test_get_active_credential_security(self, session, sample_user, sample_account, sample_credential):
        """Cannot access active credential of another user."""
        repo = ApiCredentialRepository(session)
        active = repo.get_active_credential("wrong-user-id", sample_account.account_id)

        assert active is None

    def test_get_active_credential_none(self, session, sample_user, sample_account):
        """Returns None when no active credential."""
        repo = ApiCredentialRepository(session)
        active = repo.get_active_credential(sample_user.user_id, sample_account.account_id)

        assert active is None


class TestStrategyRepository:
    """Tests for StrategyRepository."""

    def test_get_by_account_id(self, session, sample_user, sample_account, sample_strategy):
        """Get all strategies for an account."""
        # Add another strategy
        strategy2 = Strategy(
            account_id=sample_account.account_id,
            strategy_type="GridStrategy",
            symbol="ETHUSDT",
            config_json={},
        )
        session.add(strategy2)
        session.flush()

        repo = StrategyRepository(session)
        strategies = repo.get_by_account_id(sample_user.user_id, sample_account.account_id)

        assert len(strategies) == 2

    def test_get_by_account_id_security(self, session, sample_user, sample_account, sample_strategy):
        """Cannot access strategies of another user."""
        repo = StrategyRepository(session)
        strategies = repo.get_by_account_id("wrong-user-id", sample_account.account_id)

        assert len(strategies) == 0

    def test_get_enabled_strategies(self, session, sample_user, sample_account, sample_strategy):
        """Get only enabled strategies."""
        # Add disabled strategy
        disabled = Strategy(
            account_id=sample_account.account_id,
            strategy_type="GridStrategy",
            symbol="ETHUSDT",
            config_json={},
            is_enabled=False,
        )
        session.add(disabled)
        session.flush()

        repo = StrategyRepository(session)
        enabled = repo.get_enabled_strategies(sample_user.user_id, sample_account.account_id)

        assert len(enabled) == 1

    def test_get_enabled_strategies_security(self, session, sample_user, sample_account, sample_strategy):
        """Cannot access enabled strategies of another user."""
        repo = StrategyRepository(session)
        enabled = repo.get_enabled_strategies("wrong-user-id", sample_account.account_id)

        assert len(enabled) == 0

    def test_get_by_symbol(self, session, sample_user, sample_account, sample_strategy):
        """Get strategy by symbol."""
        repo = StrategyRepository(session)
        strategy = repo.get_by_symbol(sample_user.user_id, sample_account.account_id, "BTCUSDT")

        assert strategy is not None
        assert strategy.strategy_id == sample_strategy.strategy_id

    def test_get_by_symbol_security(self, session, sample_user, sample_account, sample_strategy):
        """Cannot access strategy of another user by symbol."""
        repo = StrategyRepository(session)
        strategy = repo.get_by_symbol("wrong-user-id", sample_account.account_id, "BTCUSDT")

        assert strategy is None

    def test_get_by_symbol_not_found(self, session, sample_user, sample_account):
        """Non-existent symbol returns None."""
        repo = StrategyRepository(session)
        strategy = repo.get_by_symbol(sample_user.user_id, sample_account.account_id, "NONEXISTENT")

        assert strategy is None


class TestRunRepository:
    """Tests for RunRepository with multi-tenant access control."""

    def test_get_by_user_id(self, session, sample_user, sample_account, sample_strategy):
        """Get runs filtered by user_id."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="backtest",
        )
        session.add(run)
        session.flush()

        repo = RunRepository(session)
        runs = repo.get_by_user_id(sample_user.user_id)

        assert len(runs) == 1

    def test_runs_isolated_by_user(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Runs are filtered by user_id - enforces multi-tenant isolation."""
        # Create run for sample_user
        run1 = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="backtest",
        )
        session.add(run1)

        # Create another user with their own account and run
        other_user = User(username="other_user")
        session.add(other_user)
        session.flush()

        other_account = BybitAccount(
            user_id=other_user.user_id,
            account_name="other_account",
            environment="testnet",
        )
        session.add(other_account)
        session.flush()

        other_strategy = Strategy(
            account_id=other_account.account_id,
            strategy_type="GridStrategy",
            symbol="BTCUSDT",
            config_json={},
        )
        session.add(other_strategy)
        session.flush()

        run2 = Run(
            user_id=other_user.user_id,
            account_id=other_account.account_id,
            strategy_id=other_strategy.strategy_id,
            run_type="live",
        )
        session.add(run2)
        session.flush()

        repo = RunRepository(session)

        # Each user only sees their own runs
        user1_runs = repo.get_by_user_id(sample_user.user_id)
        user2_runs = repo.get_by_user_id(other_user.user_id)

        assert len(user1_runs) == 1
        assert user1_runs[0].run_type == "backtest"

        assert len(user2_runs) == 1
        assert user2_runs[0].run_type == "live"

    def test_get_running(self, session, sample_user, sample_account, sample_strategy):
        """Get currently running runs."""
        running = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            status="running",
        )
        completed = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="backtest",
            status="completed",
        )
        session.add_all([running, completed])
        session.flush()

        repo = RunRepository(session)
        running_runs = repo.get_running(sample_user.user_id)

        assert len(running_runs) == 1
        assert running_runs[0].status == "running"

    def test_get_by_account_id(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Get runs by account with user filtering."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="shadow",
        )
        session.add(run)
        session.flush()

        repo = RunRepository(session)
        runs = repo.get_by_account_id(sample_user.user_id, sample_account.account_id)

        assert len(runs) == 1

    def test_get_by_run_type(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Get runs by type."""
        backtest = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="backtest",
        )
        live = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
        )
        session.add_all([backtest, live])
        session.flush()

        repo = RunRepository(session)
        backtests = repo.get_by_run_type(sample_user.user_id, "backtest")

        assert len(backtests) == 1
        assert backtests[0].run_type == "backtest"

    def test_runs_ordered_by_start_ts(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Runs are returned in descending order by start_ts."""
        from datetime import datetime, timedelta, UTC

        now = datetime.now(UTC)

        old_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="backtest",
            start_ts=now - timedelta(days=1),
        )
        new_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            start_ts=now,
        )
        session.add_all([old_run, new_run])
        session.flush()

        repo = RunRepository(session)
        runs = repo.get_by_user_id(sample_user.user_id)

        # Most recent first
        assert runs[0].run_type == "live"
        assert runs[1].run_type == "backtest"
