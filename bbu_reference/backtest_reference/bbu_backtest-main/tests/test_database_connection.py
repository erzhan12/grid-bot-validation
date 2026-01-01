"""
Unit tests to verify database connectivity using the configured SQLAlchemy engine.

These tests will attempt a real connection using the URL provided via your
environment (.env) and Settings. Ensure your database is reachable before running.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError


def test_engine_can_connect_and_select_one():
    """Engine connects and executes a trivial statement."""
    # Import after environment is loaded to ensure Settings picks up .env
    from db.database import engine

    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            scalar = result.scalar()
            assert scalar == 1, "Expected SELECT 1 to return 1"
    except OperationalError as e:
        pytest.fail(f"Database is not reachable or URL is invalid: {e}")
    except SQLAlchemyError as e:
        pytest.fail(f"SQLAlchemy error during connection: {e}")


def test_session_can_execute_simple_statement():
    """Session factory produces a working session that can execute SQL."""
    from db.database import SessionLocal

    session = SessionLocal()
    try:
        # Simple no-op statement to validate the session/connection
        session.execute(text("SELECT 1"))
    except OperationalError as e:
        pytest.fail(f"Database is not reachable or URL is invalid: {e}")
    except SQLAlchemyError as e:
        pytest.fail(f"SQLAlchemy error during session execution: {e}")
    finally:
        session.close()


