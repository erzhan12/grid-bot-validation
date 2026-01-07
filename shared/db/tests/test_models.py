"""Tests for SQLAlchemy ORM models."""

import pytest
from sqlalchemy.exc import IntegrityError

from grid_db.models import (
    User,
    BybitAccount,
    ApiCredential,
    Strategy,
    Run,
    PublicTrade,
    PrivateExecution,
)
from datetime import datetime, UTC
from decimal import Decimal


class TestUserModel:
    """Tests for User model."""

    def test_user_creation(self, session):
        """User can be created with required fields."""
        user = User(username="newuser", email="new@example.com")
        session.add(user)
        session.flush()

        assert user.user_id is not None
        assert len(user.user_id) == 36  # UUID format
        assert user.status == "active"
        assert user.created_at is not None
        assert user.updated_at is not None

    def test_user_unique_username(self, session, sample_user):
        """Username must be unique."""
        duplicate = User(username=sample_user.username)
        session.add(duplicate)

        with pytest.raises(IntegrityError):
            session.flush()

    def test_user_unique_email(self, session, sample_user):
        """Email must be unique."""
        duplicate = User(username="another", email=sample_user.email)
        session.add(duplicate)

        with pytest.raises(IntegrityError):
            session.flush()

    def test_user_email_optional(self, session):
        """Email is optional."""
        user = User(username="noemail")
        session.add(user)
        session.flush()

        assert user.email is None


class TestBybitAccountModel:
    """Tests for BybitAccount model."""

    def test_account_creation(self, session, sample_user):
        """Account can be created with required fields."""
        account = BybitAccount(
            user_id=sample_user.user_id,
            account_name="main_account",
            environment="mainnet",
        )
        session.add(account)
        session.flush()

        assert account.account_id is not None
        assert account.status == "enabled"
        assert account.created_at is not None

    def test_account_user_relationship(self, session, sample_user, sample_account):
        """Account has relationship to user."""
        assert sample_account.user == sample_user
        assert sample_account in sample_user.accounts

    def test_account_cascade_delete(self, session, sample_user, sample_account):
        """Deleting user cascades to accounts."""
        account_id = sample_account.account_id

        session.delete(sample_user)
        session.flush()

        assert session.get(BybitAccount, account_id) is None

    def test_account_unique_per_user(self, session, sample_user, sample_account):
        """Account name must be unique per user."""
        duplicate = BybitAccount(
            user_id=sample_user.user_id,
            account_name=sample_account.account_name,
            environment="testnet",
        )
        session.add(duplicate)

        with pytest.raises(IntegrityError):
            session.flush()

    def test_account_same_name_different_user(self, session, sample_account):
        """Same account name allowed for different users."""
        other_user = User(username="other_user")
        session.add(other_user)
        session.flush()

        account = BybitAccount(
            user_id=other_user.user_id,
            account_name=sample_account.account_name,  # Same name
            environment="testnet",
        )
        session.add(account)
        session.flush()

        assert account.account_id is not None


class TestApiCredentialModel:
    """Tests for ApiCredential model."""

    def test_credential_creation(self, session, sample_account):
        """Credential can be created with required fields."""
        credential = ApiCredential(
            account_id=sample_account.account_id,
            api_key_id="my_api_key",
            api_secret="my_secret",
        )
        session.add(credential)
        session.flush()

        assert credential.credential_id is not None
        assert credential.is_active is True
        assert credential.rotated_at is None

    def test_credential_cascade_delete(self, session, sample_account, sample_credential):
        """Deleting account cascades to credentials."""
        credential_id = sample_credential.credential_id

        session.delete(sample_account)
        session.flush()

        assert session.get(ApiCredential, credential_id) is None


class TestStrategyModel:
    """Tests for Strategy model."""

    def test_strategy_creation(self, session, sample_account):
        """Strategy can be created with required fields."""
        strategy = Strategy(
            account_id=sample_account.account_id,
            strategy_type="GridStrategy",
            symbol="ETHUSDT",
            config_json={"grid_count": 30, "grid_step": 0.3},
        )
        session.add(strategy)
        session.flush()

        assert strategy.strategy_id is not None
        assert strategy.is_enabled is True

    def test_strategy_config_json(self, session, sample_account):
        """Strategy stores and retrieves JSON config."""
        config = {"grid_count": 50, "grid_step": 0.2, "nested": {"key": "value"}}
        strategy = Strategy(
            account_id=sample_account.account_id,
            strategy_type="GridStrategy",
            symbol="BTCUSDT",
            config_json=config,
        )
        session.add(strategy)
        session.flush()

        # Reload from database
        session.expire(strategy)
        assert strategy.config_json["grid_count"] == 50
        assert strategy.config_json["nested"]["key"] == "value"

    def test_strategy_unique_per_account_symbol(self, session, sample_strategy):
        """Strategy symbol must be unique per account."""
        duplicate = Strategy(
            account_id=sample_strategy.account_id,
            strategy_type="GridStrategy",
            symbol=sample_strategy.symbol,
            config_json={},
        )
        session.add(duplicate)

        with pytest.raises(IntegrityError):
            session.flush()

    def test_strategy_cascade_delete(self, session, sample_account, sample_strategy):
        """Deleting account cascades to strategies."""
        strategy_id = sample_strategy.strategy_id

        session.delete(sample_account)
        session.flush()

        assert session.get(Strategy, strategy_id) is None


