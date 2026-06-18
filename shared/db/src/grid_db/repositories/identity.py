"""Identity and account repositories (split from repositories.py, feature 0081 / issue #184)."""

from datetime import datetime
from typing import Optional, List

from sqlalchemy.orm import Session

from grid_db.models import (
    User, BybitAccount, ApiCredential, Strategy, Run,
)
from grid_db.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """Repository for User operations."""

    def __init__(self, session: Session):
        super().__init__(session, User)

    def get_by_id(self, id: str) -> Optional[User]:
        """Get user by ID.

        Args:
            id: User ID.

        Returns:
            User instance or None.
        """
        return self.session.get(User, id)

    def get_all(self, limit: int = 100, offset: int = 0) -> List[User]:
        """Get all users.

        Args:
            limit: Max users.
            offset: Offset.

        Returns:
            List of users.
        """
        return self.session.query(User).limit(limit).offset(offset).all()

    def get_by_username(self, username: str) -> Optional[User]:
        """Find user by username.

        Args:
            username: The username to search for.

        Returns:
            User instance or None if not found.
        """
        return (
            self.session.query(User)
            .filter(User.username == username)
            .first()
        )

    def get_active_users(self) -> List[User]:
        """Get all active users.

        Returns:
            List of users with status='active'.
        """
        return (
            self.session.query(User)
            .filter(User.status == "active")
            .all()
        )


class BybitAccountRepository(BaseRepository[BybitAccount]):
    """Repository for BybitAccount operations."""

    def __init__(self, session: Session):
        super().__init__(session, BybitAccount)

    def get_by_user_id(self, user_id: str) -> List[BybitAccount]:
        """Get all accounts for a user.

        Args:
            user_id: The user's ID.

        Returns:
            List of BybitAccount instances.
        """
        return (
            self.session.query(BybitAccount)
            .filter(BybitAccount.user_id == user_id)
            .all()
        )

    def get_enabled_accounts(self, user_id: str) -> List[BybitAccount]:
        """Get all enabled accounts for a user.

        Args:
            user_id: The user's ID.

        Returns:
            List of enabled BybitAccount instances.
        """
        return (
            self.session.query(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                BybitAccount.status == "enabled",
            )
            .all()
        )


class ApiCredentialRepository(BaseRepository[ApiCredential]):
    """Repository for ApiCredential operations."""

    def __init__(self, session: Session):
        super().__init__(session, ApiCredential)

    def get_by_account_id(self, user_id: str, account_id: str) -> List[ApiCredential]:
        """Get all credentials for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            List of ApiCredential instances owned by the user.
        """
        return (
            self.session.query(ApiCredential)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                ApiCredential.account_id == account_id,
            )
            .all()
        )

    def get_active_credential(self, user_id: str, account_id: str) -> Optional[ApiCredential]:
        """Get the active credential for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            Active ApiCredential or None.
        """
        return (
            self.session.query(ApiCredential)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                ApiCredential.account_id == account_id,
                ApiCredential.is_active.is_(True),
            )
            .first()
        )


class StrategyRepository(BaseRepository[Strategy]):
    """Repository for Strategy operations."""

    def __init__(self, session: Session):
        super().__init__(session, Strategy)

    def get_by_account_id(self, user_id: str, account_id: str) -> List[Strategy]:
        """Get all strategies for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            List of Strategy instances owned by the user.
        """
        return (
            self.session.query(Strategy)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                Strategy.account_id == account_id,
            )
            .all()
        )

    def get_enabled_strategies(self, user_id: str, account_id: str) -> List[Strategy]:
        """Get enabled strategies for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            List of enabled Strategy instances owned by the user.
        """
        return (
            self.session.query(Strategy)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                Strategy.account_id == account_id,
                Strategy.is_enabled.is_(True),
            )
            .all()
        )

    def get_by_symbol(self, user_id: str, account_id: str, symbol: str) -> Optional[Strategy]:
        """Get strategy for a specific symbol with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.
            symbol: Trading symbol (e.g., 'BTCUSDT').

        Returns:
            Strategy instance or None.
        """
        return (
            self.session.query(Strategy)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                Strategy.account_id == account_id,
                Strategy.symbol == symbol,
            )
            .first()
        )


