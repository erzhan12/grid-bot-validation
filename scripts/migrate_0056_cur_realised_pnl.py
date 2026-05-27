"""One-off schema migration for feature 0056 (cycle-scoped realized PnL).

Adds the ``cur_realised_pnl`` column to ``position_snapshots``:
    cur_realised_pnl NUMERIC(20, 8)

This is Bybit's per-cycle realized PnL (`curRealisedPnl` in the position
payload). It accumulates on every fill, including the close, and only resets
to zero on the next opening fill of the same side — not on the close itself.
The lifetime ``cum_realised_pnl`` column (feature 0034) is untouched.

Forward-only. Pre-migration rows remain NULL — the raw value is still
preserved verbatim inside ``raw_json``.

Idempotent: ADD COLUMN is guarded by a schema probe.

Usage:
    uv run python scripts/migrate_0056_cur_realised_pnl.py \\
        --database-url "sqlite:///data/recorder_ltcusdt_phase4.db"
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger("migrate_0056")


NEW_COLUMNS: list[tuple[str, str]] = [
    ("cur_realised_pnl", "NUMERIC(20, 8)"),
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
            if _column_exists(conn, "position_snapshots", col_name):
                continue
            # Direct f-string DDL: `col_name` / `col_def` are repo-local
            # constants (no user input), so there is no injection surface.
            conn.execute(
                text(f"ALTER TABLE position_snapshots ADD COLUMN {col_name} {col_def}")
            )
            added.append(col_name)

        if added:
            logger.info("Added columns: %s", added)
        else:
            logger.info("All 0056 columns already present")

    logger.info("Migration complete")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    migrate(args.database_url)


if __name__ == "__main__":
    main()
