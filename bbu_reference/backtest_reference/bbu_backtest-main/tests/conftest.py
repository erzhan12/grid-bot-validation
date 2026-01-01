"""Pytest configuration and fixtures for database tests.

Sets safe defaults so tests can run without a real DB by using an in-memory
SQLite URL via the DATABASE_URL environment variable. Also ensures the project
root is on sys.path so imports like `config.settings` and `db.database` work.
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session", autouse=True)
def ensure_settings_loadable():
    """Ensure Settings can load from environment without raising.

    Configure a default in-memory SQLite URL for tests if DATABASE_URL isn't set.
    """
    # Provide safe defaults for testing
    os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    os.environ.setdefault("ECHO_SQL", "false")

    try:
        from config.settings import DatabaseSettings

        _ = DatabaseSettings()  # instantiate once
    except Exception as exc:
        pytest.fail(
            "Failed to instantiate DatabaseSettings. Check your .env and required variables.\n"
            f"Error: {exc}"
        )


