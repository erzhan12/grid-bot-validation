"""Tests for DatabaseFactory and database connection management."""

import pytest
from sqlalchemy import text

from grid_db.database import DatabaseFactory, get_db, init_db
from grid_db.settings import DatabaseSettings
from grid_db.models import User


class TestDatabaseSettings:
    """Tests for DatabaseSettings configuration."""

    def test_default_sqlite_url(self):
        """Default settings produce SQLite URL."""
        settings = DatabaseSettings()
        url = settings.get_database_url()
        assert url.startswith("sqlite+pysqlite:///")

    def test_sqlite_with_custom_name(self):
        """SQLite with custom database name."""
        settings = DatabaseSettings(db_type="sqlite", db_name="custom.db")
        url = settings.get_database_url()
        assert url == "sqlite+pysqlite:///custom.db"

    def test_sqlite_memory(self):
        """SQLite in-memory database."""
        settings = DatabaseSettings(db_type="sqlite", db_name=":memory:")
        url = settings.get_database_url()
        assert url == "sqlite+pysqlite:///:memory:"

    def test_postgresql_url(self):
        """PostgreSQL URL generation."""
        settings = DatabaseSettings(
            db_type="postgresql",
            db_host="localhost",
            db_port="5432",
            db_user="gridbot",
            db_password="secret",
            db_name="gridbot_db",
        )
        url = settings.get_database_url()
        assert url == "postgresql+psycopg2://gridbot:secret@localhost:5432/gridbot_db"

    def test_postgresql_url_with_special_characters(self):
        """PostgreSQL URL generation with special characters in password."""
        from urllib.parse import quote_plus
        
        settings = DatabaseSettings(
            db_type="postgresql",
            db_host="localhost",
            db_port="5432",
            db_user="gridbot",
            db_password="p@ss:w#rd/123%",
            db_name="gridbot_db",
        )
        url = settings.get_database_url()
        # Password should be URL-encoded
        expected_password = quote_plus("p@ss:w#rd/123%")
        assert f":{expected_password}@" in url
        assert url.startswith("postgresql+psycopg2://")
        assert url.endswith("/gridbot_db")
        # Verify the password is properly encoded (not raw special chars)
        assert "@" not in url.split("@")[0].split(":")[-1]  # Password part before @
        assert ":" not in url.split("@")[0].split(":")[-1]  # Password part before @
        assert "/" not in url.split("@")[0].split(":")[-1]  # Password part before @

    def test_postgresql_url_with_special_characters_in_all_fields(self):
        """PostgreSQL URL generation with special characters in all fields."""
        from urllib.parse import quote_plus
        
        settings = DatabaseSettings(
            db_type="postgresql",
            db_host="host@example.com",
            db_port="5432",
            db_user="user@domain",
            db_password="p@ss#w:rd",
            db_name="db-name_test",
        )
        url = settings.get_database_url()
        # All components should be URL-encoded
        assert quote_plus("user@domain") in url
        assert quote_plus("p@ss#w:rd") in url
        assert quote_plus("host@example.com") in url
        assert quote_plus("db-name_test") in url
        # Verify URL structure is correct
        assert url.startswith("postgresql+psycopg2://")
        assert "/" in url  # Should have database name separator

    def test_postgresql_missing_fields(self):
        """PostgreSQL requires all connection fields."""
        settings = DatabaseSettings(
            db_type="postgresql",
            db_host="localhost",
            # Missing port, user, password
        )
        with pytest.raises(ValueError, match="PostgreSQL requires"):
            settings.get_database_url()

    def test_direct_url_override(self):
        """Direct database_url overrides other settings."""
        settings = DatabaseSettings(
            database_url="sqlite:///override.db",
            db_type="postgresql",  # Should be ignored
        )
        url = settings.get_database_url()
        assert url == "sqlite:///override.db"

    def test_unsupported_db_type(self):
        """Unsupported database type raises error."""
        settings = DatabaseSettings(db_type="mysql")
        with pytest.raises(ValueError, match="Unsupported database type"):
            settings.get_database_url()

    def test_env_var_loading(self, monkeypatch):
        """Environment variables are loaded with GRIDBOT_ prefix."""
        monkeypatch.setenv("GRIDBOT_DB_TYPE", "postgresql")
        monkeypatch.setenv("GRIDBOT_DB_HOST", "test-host")
        monkeypatch.setenv("GRIDBOT_DB_USER", "test-user")
        
        settings = DatabaseSettings()
        assert settings.db_type == "postgresql"
        assert settings.db_host == "test-host"
        assert settings.db_user == "test-user"


