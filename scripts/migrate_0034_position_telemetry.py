"""One-off schema migration for feature 0034 (position telemetry parity).

Adds five columns to `position_snapshots`:
    source, mark_price, position_im, position_mm, cum_realised_pnl

Replaces the 0029 index `ix_position_snapshots_run_account_symbol_side_ts`
with the new 0034 index that includes `source` before `exchange_ts`.

Adds a CHECK constraint on `source IN ('live', 'backtest')` where
supported. SQLite cannot ADD CONSTRAINT post-hoc — for SQLite the CHECK
is enforced at write time by the ORM `default='live'` and is implicit
in the index-equality predicate.

Idempotent: each step is gated on a probe.

Usage:
    uv run python scripts/migrate_0034_position_telemetry.py \\
        --database-url "sqlite:///data/recorder_ltcusdt_phase4.db"
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger("migrate_0034")


NEW_COLUMNS: list[tuple[str, str]] = [
    ("source", "VARCHAR(16) NOT NULL DEFAULT 'live'"),
    ("mark_price", "NUMERIC(20, 8)"),
    ("position_im", "NUMERIC(20, 8)"),
    ("position_mm", "NUMERIC(20, 8)"),
    ("cum_realised_pnl", "NUMERIC(20, 8)"),
]

OLD_INDEX = "ix_position_snapshots_run_account_symbol_side_ts"
NEW_INDEX = "ix_position_snapshots_run_account_symbol_side_source_ts"


def _column_exists(conn, table: str, column: str) -> bool:
    inspector = inspect(conn)
    return any(c["name"] == column for c in inspector.get_columns(table))


def _index_exists(conn, table: str, index_name: str) -> bool:
    inspector = inspect(conn)
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def migrate(database_url: str) -> None:
    engine = create_engine(database_url)
    dialect = engine.dialect.name
    logger.info("Migrating %s (dialect=%s)", database_url, dialect)

    with engine.begin() as conn:
        added: list[str] = []
        for col_name, col_def in NEW_COLUMNS:
            if _column_exists(conn, "position_snapshots", col_name):
                continue
            conn.execute(
                text(f"ALTER TABLE position_snapshots ADD COLUMN {col_name} {col_def}")
            )
            added.append(col_name)
        if added:
            logger.info("Added columns: %s", added)
        else:
            logger.info("All 0034 columns already present")

        # Backfill source='live' on any pre-existing rows (server_default
        # only applies to rows inserted after the column was added).
        conn.execute(
            text(
                "UPDATE position_snapshots SET source='live' "
                "WHERE source IS NULL"
            )
        )

        if _index_exists(conn, "position_snapshots", OLD_INDEX):
            conn.execute(text(f"DROP INDEX {OLD_INDEX}"))
            logger.info("Dropped legacy index %s", OLD_INDEX)

        if not _index_exists(conn, "position_snapshots", NEW_INDEX):
            conn.execute(
                text(
                    f"CREATE INDEX {NEW_INDEX} ON position_snapshots "
                    "(run_id, account_id, symbol, side, source, exchange_ts)"
                )
            )
            logger.info("Created index %s", NEW_INDEX)

        if dialect == "postgresql":
            try:
                conn.execute(
                    text(
                        "ALTER TABLE position_snapshots "
                        "ADD CONSTRAINT ck_position_snapshots_source "
                        "CHECK (source IN ('live', 'backtest'))"
                    )
                )
                logger.info("Added CHECK constraint")
            except Exception as exc:
                if "already exists" in str(exc):
                    logger.info("CHECK constraint already present")
                else:
                    raise

    logger.info("Migration complete")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    migrate(args.database_url)


if __name__ == "__main__":
    main()
