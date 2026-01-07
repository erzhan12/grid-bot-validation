"""Database connection factory supporting SQLite and PostgreSQL."""

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool, StaticPool

from grid_db.settings import DatabaseSettings
from grid_db.models import Base


class DatabaseFactory:
    """Factory for creating database connections supporting SQLite and PostgreSQL.

    Usage:
        # With default settings (SQLite)
        db = DatabaseFactory()
        db.create_tables()

        with db.get_session() as session:
            user = User(username="test")
            session.add(user)
            # Commits automatically on success

        # With custom settings
        settings = DatabaseSettings(db_type="postgresql", ...)
        db = DatabaseFactory(settings)
    """

    def __init__(self, settings: Optional[DatabaseSettings] = None):
        """Initialize factory with database settings.

        Args:
            settings: Database configuration. Uses defaults if not provided.
        """
        self.settings = settings or DatabaseSettings()
        self._engine: Optional[Engine] = None
        self._session_factory: Optional[sessionmaker] = None

    @property
    def engine(self) -> Engine:
        """Lazy-load SQLAlchemy engine."""
        if self._engine is None:
            self._engine = self._create_engine()
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        """Lazy-load session factory."""
        if self._session_factory is None:
            self._session_factory = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=self.engine,
            )
        return self._session_factory

    def _create_engine(self) -> Engine:
        """Create SQLAlchemy engine with appropriate settings for database type."""
        url = self.settings.get_database_url()

        if url.startswith("sqlite"):
            # SQLite specific settings
            connect_args = {"check_same_thread": False}
            poolclass = None

            # Use StaticPool only for in-memory databases
            if ":memory:" in url:
                poolclass = StaticPool
            
            kwargs = {
                "echo": self.settings.echo_sql,
                "connect_args": connect_args,
            }
            if poolclass:
                kwargs["poolclass"] = poolclass

            engine = create_engine(url, **kwargs)

            # Enable foreign keys for SQLite (disabled by default)
            @event.listens_for(engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        else:
            # PostgreSQL settings with connection pooling
            engine = create_engine(
                url,
                echo=self.settings.echo_sql,
                poolclass=QueuePool,
                pool_size=self.settings.pool_size,
                max_overflow=self.settings.max_overflow,
                pool_timeout=self.settings.pool_timeout,
                pool_recycle=self.settings.pool_recycle,
            )

        return engine

    def create_tables(self) -> None:
        """Create all tables defined in models."""
        Base.metadata.create_all(bind=self.engine)

    def drop_tables(self) -> None:
        """Drop all tables. Use with caution."""
        Base.metadata.drop_all(bind=self.engine)

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """Context manager for database sessions.

        Automatically commits on success, rolls back on exception.

        Usage:
            with db.get_session() as session:
                user = User(username="test")
                session.add(user)
                # Commits automatically

        Yields:
            SQLAlchemy Session instance.
        """
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# Module-level singleton for convenience
_db: Optional[DatabaseFactory] = None


def get_db() -> DatabaseFactory:
    """Get or create the singleton database factory.

    Returns:
        DatabaseFactory instance with default settings.
    """
    global _db
    if _db is None:
        _db = DatabaseFactory()
    return _db


def init_db(settings: Optional[DatabaseSettings] = None) -> DatabaseFactory:
    """Initialize database with custom settings.

    Args:
        settings: Database configuration. Uses defaults if not provided.

    Returns:
        Configured DatabaseFactory instance.
    """
    global _db
    _db = DatabaseFactory(settings)
    return _db
