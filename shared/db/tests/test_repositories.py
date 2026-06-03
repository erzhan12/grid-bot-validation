"""Tests for repository pattern with multi-tenant filtering."""

from datetime import datetime, timedelta, UTC
from decimal import Decimal

from grid_db.repositories import (
    BaseRepository,
    UserRepository,
    BybitAccountRepository,
    ApiCredentialRepository,
    StrategyRepository,
    RunRepository,
    PublicTradeRepository,
    PrivateExecutionRepository,
    GridStateSnapshotRepository,
)
from grid_db.models import (
    User,
    BybitAccount,
    ApiCredential,
    Strategy,
    Run,
    PublicTrade,
    PrivateExecution,
    GridStateSnapshot,
)
from gridcore.persistence import grid_fingerprint_hash


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

    def test_get_latest_by_type_skips_failed(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """get_latest_by_type() skips failed runs by default."""
        from datetime import timedelta

        now = datetime.now(UTC)

        failed_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="recording",
            status="failed",
            start_ts=now,
        )
        completed_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="recording",
            status="completed",
            start_ts=now - timedelta(hours=1),
        )
        session.add_all([failed_run, completed_run])
        session.flush()

        repo = RunRepository(session)
        result = repo.get_latest_by_type("recording")

        # Should skip failed and return completed
        assert result is not None
        assert result.status == "completed"

    def test_get_latest_by_type_returns_running(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """get_latest_by_type() returns running runs."""
        now = datetime.now(UTC)

        running_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="recording",
            status="running",
            start_ts=now,
        )
        session.add(running_run)
        session.flush()

        repo = RunRepository(session)
        result = repo.get_latest_by_type("recording")

        assert result is not None
        assert result.status == "running"

    def test_get_latest_by_type_empty_statuses_returns_any(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """get_latest_by_type() with empty statuses returns any status."""
        now = datetime.now(UTC)

        failed_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="recording",
            status="failed",
            start_ts=now,
        )
        session.add(failed_run)
        session.flush()

        repo = RunRepository(session)
        result = repo.get_latest_by_type("recording", statuses=())

        assert result is not None
        assert result.status == "failed"

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

    def test_close_stale_running_runs_marks_open_runs(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """0062/#148: orphaned running rows are completed before a new run."""
        from datetime import datetime, UTC

        end_ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        stale = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            status="running",
            start_ts=datetime(2026, 5, 1, tzinfo=UTC),
            end_ts=None,
        )
        session.add(stale)
        session.flush()

        closed = RunRepository(session).close_stale_running_runs(
            sample_user.user_id,
            sample_account.account_id,
            sample_strategy.strategy_id,
            "live",
            end_ts=end_ts,
        )

        assert closed == 1
        assert stale.status == "completed"
        assert stale.end_ts == end_ts

    def test_close_stale_running_runs_scoped_by_strategy_and_run_type(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """Only matching (strategy_id, run_type) rows are closed."""
        from datetime import datetime, UTC

        end_ts = datetime(2026, 6, 1, tzinfo=UTC)
        other_strategy = Strategy(
            strategy_id="other-strat-id",
            account_id=sample_account.account_id,
            strategy_type="GridStrategy",
            symbol="ETHUSDT",
            config_json={},
        )
        session.add(other_strategy)
        live_stale = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            status="running",
            start_ts=datetime(2026, 5, 1, tzinfo=UTC),
        )
        shadow_stale = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="shadow",
            status="running",
            start_ts=datetime(2026, 5, 1, tzinfo=UTC),
        )
        other_strat_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=other_strategy.strategy_id,
            run_type="live",
            status="running",
            start_ts=datetime(2026, 5, 1, tzinfo=UTC),
        )
        session.add_all([live_stale, shadow_stale, other_strat_run])
        session.flush()

        closed = RunRepository(session).close_stale_running_runs(
            sample_user.user_id,
            sample_account.account_id,
            sample_strategy.strategy_id,
            "live",
            end_ts=end_ts,
        )

        assert closed == 1
        assert live_stale.status == "completed"
        assert shadow_stale.status == "running"
        assert other_strat_run.status == "running"


class TestPublicTradeRepository:
    """Tests for PublicTradeRepository bulk operations."""

    def test_bulk_insert_new_trades(self, session):
        """Bulk insert inserts all trades when none exist."""
        from datetime import datetime, UTC
        from decimal import Decimal

        repo = PublicTradeRepository(session)
        trades = [
            PublicTrade(
                symbol="BTCUSDT",
                trade_id="trade_1",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                side="Buy",
                price=Decimal("50000.00"),
                size=Decimal("0.001"),
            ),
            PublicTrade(
                symbol="BTCUSDT",
                trade_id="trade_2",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                side="Sell",
                price=Decimal("50001.00"),
                size=Decimal("0.002"),
            ),
        ]

        count = repo.bulk_insert(trades)
        assert count == 2

    def test_bulk_insert_skips_duplicates(self, session):
        """Bulk insert skips duplicates via ON CONFLICT DO NOTHING."""
        from datetime import datetime, UTC
        from decimal import Decimal

        repo = PublicTradeRepository(session)

        # Insert initial trade
        trade1 = PublicTrade(
            symbol="BTCUSDT",
            trade_id="trade_1",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            side="Buy",
            price=Decimal("50000.00"),
            size=Decimal("0.001"),
        )
        repo.bulk_insert([trade1])

        # Try to insert duplicate + new trade
        trades = [
            PublicTrade(
                symbol="BTCUSDT",
                trade_id="trade_1",  # DUPLICATE
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                side="Buy",
                price=Decimal("50000.00"),
                size=Decimal("0.001"),
            ),
            PublicTrade(
                symbol="BTCUSDT",
                trade_id="trade_2",  # NEW
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                side="Sell",
                price=Decimal("50001.00"),
                size=Decimal("0.002"),
            ),
        ]

        count = repo.bulk_insert(trades)
        assert count == 1  # Only new trade inserted

    def test_bulk_insert_empty_list(self, session):
        """Bulk insert with empty list returns 0."""
        repo = PublicTradeRepository(session)
        count = repo.bulk_insert([])
        assert count == 0


