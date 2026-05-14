"""One-off schema migration for feature 0042 (UTA account wallet fields).

Adds account-level UTA wallet columns to `wallet_snapshots`:
    total_equity, total_available_balance, total_margin_balance,
    account_im_rate, account_mm_rate

Idempotent: every ADD COLUMN is guarded by a schema probe.

Usage:
    uv run python scripts/migrate_0042_uta_wallet_fields.py \\
        --database-url "sqlite:///data/recorder_ltcusdt_phase4.db"
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger("migrate_0042")


NEW_COLUMNS: list[tuple[str, str]] = [
    ("total_equity", "NUMERIC(20, 8)"),
    ("total_available_balance", "NUMERIC(20, 8)"),
    ("total_margin_balance", "NUMERIC(20, 8)"),
    ("account_im_rate", "NUMERIC(20, 8)"),
    ("account_mm_rate", "NUMERIC(20, 8)"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    inspector = inspect(conn)
    return any(c["name"] == column for c in inspector.get_columns(table))


def migrate(database_url: str) -> None:
    engine = create_engine(database_url)
    logger.info("Migrating %s (dialect=%s)", database_url, engine.dialect.name)

    with engine.begin() as conn:
        added: list[str] = []
        for col_name, col_def in NEW_COLUMNS:
            if _column_exists(conn, "wallet_snapshots", col_name):
                continue
            # Direct f-string DDL: `col_name` / `col_def` are repo-local
            # constants (no user input), so there is no injection surface.
            # SQLAlchemy's typed DDL (Column/MetaData) is overkill for a
            # one-off ADD COLUMN and would diverge from migrate_0034.
            conn.execute(
                text(f"ALTER TABLE wallet_snapshots ADD COLUMN {col_name} {col_def}")
            )
            added.append(col_name)

        if added:
            logger.info("Added columns: %s", added)
        else:
            logger.info("All 0042 columns already present")

    logger.info("Migration complete")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    migrate(args.database_url)


if __name__ == "__main__":
    main()
