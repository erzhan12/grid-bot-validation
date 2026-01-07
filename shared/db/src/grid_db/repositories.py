"""Repository pattern for database operations with multi-tenant filtering."""

from typing import Generic, TypeVar, Optional, List

from sqlalchemy.orm import Session

from grid_db.models import (
    Base,
    User,
    BybitAccount,
    ApiCredential,
    Strategy,
    Run,
)


T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """Base repository with common CRUD operations.

    Usage:
        repo = BaseRepository(session, User)
        user = repo.get_by_id("uuid-here")
        all_users = repo.get_all(limit=10)
    """

    def __init__(self, session: Session, model_class: type[T]):
        """Initialize repository with session and model class.

        Args:
            session: SQLAlchemy session instance.
            model_class: The ORM model class to operate on.
        """
        self.session = session
        self.model_class = model_class

    def create(self, entity: T) -> T:
        """Create new entity.

        Args:
            entity: Entity instance to insert.

        Returns:
            The created entity with generated fields populated.
        """
        self.session.add(entity)
        self.session.flush()
        return entity

    def update(self, entity: T) -> T:
        """Update existing entity.

        Args:
            entity: Entity instance to update.

        Returns:
            The updated entity.
        """
        merged = self.session.merge(entity)
        self.session.flush()
        return merged

    def delete(self, entity: T) -> None:
        """Delete entity.

        Args:
            entity: Entity instance to delete.
        """
        self.session.delete(entity)
        self.session.flush()


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