class TestPrivateExecutionRepository:
    """Tests for PrivateExecutionRepository bulk operations."""

    def test_bulk_insert_new_executions(self, session, sample_user, sample_account):
        """Bulk insert inserts all executions when none exist."""
        from datetime import datetime, UTC
        from decimal import Decimal

        # Create a run first (required foreign key)
        sample_strategy = Strategy(
            account_id=sample_account.account_id,
            strategy_type="GridStrategy",
            symbol="BTCUSDT",
            config_json={},
        )
        session.add(sample_strategy)
        session.flush()

        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            start_ts=datetime.now(UTC),
        )
        session.add(run)
        session.flush()

        repo = PrivateExecutionRepository(session)
        executions = [
            PrivateExecution(
                run_id=run.run_id,
                account_id=sample_account.account_id,
                symbol="BTCUSDT",
                exec_id="exec_1",
                order_id="order_1",
                order_link_id="link_1",
                exchange_ts=datetime.now(UTC),
                side="Buy",
                exec_price=Decimal("50000.00"),
                exec_qty=Decimal("0.001"),
                exec_fee=Decimal("0.01"),
                closed_pnl=Decimal("0"),
                raw_json={},
            ),
            PrivateExecution(
                run_id=run.run_id,
                account_id=sample_account.account_id,
                symbol="BTCUSDT",
                exec_id="exec_2",
                order_id="order_2",
                order_link_id="link_2",
                exchange_ts=datetime.now(UTC),
                side="Sell",
                exec_price=Decimal("50001.00"),
                exec_qty=Decimal("0.002"),
                exec_fee=Decimal("0.02"),
                closed_pnl=Decimal("0"),
                raw_json={},
            ),
        ]

        count = repo.bulk_insert(executions)
        assert count == 2

    def test_bulk_insert_skips_duplicates(self, session, sample_user, sample_account):
        """Bulk insert skips duplicates via ON CONFLICT DO NOTHING."""
        from datetime import datetime, UTC
        from decimal import Decimal

        # Create a run first
        sample_strategy = Strategy(
            account_id=sample_account.account_id,
            strategy_type="GridStrategy",
            symbol="BTCUSDT",
            config_json={},
        )
        session.add(sample_strategy)
        session.flush()

        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            start_ts=datetime.now(UTC),
        )
        session.add(run)
        session.flush()

        repo = PrivateExecutionRepository(session)

        # Insert initial execution
        exec1 = PrivateExecution(
            run_id=run.run_id,
            account_id=sample_account.account_id,
            symbol="BTCUSDT",
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="link_1",
            exchange_ts=datetime.now(UTC),
            side="Buy",
            exec_price=Decimal("50000.00"),
            exec_qty=Decimal("0.001"),
            exec_fee=Decimal("0.01"),
            closed_pnl=Decimal("0"),
            raw_json={},
        )
        repo.bulk_insert([exec1])

        # Try to insert duplicate + new execution
        executions = [
            PrivateExecution(
                run_id=run.run_id,
                account_id=sample_account.account_id,
                symbol="BTCUSDT",
                exec_id="exec_1",  # DUPLICATE
                order_id="order_1",
                order_link_id="link_1",
                exchange_ts=datetime.now(UTC),
                side="Buy",
                exec_price=Decimal("50000.00"),
                exec_qty=Decimal("0.001"),
                exec_fee=Decimal("0.01"),
                closed_pnl=Decimal("0"),
                raw_json={},
            ),
            PrivateExecution(
                run_id=run.run_id,
                account_id=sample_account.account_id,
                symbol="BTCUSDT",
                exec_id="exec_2",  # NEW
                order_id="order_2",
                order_link_id="link_2",
                exchange_ts=datetime.now(UTC),
                side="Sell",
                exec_price=Decimal("50001.00"),
                exec_qty=Decimal("0.002"),
                exec_fee=Decimal("0.02"),
                closed_pnl=Decimal("0"),
                raw_json={},
            ),
        ]

        count = repo.bulk_insert(executions)
        assert count == 1  # Only new execution inserted

    def test_bulk_insert_empty_list(self, session):
        """Bulk insert with empty list returns 0."""
        repo = PrivateExecutionRepository(session)
        count = repo.bulk_insert([])
        assert count == 0