class RunRepository(BaseRepository[Run]):
    """Repository for Run operations with multi-tenant filtering.

    CRITICAL: All queries filter by user_id to enforce data isolation.
    """

    def __init__(self, session: Session):
        super().__init__(session, Run)

    def get_by_user_id(self, user_id: str, limit: int = 100) -> List[Run]:
        """Get runs for a user (multi-tenant access control).

        Args:
            user_id: The user's ID for access filtering.
            limit: Maximum number of runs to return.

        Returns:
            List of Run instances owned by the user.
        """
        return (
            self.session.query(Run)
            .filter(Run.user_id == user_id)
            .order_by(Run.start_ts.desc())
            .limit(limit)
            .all()
        )

    def get_running(self, user_id: str) -> List[Run]:
        """Get currently running runs for a user.

        Args:
            user_id: The user's ID for access filtering.

        Returns:
            List of runs with status='running'.
        """
        return (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.status == "running",
            )
            .all()
        )

    def get_by_account_id(self, user_id: str, account_id: str) -> List[Run]:
        """Get runs for a specific account (with user filtering).

        Args:
            user_id: The user's ID for access filtering.
            account_id: The account's ID.

        Returns:
            List of Run instances for the account.
        """
        return (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.account_id == account_id,
            )
            .order_by(Run.start_ts.desc())
            .all()
        )

    def get_by_run_type(self, user_id: str, run_type: str) -> List[Run]:
        """Get runs by type for a user.

        Args:
            user_id: The user's ID for access filtering.
            run_type: Run type ('live', 'backtest', 'shadow').

        Returns:
            List of Run instances of the specified type.
        """
        return (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.run_type == run_type,
            )
            .order_by(Run.start_ts.desc())
            .all()
        )

    def get_latest_by_type(
        self,
        run_type: str,
        statuses: tuple[str, ...] = ("completed", "running"),
    ) -> Optional[Run]:
        """Get the most recent run of a given type.

        Unlike other methods, does NOT filter by user_id — intended for
        standalone tools (e.g. replay engine) that need to auto-discover
        the latest recording run regardless of user.

        Example::

            repo = RunRepository(session)
            run = repo.get_latest_by_type("recording")
            if run is None:
                raise ValueError("No recording runs found")
            run_id, start_ts, end_ts = run.run_id, run.start_ts, run.end_ts

        Args:
            run_type: Run type (e.g. 'recording', 'live', 'backtest').
            statuses: Tuple of acceptable statuses to filter by.
                Defaults to ("completed", "running") to skip failed runs.

        Returns:
            Most recent Run of that type, or None if none exist.
        """
        query = (
            self.session.query(Run)
            .filter(Run.run_type == run_type)
        )
        if statuses:
            query = query.filter(Run.status.in_(statuses))
        return query.order_by(Run.start_ts.desc()).first()

    def close_stale_running_runs(
        self,
        user_id: str,
        account_id: str,
        strategy_id: str,
        run_type: str,
        *,
        end_ts: datetime,
    ) -> int:
        """Mark orphaned open runs completed before a new run starts (0062 / #148).

        After crash/kill the prior process may leave ``status='running'`` and
        ``end_ts=NULL``. Without closing those rows, replay's cross-run grid
        seed lookup can still treat the stale run as active at ``at_ts``.
        """
        runs = (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.account_id == account_id,
                Run.strategy_id == strategy_id,
                Run.run_type == run_type,
                Run.status == "running",
                Run.end_ts.is_(None),
            )
            .all()
        )
        for run in runs:
            run.status = "completed"
            run.end_ts = end_ts
        return len(runs)