class TestDatabaseFactory:
    """Tests for DatabaseFactory."""

    def test_sqlite_connection(self, db):
        """SQLite database connects successfully."""
        with db.engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar() == 1

    def test_create_tables(self, db_settings):
        """Tables are created correctly."""
        database = DatabaseFactory(db_settings)
        database.create_tables()

        with database.engine.connect() as conn:
            # Check users table exists
            result = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
                )
            )
            assert result.scalar() == "users"

    def test_all_tables_created(self, db_settings):
        """All 7 tables are created."""
        database = DatabaseFactory(db_settings)
        database.create_tables()

        expected_tables = {
            "users",
            "bybit_accounts",
            "api_credentials",
            "strategies",
            "runs",
            "public_trades",
            "private_executions",
        }

        with database.engine.connect() as conn:
            result = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
            tables = {row[0] for row in result}

        assert expected_tables.issubset(tables)

    def test_drop_tables(self, db_settings):
        """Tables can be dropped."""
        database = DatabaseFactory(db_settings)
        database.create_tables()
        database.drop_tables()

        with database.engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
                )
            )
            assert result.scalar() is None

    def test_lazy_engine_creation(self, db_settings):
        """Engine is created lazily on first access."""
        database = DatabaseFactory(db_settings)
        assert database._engine is None

        _ = database.engine  # Access engine
        assert database._engine is not None

    def test_lazy_session_factory(self, db_settings):
        """Session factory is created lazily."""
        database = DatabaseFactory(db_settings)
        database.create_tables()
        assert database._session_factory is None

        _ = database.session_factory  # Access session factory
        assert database._session_factory is not None


class TestSessionContextManager:
    """Tests for session context manager."""

    def test_session_commits_on_success(self, db):
        """Session context manager commits on success."""
        with db.get_session() as session:
            user = User(username="commit_test")
            session.add(user)

        # Verify committed in new session
        with db.get_session() as session:
            found = session.query(User).filter(User.username == "commit_test").first()
            assert found is not None

    def test_session_rollback_on_error(self, db):
        """Session context manager rolls back on error."""
        try:
            with db.get_session() as session:
                user = User(username="rollback_test")
                session.add(user)
                session.flush()
                raise ValueError("Test error")
        except ValueError:
            pass

        # Verify rolled back
        with db.get_session() as session:
            found = (
                session.query(User).filter(User.username == "rollback_test").first()
            )
            assert found is None

    def test_session_closes_on_exit(self, db):
        """Session is closed (cleared) after context manager exits."""
        with db.get_session() as session:
            user = User(username="close_test")
            session.add(user)
            session.flush()
            assert user in session
            session_ref = session

        # session.close() clears the identity map
        # Note: The object might still be associated but the session state is reset.
        # Checking identity_map is a good proxy for close() being called.
        assert len(session_ref.identity_map) == 0

    def test_nested_operations(self, db):
        """Multiple operations in single session."""
        with db.get_session() as session:
            user1 = User(username="user1")
            user2 = User(username="user2")
            session.add(user1)
            session.add(user2)

        with db.get_session() as session:
            users = session.query(User).all()
            assert len(users) == 2


class TestModuleFunctions:
    """Tests for module-level convenience functions."""

    def test_get_db_singleton(self, db_settings):
        """get_db returns singleton instance."""
        # Reset singleton
        import grid_db.database as db_module

        db_module._db = None

        db1 = get_db()
        db2 = get_db()
        assert db1 is db2

    def test_init_db_replaces_singleton(self, db_settings):
        """init_db creates new factory."""
        import grid_db.database as db_module

        db_module._db = None

        db1 = get_db()
        db2 = init_db(db_settings)

        assert db1 is not db2
        assert get_db() is db2


class TestForeignKeyEnforcement:
    """Tests for SQLite foreign key enforcement."""

    def test_foreign_keys_enabled(self, db):
        """Foreign keys are enforced in SQLite."""
        from grid_db.models import BybitAccount

        db.create_tables()

        with pytest.raises(Exception):  # IntegrityError
            with db.get_session() as session:
                # Try to create account with non-existent user
                account = BybitAccount(
                    user_id="non-existent-user-id",
                    account_name="test",
                    environment="testnet",
                )
                session.add(account)
                session.flush()