class TestOrderRepository:
    """Test OrderRepository bulk insert and conflict handling."""

    def test_bulk_insert_new_orders(self, session, sample_user, sample_account, sample_run):
        """Test bulk insert creates new order records."""
        from grid_db import OrderRepository, Order
        from decimal import Decimal

        repo = OrderRepository(session)
        models = [
            Order(
                account_id=str(sample_account.account_id),
                run_id=str(sample_run.run_id),
                order_id="order1",
                order_link_id="link1",
                symbol="BTCUSDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                status="New",
                side="Buy",
                price=Decimal("50000.0"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
            ),
            Order(
                account_id=str(sample_account.account_id),
                run_id=str(sample_run.run_id),
                order_id="order2",
                order_link_id="link2",
                symbol="ETHUSDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                status="New",
                side="Sell",
                price=Decimal("3000.0"),
                qty=Decimal("2.0"),
                leaves_qty=Decimal("2.0"),
            ),
        ]
        count = repo.bulk_insert(models)
        assert count == 2

        # Verify orders exist
        orders = session.query(Order).all()
        assert len(orders) == 2

    def test_bulk_insert_updates_existing(self, session, sample_user, sample_account, sample_run):
        """Test bulk insert updates existing orders with same account_id/order_id/exchange_ts."""
        from grid_db import OrderRepository, Order
        from decimal import Decimal

        repo = OrderRepository(session)
        ts = datetime.now(UTC)

        # Insert initial order
        initial = Order(
            account_id=str(sample_account.account_id),
            run_id=str(sample_run.run_id),
            order_id="order1",
            order_link_id="link1",
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=datetime.now(UTC),
            status="New",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("1.0"),
            leaves_qty=Decimal("1.0"),
        )
        repo.bulk_insert([initial])

        # Update with same account_id/order_id/exchange_ts but different status
        updated = Order(
            account_id=str(sample_account.account_id),
            run_id=str(sample_run.run_id),
            order_id="order1",
            order_link_id="link1",
            symbol="BTCUSDT",
            exchange_ts=ts,  # Same timestamp
            local_ts=datetime.now(UTC),
            status="Filled",  # Changed status
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("1.0"),
            leaves_qty=Decimal("0.0"),  # Changed leaves_qty
        )
        count = repo.bulk_insert([updated])
        assert count == 1  # Upsert should update 1 row

        # Verify status and leaves_qty were updated
        orders = session.query(Order).filter_by(order_id="order1").all()
        assert len(orders) == 1
        assert orders[0].status == "Filled"
        assert orders[0].leaves_qty == Decimal("0.0")

    def test_bulk_insert_different_accounts_no_conflict(self, session, sample_user, sample_run):
        """Test same order_id on different accounts doesn't conflict."""
        from grid_db import OrderRepository, Order, BybitAccount
        from decimal import Decimal

        # Create two different accounts
        account1 = BybitAccount(
            user_id=sample_user.user_id,
            account_name="account1",
            environment="testnet",
        )
        account2 = BybitAccount(
            user_id=sample_user.user_id,
            account_name="account2",
            environment="testnet",
        )
        session.add_all([account1, account2])
        session.flush()

        repo = OrderRepository(session)
        ts = datetime.now(UTC)

        # Insert same order_id for both accounts
        models = [
            Order(
                account_id=str(account1.account_id),
                run_id=str(sample_run.run_id),
                order_id="order1",  # Same order_id
                order_link_id="link1",
                symbol="BTCUSDT",
                exchange_ts=ts,
                local_ts=datetime.now(UTC),
                status="New",
                side="Buy",
                price=Decimal("50000.0"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
            ),
            Order(
                account_id=str(account2.account_id),  # Different account
                run_id=str(sample_run.run_id),
                order_id="order1",  # Same order_id
                order_link_id="link1",
                symbol="BTCUSDT",
                exchange_ts=ts,
                local_ts=datetime.now(UTC),
                status="New",
                side="Sell",
                price=Decimal("50000.0"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
            ),
        ]
        count = repo.bulk_insert(models)
        assert count == 2  # Both should be inserted

        # Verify both orders exist
        orders = session.query(Order).filter_by(order_id="order1").all()
        assert len(orders) == 2


class TestPositionSnapshotRepository:
    """Test PositionSnapshotRepository bulk insert and queries."""

    def test_bulk_insert_positions(self, session, sample_account):
        """Test bulk insert creates position snapshots."""
        from grid_db import PositionSnapshotRepository, PositionSnapshot
        from decimal import Decimal

        repo = PositionSnapshotRepository(session)
        models = [
            PositionSnapshot(
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                side="Buy",
                size=Decimal("1.0"),
                entry_price=Decimal("50000.0"),
                liq_price=Decimal("45000.0"),
                unrealised_pnl=Decimal("100.50"),
            ),
        ]
        count = repo.bulk_insert(models)
        assert count == 1

        # Verify position exists
        positions = session.query(PositionSnapshot).all()
        assert len(positions) == 1
        assert positions[0].symbol == "BTCUSDT"
        assert positions[0].size == Decimal("1.0")

    def test_get_latest_by_account_symbol(self, session, sample_account):
        """Test retrieval of most recent position."""
        from grid_db import PositionSnapshotRepository, PositionSnapshot
        from decimal import Decimal
        import time

        repo = PositionSnapshotRepository(session)

        # Insert 3 positions at different timestamps
        ts1 = datetime.now(UTC)
        time.sleep(0.01)  # Ensure different timestamps
        ts2 = datetime.now(UTC)
        time.sleep(0.01)
        ts3 = datetime.now(UTC)

        models = [
            PositionSnapshot(
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=ts1,
                local_ts=datetime.now(UTC),
                side="Buy",
                size=Decimal("1.0"),
                entry_price=Decimal("50000.0"),
            ),
            PositionSnapshot(
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=ts2,
                local_ts=datetime.now(UTC),
                side="Buy",
                size=Decimal("1.5"),
                entry_price=Decimal("51000.0"),
            ),
            PositionSnapshot(
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=ts3,  # Latest
                local_ts=datetime.now(UTC),
                side="Buy",
                size=Decimal("2.0"),
                entry_price=Decimal("52000.0"),
            ),
        ]
        repo.bulk_insert(models)

        # Query latest
        latest = repo.get_latest_by_account_symbol(
            str(sample_account.account_id),
            "BTCUSDT"
        )

        assert latest is not None
        assert latest.size == Decimal("2.0")
        assert latest.entry_price == Decimal("52000.0")
        # Compare timestamps (may lose timezone info in SQLite)
        assert latest.exchange_ts.replace(tzinfo=None) == ts3.replace(tzinfo=None)


class TestWalletSnapshotRepository:
    """Test WalletSnapshotRepository bulk insert and queries."""

    def test_bulk_insert_wallets(self, session, sample_account):
        """Test bulk insert creates wallet snapshots."""
        from grid_db import WalletSnapshotRepository, WalletSnapshot
        from decimal import Decimal

        repo = WalletSnapshotRepository(session)
        models = [
            WalletSnapshot(
                account_id=str(sample_account.account_id),
                coin="USDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                wallet_balance=Decimal("10000.00"),
                available_balance=Decimal("9500.00"),
                total_equity=Decimal("15000.50"),
                total_available_balance=Decimal("14000.25"),
                total_margin_balance=Decimal("14900.75"),
                account_im_rate=Decimal("0.01000000"),
                account_mm_rate=Decimal("0.00500000"),
            ),
        ]
        count = repo.bulk_insert(models)
        assert count == 1

        # Verify wallet exists
        wallets = session.query(WalletSnapshot).all()
        assert len(wallets) == 1
        assert wallets[0].coin == "USDT"
        assert wallets[0].wallet_balance == Decimal("10000.00")
        assert wallets[0].total_equity == Decimal("15000.50")
        assert wallets[0].total_available_balance == Decimal("14000.25")
        assert wallets[0].total_margin_balance == Decimal("14900.75")
        assert wallets[0].account_im_rate == Decimal("0.01000000")
        assert wallets[0].account_mm_rate == Decimal("0.00500000")

    def test_get_latest_by_account_coin(self, session, sample_account):
        """Test retrieval of most recent wallet balance."""
        from grid_db import WalletSnapshotRepository, WalletSnapshot
        from decimal import Decimal
        import time

        repo = WalletSnapshotRepository(session)

        # Insert 3 snapshots for USDT
        ts1 = datetime.now(UTC)
        time.sleep(0.01)
        ts2 = datetime.now(UTC)
        time.sleep(0.01)
        ts3 = datetime.now(UTC)

        models = [
            WalletSnapshot(
                account_id=str(sample_account.account_id),
                coin="USDT",
                exchange_ts=ts1,
                local_ts=datetime.now(UTC),
                wallet_balance=Decimal("10000.00"),
                available_balance=Decimal("9500.00"),
            ),
            WalletSnapshot(
                account_id=str(sample_account.account_id),
                coin="USDT",
                exchange_ts=ts2,
                local_ts=datetime.now(UTC),
                wallet_balance=Decimal("10500.00"),
                available_balance=Decimal("10000.00"),
            ),
            WalletSnapshot(
                account_id=str(sample_account.account_id),
                coin="USDT",
                exchange_ts=ts3,  # Latest
                local_ts=datetime.now(UTC),
                wallet_balance=Decimal("11000.00"),
                available_balance=Decimal("10500.00"),
            ),
        ]
        repo.bulk_insert(models)

        # Query latest
        latest = repo.get_latest_by_account_coin(
            str(sample_account.account_id),
            "USDT"
        )

        assert latest is not None
        assert latest.wallet_balance == Decimal("11000.00")
        assert latest.available_balance == Decimal("10500.00")
        # Compare timestamps (may lose timezone info in SQLite)
        assert latest.exchange_ts.replace(tzinfo=None) == ts3.replace(tzinfo=None)

    def test_get_by_account_range(self, session, sample_account):
        """Test retrieval of wallet snapshots within a time range."""
        from grid_db import WalletSnapshotRepository, WalletSnapshot
        from decimal import Decimal
        from datetime import timedelta

        repo = WalletSnapshotRepository(session)

        base_ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        models = [
            WalletSnapshot(
                account_id=str(sample_account.account_id),
                coin="USDT",
                exchange_ts=base_ts,
                local_ts=base_ts,
                wallet_balance=Decimal("10000.00"),
                available_balance=Decimal("9500.00"),
            ),
            WalletSnapshot(
                account_id=str(sample_account.account_id),
                coin="USDT",
                exchange_ts=base_ts + timedelta(hours=1),
                local_ts=base_ts + timedelta(hours=1),
                wallet_balance=Decimal("10050.00"),
                available_balance=Decimal("9550.00"),
            ),
            WalletSnapshot(
                account_id=str(sample_account.account_id),
                coin="USDT",
                exchange_ts=base_ts + timedelta(hours=2),
                local_ts=base_ts + timedelta(hours=2),
                wallet_balance=Decimal("10030.00"),
                available_balance=Decimal("9530.00"),
            ),
        ]
        repo.bulk_insert(models)

        # Query range that covers first two snapshots
        results = repo.get_by_account_range(
            str(sample_account.account_id),
            "USDT",
            base_ts,
            base_ts + timedelta(hours=1),
        )

        assert len(results) == 2
        assert results[0].wallet_balance == Decimal("10000.00")
        assert results[1].wallet_balance == Decimal("10050.00")

        # Query range that covers none
        results = repo.get_by_account_range(
            str(sample_account.account_id),
            "USDT",
            base_ts - timedelta(hours=2),
            base_ts - timedelta(hours=1),
        )
        assert len(results) == 0

    def test_get_all_coins_latest_before(self, session, sample_account, sample_run):
        """Feature 0065: latest row per coin at-or-before at_ts, run-scoped."""
        from grid_db import WalletSnapshotRepository, WalletSnapshot
        from decimal import Decimal

        repo = WalletSnapshotRepository(session)
        base = datetime(2026, 6, 1, 17, 38, 0, tzinfo=UTC)
        repo.bulk_insert([
            # USDT: two rows — newer one (<= at_ts) wins.
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base, local_ts=base, coin="USDT",
                wallet_balance=Decimal("100"), available_balance=Decimal("100"),
            ),
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base + timedelta(seconds=60),
                local_ts=base + timedelta(seconds=60), coin="USDT",
                wallet_balance=Decimal("170"), available_balance=Decimal("110"),
            ),
            # SOL: single row.
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base + timedelta(seconds=30),
                local_ts=base + timedelta(seconds=30), coin="SOL",
                wallet_balance=Decimal("0.2473524"),
                available_balance=Decimal("0.2473524"),
            ),
            # Future row beyond at_ts — must be excluded for both coins.
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base + timedelta(seconds=300),
                local_ts=base + timedelta(seconds=300), coin="SOL",
                wallet_balance=Decimal("99"), available_balance=Decimal("99"),
            ),
        ])

        at_ts = base + timedelta(seconds=120)
        rows = repo.get_all_coins_latest_before(
            sample_run.run_id, str(sample_account.account_id), at_ts
        )
        by_coin = {r.coin: r for r in rows}
        assert set(by_coin) == {"USDT", "SOL"}
        assert by_coin["USDT"].wallet_balance == Decimal("170")  # newer USDT row
        assert by_coin["SOL"].wallet_balance == Decimal("0.2473524")  # not the future 99

    def test_get_all_coins_latest_before_excludes_other_runs(
        self, session, sample_user, sample_account, sample_strategy, sample_run
    ):
        """Feature 0065: rows from a different run must not leak in."""
        from grid_db import WalletSnapshotRepository, WalletSnapshot
        from grid_db.models import Run
        from decimal import Decimal

        other_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live", status="completed",
            start_ts=datetime(2026, 5, 31, tzinfo=UTC),
        )
        session.add(other_run)
        session.flush()

        repo = WalletSnapshotRepository(session)
        base = datetime(2026, 6, 1, 17, 38, 0, tzinfo=UTC)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=other_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base, local_ts=base, coin="SOL",
                wallet_balance=Decimal("5"), available_balance=Decimal("5"),
            ),
        ])

        rows = repo.get_all_coins_latest_before(
            sample_run.run_id, str(sample_account.account_id),
            base + timedelta(seconds=60),
        )
        assert rows == []

    def test_get_all_coins_latest_before_tie_is_deterministic(
        self, session, sample_account, sample_run
    ):
        """Feature 0065: two rows at the same exchange_ts → ordered (coin, id)
        so a last-wins caller deterministically gets the highest-id row."""
        from grid_db import WalletSnapshotRepository, WalletSnapshot
        from decimal import Decimal

        repo = WalletSnapshotRepository(session)
        base = datetime(2026, 6, 1, 17, 38, 0, tzinfo=UTC)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base, local_ts=base, coin="SOL",
                wallet_balance=Decimal("1"), available_balance=Decimal("1"),
            ),
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base, local_ts=base, coin="SOL",  # same ts → tie
                wallet_balance=Decimal("2"), available_balance=Decimal("2"),
            ),
        ])

        rows = [
            r for r in repo.get_all_coins_latest_before(
                sample_run.run_id, str(sample_account.account_id),
                base + timedelta(seconds=1),
            )
            if r.coin == "SOL"
        ]
        # Ordered ascending by id → last is the highest-id (latest insert).
        assert rows[-1].wallet_balance == Decimal("2")


