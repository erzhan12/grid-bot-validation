"""Tests for the feature 0042 wallet schema migration.

Covers idempotency, empty-DB safety, and data preservation against a
SQLite engine. Cross-dialect (PostgreSQL) coverage requires a live
server and is skipped by intent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import NoSuchTableError


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "migrate_0042_uta_wallet_fields.py"
)

_NEW_COLUMNS = (
    "total_equity",
    "total_available_balance",
    "total_margin_balance",
    "account_im_rate",
    "account_mm_rate",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migrate_0042_uta_wallet_fields", _MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_wallet_table(database_url: str) -> None:
    """Build a pre-0042 wallet_snapshots table (no 0042 columns)."""
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE wallet_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id VARCHAR(36),
                    account_id VARCHAR(36) NOT NULL,
                    exchange_ts DATETIME NOT NULL,
                    local_ts DATETIME NOT NULL,
                    coin VARCHAR(20) NOT NULL,
                    wallet_balance NUMERIC(20, 8) NOT NULL,
                    available_balance NUMERIC(20, 8) NOT NULL,
                    raw_json JSON
                )
                """
            )
        )
    engine.dispose()


def _column_names(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        return {c["name"] for c in inspector.get_columns("wallet_snapshots")}
    finally:
        engine.dispose()


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'wallet.db'}"


class TestMigrate0042:
    """Migration `scripts/migrate_0042_uta_wallet_fields.py`."""

    def test_adds_columns_on_legacy_schema(self, sqlite_url: str) -> None:
        """Migration adds all five 0042 columns to a pre-0042 table."""
        _create_legacy_wallet_table(sqlite_url)
        before = _column_names(sqlite_url)
        assert before.isdisjoint(_NEW_COLUMNS)

        _load_migration().migrate(sqlite_url)

        after = _column_names(sqlite_url)
        for col in _NEW_COLUMNS:
            assert col in after, f"missing column: {col}"

    def test_is_idempotent(self, sqlite_url: str) -> None:
        """Running migration twice is a no-op on the second pass."""
        _create_legacy_wallet_table(sqlite_url)
        module = _load_migration()
        module.migrate(sqlite_url)
        module.migrate(sqlite_url)  # must not raise

        cols = _column_names(sqlite_url)
        for col in _NEW_COLUMNS:
            assert col in cols

    def test_preserves_existing_rows(self, sqlite_url: str) -> None:
        """Existing wallet_snapshots data survives the ALTER."""
        _create_legacy_wallet_table(sqlite_url)
        engine = create_engine(sqlite_url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO wallet_snapshots
                        (account_id, exchange_ts, local_ts, coin,
                         wallet_balance, available_balance)
                    VALUES
                        ('acct-1', '2026-05-01 00:00:00', '2026-05-01 00:00:01',
                         'USDT', 123.45, 100.0)
                    """
                )
            )
        engine.dispose()

        _load_migration().migrate(sqlite_url)

        engine = create_engine(sqlite_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT account_id, coin, wallet_balance, "
                        "total_available_balance FROM wallet_snapshots"
                    )
                ).all()
        finally:
            engine.dispose()

        assert len(rows) == 1
        account_id, coin, wallet_balance, total_available_balance = rows[0]
        assert account_id == "acct-1"
        assert coin == "USDT"
        assert float(wallet_balance) == pytest.approx(123.45)
        assert total_available_balance is None  # NULL on backfilled rows

    def test_empty_db_with_no_wallet_table_raises(self, sqlite_url: str) -> None:
        """Migration on a DB without wallet_snapshots surfaces the error.

        The script does not pre-check table existence; SQLAlchemy raises
        when ``inspect(...).get_columns(...)`` is called on a missing
        table. This documents that callers must run the recorder once
        first (or `create_tables`) before applying 0042.
        """
        with pytest.raises(NoSuchTableError):
            _load_migration().migrate(sqlite_url)