class TestRunModel:
    """Tests for Run model."""

    def test_run_creation(self, session, sample_user, sample_account, sample_strategy):
        """Run can be created with required fields."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="backtest",
        )
        session.add(run)
        session.flush()

        assert run.run_id is not None
        assert run.status == "running"
        assert run.start_ts is not None
        assert run.end_ts is None

    def test_run_with_config_snapshot(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Run can store config snapshot."""
        config = {"grid_count": 50, "grid_step": 0.2}
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
            gridcore_version="0.1.0",
            config_snapshot=config,
        )
        session.add(run)
        session.flush()

        session.expire(run)
        assert run.config_snapshot["grid_count"] == 50
        assert run.gridcore_version == "0.1.0"

    def test_run_relationships(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Run has relationships to user, account, strategy."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="shadow",
        )
        session.add(run)
        session.flush()

        assert run.user == sample_user
        assert run.account == sample_account
        assert run.strategy == sample_strategy

    def test_run_cascade_delete_from_user(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Deleting user cascades to runs."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="backtest",
        )
        session.add(run)
        session.flush()
        run_id = run.run_id

        session.delete(sample_user)
        session.flush()

        assert session.get(Run, run_id) is None

    def test_run_cascade_delete_from_account(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Deleting account cascades to runs."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
        )
        session.add(run)
        session.flush()
        run_id = run.run_id

        session.delete(sample_account)
        session.flush()

        assert session.get(Run, run_id) is None

    def test_run_cascade_delete_from_strategy(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Deleting strategy cascades to runs."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="shadow",
        )
        session.add(run)
        session.flush()
        run_id = run.run_id

        session.delete(sample_strategy)
        session.flush()

        assert session.get(Run, run_id) is None


class TestPublicTradeModel:
    """Tests for PublicTrade model."""

    def test_public_trade_creation(self, session):
        """PublicTrade can be created with required fields."""
        now = datetime.now(UTC)
        trade = PublicTrade(
            symbol="BTCUSDT",
            trade_id="12345",
            exchange_ts=now,
            local_ts=now,
            side="Buy",
            price=Decimal("50000.50"),
            size=Decimal("0.001"),
        )
        session.add(trade)
        session.flush()

        assert trade.id is not None
        assert trade.price == Decimal("50000.50")

    def test_public_trade_decimal_precision(self, session):
        """PublicTrade preserves decimal precision."""
        now = datetime.now(UTC)
        trade = PublicTrade(
            symbol="BTCUSDT",
            trade_id="12346",
            exchange_ts=now,
            local_ts=now,
            side="Sell",
            price=Decimal("50000.12345678"),
            size=Decimal("0.00000001"),
        )
        session.add(trade)
        session.flush()

        session.expire(trade)
        assert trade.price == Decimal("50000.12345678")
        assert trade.size == Decimal("0.00000001")


class TestPrivateExecutionModel:
    """Tests for PrivateExecution model."""

    def test_private_execution_creation(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """PrivateExecution can be created with required fields."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
        )
        session.add(run)
        session.flush()

        now = datetime.now(UTC)
        execution = PrivateExecution(
            run_id=run.run_id,
            account_id=sample_account.account_id,
            symbol="BTCUSDT",
            exec_id="exec123",
            order_id="order456",
            order_link_id="link789",
            exchange_ts=now,
            side="Buy",
            exec_price=Decimal("50000.00"),
            exec_qty=Decimal("0.01"),
            exec_fee=Decimal("0.50"),
            closed_pnl=Decimal("10.00"),
        )
        session.add(execution)
        session.flush()

        assert execution.id is not None
        assert execution.run == run

    def test_private_execution_raw_json(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """PrivateExecution can store raw JSON response."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
        )
        session.add(run)
        session.flush()

        now = datetime.now(UTC)
        raw_data = {"execType": "Trade", "isMaker": True, "extra": {"field": "value"}}
        execution = PrivateExecution(
            run_id=run.run_id,
            account_id=sample_account.account_id,
            symbol="BTCUSDT",
            exec_id="exec124",
            order_id="order457",
            exchange_ts=now,
            side="Sell",
            exec_price=Decimal("51000.00"),
            exec_qty=Decimal("0.02"),
            raw_json=raw_data,
        )
        session.add(execution)
        session.flush()

        session.expire(execution)
        assert execution.raw_json["isMaker"] is True
        assert execution.raw_json["extra"]["field"] == "value"

    def test_private_execution_cascade_delete_from_run(
        self, session, sample_user, sample_account, sample_strategy
    ):
        """Deleting run cascades to private executions."""
        run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="live",
        )
        session.add(run)
        session.flush()

        now = datetime.now(UTC)
        execution = PrivateExecution(
            run_id=run.run_id,
            account_id=sample_account.account_id,
            symbol="BTCUSDT",
            exec_id="exec125",
            order_id="order458",
            exchange_ts=now,
            side="Buy",
            exec_price=Decimal("50000.00"),
            exec_qty=Decimal("0.01"),
        )
        session.add(execution)
        session.flush()
        execution_id = execution.id

        session.delete(run)
        session.flush()

        assert session.get(PrivateExecution, execution_id) is None