class TestSeedAwareReplayRepositoryMethods:
    """Feature 0029: get_latest_before / get_active_at on the three
    private-stream repositories. Run-scoped queries that the seed loader
    in apps/replay relies on."""

    def test_position_get_latest_before_picks_older_when_ts_between(
        self, session, sample_account, sample_run
    ):
        from grid_db import PositionSnapshotRepository, PositionSnapshot
        from decimal import Decimal
        from datetime import timedelta

        repo = PositionSnapshotRepository(session)
        base = datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC)
        repo.bulk_insert([
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT", exchange_ts=base, local_ts=base,
                side="Buy", size=Decimal("1.0"), entry_price=Decimal("50000"),
            ),
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=base + timedelta(seconds=10),
                local_ts=base + timedelta(seconds=10),
                side="Buy", size=Decimal("2.0"), entry_price=Decimal("50100"),
            ),
        ])

        # at_ts strictly between → older row.
        between = base + timedelta(seconds=5)
        got = repo.get_latest_before(
            sample_run.run_id, str(sample_account.account_id),
            "BTCUSDT", "Buy", between,
        )
        assert got is not None
        assert got.size == Decimal("1.0")

        # at_ts at or after the newer row → newer row.
        got = repo.get_latest_before(
            sample_run.run_id, str(sample_account.account_id),
            "BTCUSDT", "Buy", base + timedelta(seconds=20),
        )
        assert got is not None
        assert got.size == Decimal("2.0")

    def test_position_get_latest_before_excludes_other_runs(
        self, session, sample_user, sample_account, sample_strategy, sample_run
    ):
        """Same account/symbol/side in a DIFFERENT run must not leak in."""
        from grid_db import PositionSnapshotRepository, PositionSnapshot
        from grid_db.models import Run
        from decimal import Decimal

        # Make a second run for the same account.
        other_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live", status="completed",
            start_ts=datetime(2026, 5, 6, tzinfo=UTC),
        )
        session.add(other_run)
        session.flush()

        repo = PositionSnapshotRepository(session)
        ts = datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC)
        repo.bulk_insert([
            PositionSnapshot(
                run_id=other_run.run_id,  # OTHER RUN
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT", exchange_ts=ts, local_ts=ts,
                side="Buy", size=Decimal("99"),
                entry_price=Decimal("50000"),
            ),
        ])

        # Queried with sample_run.run_id → no row.
        got = repo.get_latest_before(
            sample_run.run_id, str(sample_account.account_id),
            "BTCUSDT", "Buy", ts + timedelta(seconds=10),
        )
        assert got is None

    def test_wallet_get_latest_before_filters_by_coin_and_run(
        self, session, sample_account, sample_run
    ):
        from grid_db import WalletSnapshotRepository, WalletSnapshot
        from decimal import Decimal
        from datetime import timedelta

        repo = WalletSnapshotRepository(session)
        base = datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base, local_ts=base,
                coin="USDT",
                wallet_balance=Decimal("100"),
                available_balance=Decimal("100"),
            ),
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base, local_ts=base,
                coin="BTC",  # different coin — must not be returned for USDT query
                wallet_balance=Decimal("0.5"),
                available_balance=Decimal("0.5"),
            ),
        ])

        got = repo.get_latest_before(
            sample_run.run_id, str(sample_account.account_id),
            "USDT", base + timedelta(seconds=1),
        )
        assert got is not None
        assert got.coin == "USDT"
        assert got.wallet_balance == Decimal("100")

    def test_order_get_active_at_returns_only_latest_active(
        self, session, sample_account, sample_run
    ):
        """Per order_id, only the latest snapshot at_or_before at_ts; only
        active states (New/PartiallyFilled) with leaves_qty > 0 are kept.
        """
        from grid_db import OrderRepository, Order
        from decimal import Decimal
        from datetime import timedelta

        repo = OrderRepository(session)
        base = datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC)
        repo.bulk_insert([
            # Order 1: New then Filled — should NOT be in active set.
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="O1", symbol="BTCUSDT",
                exchange_ts=base, local_ts=base,
                status="New", side="Buy",
                price=Decimal("50000"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0.001"), reduce_only=False,
            ),
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="O1", symbol="BTCUSDT",
                exchange_ts=base + timedelta(seconds=5),
                local_ts=base + timedelta(seconds=5),
                status="Filled", side="Buy",
                price=Decimal("50000"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0"), reduce_only=False,
            ),
            # Order 2: only New — IS in active set.
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="O2", symbol="BTCUSDT",
                exchange_ts=base + timedelta(seconds=2),
                local_ts=base + timedelta(seconds=2),
                status="New", side="Sell",
                price=Decimal("51000"), qty=Decimal("0.002"),
                leaves_qty=Decimal("0.002"), reduce_only=False,
            ),
        ])

        active = repo.get_active_at(
            sample_run.run_id, str(sample_account.account_id),
            "BTCUSDT", base + timedelta(seconds=10),
        )
        assert len(active) == 1
        assert active[0].order_id == "O2"

    def test_order_get_active_at_excludes_prior_run(
        self, session, sample_user, sample_account, sample_strategy, sample_run
    ):
        """Stale 'New' row from a previous run must not leak when recorder
        was killed before the terminal update."""
        from grid_db import OrderRepository, Order
        from grid_db.models import Run
        from decimal import Decimal

        prior_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live", status="completed",
            start_ts=datetime(2026, 5, 6, tzinfo=UTC),
        )
        session.add(prior_run)
        session.flush()

        repo = OrderRepository(session)
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        repo.bulk_insert([
            Order(
                run_id=prior_run.run_id,  # PRIOR RUN
                account_id=str(sample_account.account_id),
                order_id="O-leaked", symbol="BTCUSDT",
                exchange_ts=ts, local_ts=ts,
                status="New", side="Buy",
                price=Decimal("50000"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0.001"), reduce_only=False,
            ),
        ])

        # New run starts the next day; query at later timestamp.
        active = repo.get_active_at(
            sample_run.run_id, str(sample_account.account_id),
            "BTCUSDT", datetime(2026, 5, 7, 13, 0, 0, tzinfo=UTC),
        )
        assert active == []

    def test_order_get_active_at_persists_reduce_only(
        self, session, sample_account, sample_run
    ):
        """The new column must round-trip through bulk_insert + query."""
        from grid_db import OrderRepository, Order
        from decimal import Decimal

        repo = OrderRepository(session)
        ts = datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC)
        repo.bulk_insert([
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="O-ro", symbol="BTCUSDT",
                exchange_ts=ts, local_ts=ts,
                status="New", side="Sell",
                price=Decimal("51000"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0.001"), reduce_only=True,
            ),
        ])
        active = repo.get_active_at(
            sample_run.run_id, str(sample_account.account_id),
            "BTCUSDT", ts + timedelta(seconds=10),
        )
        assert len(active) == 1
        assert active[0].reduce_only is True


