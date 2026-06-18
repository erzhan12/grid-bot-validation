"""Base repository with multi-tenant filtering (split from repositories.py, feature 0081 / issue #184)."""

from typing import Generic, TypeVar

from sqlalchemy.orm import Session

from grid_db.models import Base


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