class TestTickerSnapshotRepository:
    """Test TickerSnapshotRepository bulk insert and queries."""

    def test_bulk_insert_tickers(self, session):
        """Test bulk insert creates ticker snapshots."""
        from grid_db import TickerSnapshotRepository, TickerSnapshot
        from decimal import Decimal
        from datetime import datetime, UTC

        repo = TickerSnapshotRepository(session)
        models = [
            TickerSnapshot(
                symbol="BTCUSDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                last_price=Decimal("50000.00"),
                mark_price=Decimal("50001.00"),
                bid1_price=Decimal("49999.00"),
                ask1_price=Decimal("50002.00"),
                funding_rate=Decimal("0.0001"),
            )
        ]

        count = repo.bulk_insert(models)
        assert count == 1

        rows = session.query(TickerSnapshot).all()
        assert len(rows) == 1
        assert rows[0].symbol == "BTCUSDT"

    def test_bulk_insert_skips_duplicates(self, session):
        """Duplicate (symbol, exchange_ts) is skipped via ON CONFLICT DO NOTHING."""
        from grid_db import TickerSnapshotRepository, TickerSnapshot
        from decimal import Decimal
        from datetime import datetime, UTC

        repo = TickerSnapshotRepository(session)
        ts = datetime.now(UTC)
        first = TickerSnapshot(
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=datetime.now(UTC),
            last_price=Decimal("50000.00"),
            mark_price=Decimal("50001.00"),
            bid1_price=Decimal("49999.00"),
            ask1_price=Decimal("50002.00"),
            funding_rate=Decimal("0.0001"),
        )
        repo.bulk_insert([first])

        dup_and_new = [
            TickerSnapshot(
                symbol="BTCUSDT",
                exchange_ts=ts,  # duplicate ts
                local_ts=datetime.now(UTC),
                last_price=Decimal("50010.00"),
                mark_price=Decimal("50011.00"),
                bid1_price=Decimal("50009.00"),
                ask1_price=Decimal("50012.00"),
                funding_rate=Decimal("0.0002"),
            ),
            TickerSnapshot(
                symbol="BTCUSDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                last_price=Decimal("50020.00"),
                mark_price=Decimal("50021.00"),
                bid1_price=Decimal("50019.00"),
                ask1_price=Decimal("50022.00"),
                funding_rate=Decimal("0.0003"),
            ),
        ]

        count = repo.bulk_insert(dup_and_new)
        assert count == 1

    def test_get_latest_by_symbol(self, session):
        """Latest snapshot by exchange_ts is returned."""
        from grid_db import TickerSnapshotRepository, TickerSnapshot
        from decimal import Decimal
        from datetime import datetime, UTC
        import time

        repo = TickerSnapshotRepository(session)

        ts1 = datetime.now(UTC)
        time.sleep(0.01)
        ts2 = datetime.now(UTC)

        repo.bulk_insert(
            [
                TickerSnapshot(
                    symbol="BTCUSDT",
                    exchange_ts=ts1,
                    local_ts=datetime.now(UTC),
                    last_price=Decimal("50000.00"),
                    mark_price=Decimal("50001.00"),
                    bid1_price=Decimal("49999.00"),
                    ask1_price=Decimal("50002.00"),
                    funding_rate=Decimal("0.0001"),
                ),
                TickerSnapshot(
                    symbol="BTCUSDT",
                    exchange_ts=ts2,
                    local_ts=datetime.now(UTC),
                    last_price=Decimal("51000.00"),
                    mark_price=Decimal("51001.00"),
                    bid1_price=Decimal("50999.00"),
                    ask1_price=Decimal("51002.00"),
                    funding_rate=Decimal("0.0002"),
                ),
            ]
        )

        latest = repo.get_latest_by_symbol("BTCUSDT")
        assert latest is not None
        assert latest.last_price == Decimal("51000.00")
        assert latest.exchange_ts.replace(tzinfo=None) == ts2.replace(tzinfo=None)

    def test_get_mark_at_or_before_returns_latest_le_ts(self, session):
        """Feature 0065: latest mark_price with exchange_ts <= at_ts (carry-forward)."""
        from grid_db import TickerSnapshotRepository, TickerSnapshot
        from decimal import Decimal

        repo = TickerSnapshotRepository(session)
        base = datetime(2026, 5, 27, 16, 58, 0, tzinfo=UTC)
        repo.bulk_insert([
            TickerSnapshot(
                symbol="SOLUSDT", exchange_ts=base, local_ts=base,
                last_price=Decimal("84.10"), mark_price=Decimal("84.15"),
                bid1_price=Decimal("84.09"), ask1_price=Decimal("84.11"),
                funding_rate=Decimal("0.0001"),
            ),
            TickerSnapshot(
                symbol="SOLUSDT",
                exchange_ts=base + timedelta(seconds=30),
                local_ts=base + timedelta(seconds=30),
                last_price=Decimal("84.50"), mark_price=Decimal("84.55"),
                bid1_price=Decimal("84.49"), ask1_price=Decimal("84.51"),
                funding_rate=Decimal("0.0001"),
            ),
        ])

        # at_ts strictly between the two rows → older mark carried forward.
        between = base + timedelta(seconds=10)
        assert repo.get_mark_at_or_before("SOLUSDT", between) == Decimal("84.15")
        # at_ts at/after newer row → newer mark.
        assert repo.get_mark_at_or_before(
            "SOLUSDT", base + timedelta(seconds=60)
        ) == Decimal("84.55")

    def test_get_mark_at_or_before_none_when_no_row(self, session):
        """Feature 0065: None when no row exists at-or-before at_ts."""
        from grid_db import TickerSnapshotRepository, TickerSnapshot
        from decimal import Decimal

        repo = TickerSnapshotRepository(session)
        base = datetime(2026, 5, 27, 16, 58, 0, tzinfo=UTC)
        repo.bulk_insert([
            TickerSnapshot(
                symbol="SOLUSDT", exchange_ts=base, local_ts=base,
                last_price=Decimal("84.10"), mark_price=Decimal("84.15"),
                bid1_price=Decimal("84.09"), ask1_price=Decimal("84.11"),
                funding_rate=Decimal("0.0001"),
            ),
        ])
        # Before the only row → None.
        assert repo.get_mark_at_or_before(
            "SOLUSDT", base - timedelta(seconds=1)
        ) is None
        # Wrong symbol → None.
        assert repo.get_mark_at_or_before(
            "LTCUSDT", base + timedelta(seconds=1)
        ) is None


class TestPositionSnapshotSourceFiltering0034:
    """Feature 0034 — source-filtered reads on PositionSnapshotRepository."""

    def _make_snap(self, account_id, ts, source, run_id=None, side="Buy"):
        from grid_db import PositionSnapshot
        from decimal import Decimal
        return PositionSnapshot(
            run_id=run_id,
            account_id=str(account_id),
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            side=side,
            size=Decimal("1"),
            entry_price=Decimal("100"),
            liq_price=Decimal("90"),
            unrealised_pnl=Decimal("0"),
            source=source,
            mark_price=Decimal("101"),
            position_im=Decimal("10"),
            position_mm=Decimal("0.5"),
            cum_realised_pnl=Decimal("5"),
            cur_realised_pnl=Decimal("1.75"),
            position_value=Decimal("100.50"),
        )

    def test_bulk_insert_round_trips_new_columns(self, session, sample_account):
        from grid_db import PositionSnapshotRepository, PositionSnapshot
        from decimal import Decimal

        ts = datetime.now(UTC)
        repo = PositionSnapshotRepository(session)
        repo.bulk_insert([self._make_snap(sample_account.account_id, ts, "backtest")])

        loaded = session.query(PositionSnapshot).one()
        assert loaded.source == "backtest"
        assert loaded.mark_price == Decimal("101")
        assert loaded.position_im == Decimal("10")
        assert loaded.position_mm == Decimal("0.5")
        assert loaded.cum_realised_pnl == Decimal("5")
        # 0056: cycle-scoped realized PnL round-trips via bulk_insert.
        assert loaded.cur_realised_pnl == Decimal("1.75")
        # 0059: position_value column round-trips via bulk_insert (value is opaque storage).
        assert loaded.position_value == Decimal("100.50")

    def test_bulk_insert_round_trips_null_cur_realised_pnl(
        self, session, sample_account,
    ):
        """0056: an explicit None cur_realised_pnl round-trips as NULL."""
        from grid_db import PositionSnapshotRepository, PositionSnapshot

        ts = datetime.now(UTC)
        snap = self._make_snap(sample_account.account_id, ts, "live")
        snap.cur_realised_pnl = None
        repo = PositionSnapshotRepository(session)
        repo.bulk_insert([snap])

        loaded = session.query(PositionSnapshot).one()
        assert loaded.cur_realised_pnl is None

    def test_bulk_insert_round_trips_null_position_value(
        self, session, sample_account,
    ):
        """0059: an explicit None position_value round-trips as NULL."""
        from grid_db import PositionSnapshotRepository, PositionSnapshot

        ts = datetime.now(UTC)
        snap = self._make_snap(sample_account.account_id, ts, "live")
        snap.position_value = None
        repo = PositionSnapshotRepository(session)
        repo.bulk_insert([snap])

        loaded = session.query(PositionSnapshot).one()
        assert loaded.position_value is None

    def test_get_latest_by_account_symbol_defaults_live(self, session, sample_account):
        from grid_db import PositionSnapshotRepository
        import time
        repo = PositionSnapshotRepository(session)
        ts1 = datetime.now(UTC)
        time.sleep(0.01)
        ts2 = datetime.now(UTC)
        repo.bulk_insert([
            self._make_snap(sample_account.account_id, ts1, "live"),
            self._make_snap(sample_account.account_id, ts2, "backtest"),
        ])
        latest = repo.get_latest_by_account_symbol(
            str(sample_account.account_id), "BTCUSDT",
        )
        assert latest is not None
        assert latest.source == "live"

    def test_get_latest_by_account_symbol_explicit_backtest(self, session, sample_account):
        from grid_db import PositionSnapshotRepository
        import time
        repo = PositionSnapshotRepository(session)
        ts1 = datetime.now(UTC)
        time.sleep(0.01)
        ts2 = datetime.now(UTC)
        repo.bulk_insert([
            self._make_snap(sample_account.account_id, ts1, "live"),
            self._make_snap(sample_account.account_id, ts2, "backtest"),
        ])
        latest = repo.get_latest_by_account_symbol(
            str(sample_account.account_id), "BTCUSDT", source="backtest",
        )
        assert latest is not None
        assert latest.source == "backtest"

    def test_get_latest_by_account_symbol_source_none_union(self, session, sample_account):
        from grid_db import PositionSnapshotRepository
        import time
        repo = PositionSnapshotRepository(session)
        ts1 = datetime.now(UTC)
        time.sleep(0.01)
        ts2 = datetime.now(UTC)
        repo.bulk_insert([
            self._make_snap(sample_account.account_id, ts1, "live"),
            self._make_snap(sample_account.account_id, ts2, "backtest"),
        ])
        latest = repo.get_latest_by_account_symbol(
            str(sample_account.account_id), "BTCUSDT", source=None,
        )
        assert latest is not None
        # union by timestamp → ts2 wins
        assert latest.source == "backtest"

    def test_get_latest_before_filters_source(self, session, sample_user, sample_account, sample_strategy):
        """get_latest_before with explicit run_id filters by source."""
        from grid_db import PositionSnapshotRepository, Run

        # get_latest_before requires a run_id; create a Run record for FK.
        run = Run(
            run_id="r1",
            user_id=sample_user.user_id,
            run_type="live",
            account_id=str(sample_account.account_id),
            strategy_id=sample_strategy.strategy_id,
            start_ts=datetime.now(UTC),
        )
        session.add(run)
        session.commit()

        repo = PositionSnapshotRepository(session)
        ts = datetime.now(UTC)
        repo.bulk_insert([
            self._make_snap(sample_account.account_id, ts, "live", run_id="r1"),
            self._make_snap(sample_account.account_id, ts, "backtest", run_id="r1"),
        ])
        # default = live
        result = repo.get_latest_before("r1", str(sample_account.account_id), "BTCUSDT", "Buy", ts)
        assert result is not None
        assert result.source == "live"
        result = repo.get_latest_before(
            "r1", str(sample_account.account_id), "BTCUSDT", "Buy", ts, source="backtest"
        )
        assert result is not None
        assert result.source == "backtest"


class TestGridStateSnapshotRepository:
    """Tests for GridStateSnapshotRepository."""

    def test_get_latest_picks_newest_row_by_exchange_ts_then_id(
        self, session, sample_run,
    ):
        """``get_latest`` uses ``ORDER BY exchange_ts DESC, id DESC``."""
        repo = GridStateSnapshotRepository(session)
        grid = [
            {"side": "Buy", "price": 100.0},
            {"side": "Wait", "price": 101.0},
            {"side": "Sell", "price": 102.0},
        ]
        ts_old = datetime(2026, 1, 1, tzinfo=UTC)
        ts_new = datetime(2026, 1, 2, tzinfo=UTC)
        fp = grid_fingerprint_hash(grid, 0.5, 3)

        older = GridStateSnapshot(
            run_id=sample_run.run_id,
            account_id=sample_run.account_id,
            strat_id="strat_a",
            symbol="BTCUSDT",
            exchange_ts=ts_old,
            local_ts=ts_old,
            grid_json=grid,
            grid_step=Decimal("0.5"),
            grid_count=3,
            raw_fingerprint=fp,
        )
        newer_ts = GridStateSnapshot(
            run_id=sample_run.run_id,
            account_id=sample_run.account_id,
            strat_id="strat_a",
            symbol="BTCUSDT",
            exchange_ts=ts_new,
            local_ts=ts_new,
            grid_json=grid,
            grid_step=Decimal("0.5"),
            grid_count=3,
            raw_fingerprint=fp,
        )
        tie_low_id = GridStateSnapshot(
            run_id=sample_run.run_id,
            account_id=sample_run.account_id,
            strat_id="strat_a",
            symbol="BTCUSDT",
            exchange_ts=ts_new,
            local_ts=ts_new,
            grid_json=[{"side": "Buy", "price": 99.0}, *grid[1:]],
            grid_step=Decimal("0.5"),
            grid_count=3,
            raw_fingerprint=grid_fingerprint_hash(
                [{"side": "Buy", "price": 99.0}, *grid[1:]], 0.5, 3,
            ),
        )
        tie_high_id = GridStateSnapshot(
            run_id=sample_run.run_id,
            account_id=sample_run.account_id,
            strat_id="strat_a",
            symbol="BTCUSDT",
            exchange_ts=ts_new,
            local_ts=ts_new,
            grid_json=[{"side": "Buy", "price": 98.0}, *grid[1:]],
            grid_step=Decimal("0.5"),
            grid_count=3,
            raw_fingerprint=grid_fingerprint_hash(
                [{"side": "Buy", "price": 98.0}, *grid[1:]], 0.5, 3,
            ),
        )
        for snap in (older, newer_ts, tie_low_id, tie_high_id):
            repo.insert(snap)
        session.flush()

        latest = repo.get_latest(
            sample_run.run_id, sample_run.account_id, "strat_a",
        )
        all_rows = (
            session.query(GridStateSnapshot)
            .filter(
                GridStateSnapshot.run_id == sample_run.run_id,
                GridStateSnapshot.account_id == sample_run.account_id,
                GridStateSnapshot.strat_id == "strat_a",
            )
            .order_by(GridStateSnapshot.id)
            .all()
        )
        assert latest is not None
        assert len(all_rows) == 4
        assert latest.exchange_ts.replace(tzinfo=UTC) == ts_new
        assert latest.grid_json[0]["price"] == 98.0
        assert latest.id == all_rows[-1].id

    def test_get_at_or_before_cross_run(
        self, session, sample_user, sample_account, sample_strategy, sample_run,
    ):
        """0052: ``get_at_or_before`` ignores ``run_id`` — picks the most
        recent row in (account_id, strat_id, symbol, ts) regardless of
        which process wrote it.
        """
        repo = GridStateSnapshotRepository(session)
        other_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            status="running",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(other_run)
        session.flush()

        grid = [
            {"side": "Buy", "price": 100.0},
            {"side": "Wait", "price": 101.0},
            {"side": "Sell", "price": 102.0},
        ]
        ts_old = datetime(2026, 1, 1, tzinfo=UTC)
        ts_new = datetime(2026, 1, 2, tzinfo=UTC)
        # Older row under ``sample_run``, newer row under ``other_run``.
        repo.insert(GridStateSnapshot(
            run_id=sample_run.run_id,
            account_id=sample_run.account_id,
            strat_id="strat_a",
            symbol="BTCUSDT",
            exchange_ts=ts_old, local_ts=ts_old,
            grid_json=grid, grid_step=Decimal("0.5"), grid_count=3,
            raw_fingerprint=grid_fingerprint_hash(grid, 0.5, 3),
        ))
        newer_grid = [
            {"side": "Buy", "price": 200.0}, *grid[1:],
        ]
        repo.insert(GridStateSnapshot(
            run_id=other_run.run_id,
            account_id=sample_run.account_id,
            strat_id="strat_a",
            symbol="BTCUSDT",
            exchange_ts=ts_new, local_ts=ts_new,
            grid_json=newer_grid, grid_step=Decimal("0.5"), grid_count=3,
            raw_fingerprint=grid_fingerprint_hash(newer_grid, 0.5, 3),
        ))
        session.flush()

        picked = repo.get_at_or_before(
            sample_run.account_id, "strat_a", "BTCUSDT",
            ts_new + timedelta(hours=1),
        )
        assert picked is not None
        assert picked.grid_json[0]["price"] == 200.0
        assert picked.run_id == other_run.run_id

    def test_get_at_or_before_filters_by_symbol(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """0052 F-1-2: rows for a different ``symbol`` must NOT match,
        even when ``(account_id, strat_id)`` is identical.
        """
        repo = GridStateSnapshotRepository(session)
        ts = datetime(2026, 1, 2, tzinfo=UTC)
        # 0062: seed under a live run active at ``at_ts`` so the only reason
        # the lookup returns None is the symbol mismatch (not the run-active
        # guard excluding a wall-clock-``start_ts`` ``sample_run``).
        writer_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            status="running",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(writer_run)
        session.flush()
        grid = [
            {"side": "Buy", "price": 100.0},
            {"side": "Wait", "price": 101.0},
            {"side": "Sell", "price": 102.0},
        ]
        repo.insert(GridStateSnapshot(
            run_id=writer_run.run_id,
            account_id=writer_run.account_id,
            strat_id="strat_a",
            symbol="ETHUSDT",
            exchange_ts=ts, local_ts=ts,
            grid_json=grid, grid_step=Decimal("0.5"), grid_count=3,
            raw_fingerprint=grid_fingerprint_hash(grid, 0.5, 3),
        ))
        session.flush()

        picked = repo.get_at_or_before(
            writer_run.account_id, "strat_a", "BTCUSDT",
            ts + timedelta(hours=1),
        )
        assert picked is None

    # ----- feature 0062: run-active guard on get_at_or_before -----

    def _seed_grid_run(
        self, session, sample_user, sample_account, sample_strategy,
        *, run_type, start_ts, end_ts, exchange_ts, run_status="running",
    ):
        """Helper: create a Run + a single BTCUSDT grid snapshot under it.

        Returns ``(run, snapshot)``. Each 0062 test builds its OWN run with
        an explicit ``start_ts`` (never reuses ``sample_run``, whose
        ``start_ts`` is wall-clock now and would be excluded by the new
        ``start_ts <= at_ts`` predicate for a fixed-past ``at_ts``).
        """
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type=run_type,
            status=run_status,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        session.add(run)
        session.flush()
        grid = [
            {"side": "Buy", "price": 100.0},
            {"side": "Wait", "price": 101.0},
            {"side": "Sell", "price": 102.0},
        ]
        snap = GridStateSnapshot(
            run_id=run.run_id,
            account_id=sample_account.account_id,
            strat_id="strat_a",
            symbol="BTCUSDT",
            exchange_ts=exchange_ts, local_ts=exchange_ts,
            grid_json=grid, grid_step=Decimal("0.5"), grid_count=3,
            raw_fingerprint=grid_fingerprint_hash(grid, 0.5, 3),
        )
        GridStateSnapshotRepository(session).insert(snap)
        session.flush()
        return run, snap

    def test_get_at_or_before_excludes_run_ended_before_at_ts(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """0062 reproducer: a graceful-stopped live run whose ``end_ts`` is
        before ``at_ts`` must NOT seed replay even though its snapshot's
        ``exchange_ts <= at_ts``. (FAILS pre-fix, PASSES after.)
        """
        at_ts = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="live",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
            end_ts=datetime(2026, 1, 2, 9, 0, tzinfo=UTC),  # ended before at_ts
            exchange_ts=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        )
        picked = GridStateSnapshotRepository(session).get_at_or_before(
            sample_account.account_id, "strat_a", "BTCUSDT", at_ts,
        )
        assert picked is None

    def test_get_at_or_before_includes_active_run_null_end_ts(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """A still-running live run (``end_ts IS NULL``) with
        ``start_ts <= at_ts`` is selected.
        """
        at_ts = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        run, _ = self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="live",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
            end_ts=None,
            exchange_ts=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        )
        picked = GridStateSnapshotRepository(session).get_at_or_before(
            sample_account.account_id, "strat_a", "BTCUSDT", at_ts,
        )
        assert picked is not None
        assert picked.run_id == run.run_id

    def test_get_at_or_before_includes_active_shadow_run(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """0062: ``shadow`` is also a grid-writing run_type — an active
        shadow run's snapshot is selected (locks the ``shadow`` branch of
        the ``run_type.in_(("live","shadow"))`` predicate).
        """
        at_ts = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        run, _ = self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="shadow",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
            end_ts=None,
            exchange_ts=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        )
        picked = GridStateSnapshotRepository(session).get_at_or_before(
            sample_account.account_id, "strat_a", "BTCUSDT", at_ts,
        )
        assert picked is not None
        assert picked.run_id == run.run_id

    def test_get_at_or_before_boundary_end_ts_inclusive(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """``end_ts == at_ts`` is inclusive — the run still owns the grid
        at that instant, so its snapshot is selected.
        """
        at_ts = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        run, _ = self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="live",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
            end_ts=at_ts,
            exchange_ts=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        )
        picked = GridStateSnapshotRepository(session).get_at_or_before(
            sample_account.account_id, "strat_a", "BTCUSDT", at_ts,
        )
        assert picked is not None
        assert picked.run_id == run.run_id

    def test_get_at_or_before_boundary_start_ts_inclusive(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """``start_ts == at_ts`` (end_ts NULL) is inclusive."""
        at_ts = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        run, _ = self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="live",
            start_ts=at_ts,
            end_ts=None,
            exchange_ts=at_ts,
        )
        picked = GridStateSnapshotRepository(session).get_at_or_before(
            sample_account.account_id, "strat_a", "BTCUSDT", at_ts,
        )
        assert picked is not None
        assert picked.run_id == run.run_id

    def test_get_at_or_before_excludes_recording_run(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """A ``recording`` run active at ``at_ts`` is excluded by the
        ``run_type`` guard (the recorder never writes grid snapshots).
        """
        at_ts = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="recording",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
            end_ts=None,
            exchange_ts=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        )
        picked = GridStateSnapshotRepository(session).get_at_or_before(
            sample_account.account_id, "strat_a", "BTCUSDT", at_ts,
        )
        assert picked is None

    def test_get_at_or_before_two_active_runs_latest_wins(
        self, session, sample_user, sample_account, sample_strategy,
    ):
        """Two live runs both active at ``at_ts`` with snapshots at
        different ``exchange_ts`` — the later snapshot wins (``ORDER BY``
        survives the JOIN).
        """
        at_ts = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)
        self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="live",
            start_ts=datetime(2026, 1, 1, tzinfo=UTC),
            end_ts=None,
            exchange_ts=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        )
        newer_run, _ = self._seed_grid_run(
            session, sample_user, sample_account, sample_strategy,
            run_type="live",
            start_ts=datetime(2026, 1, 2, tzinfo=UTC),
            end_ts=None,
            exchange_ts=datetime(2026, 1, 3, 8, 0, tzinfo=UTC),
        )
        picked = GridStateSnapshotRepository(session).get_at_or_before(
            sample_account.account_id, "strat_a", "BTCUSDT", at_ts,
        )
        assert picked is not None
        assert picked.run_id == newer_run.run_id
