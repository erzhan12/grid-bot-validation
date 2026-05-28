"""One-off schema migration for feature 0059 (position value parity).

Adds the ``position_value`` column to ``position_snapshots``:
    position_value NUMERIC(20, 8)

This is Bybit's position notional (`positionValue` in the position payload),
equal to ``size * entry_price`` for linear futures. It mirrors the value the
backtest tracker keeps in ``tracker.state.position_value`` and the 0058 log
line emits as ``pos_value_usdt``.

Forward-only. Pre-migration rows remain NULL — the raw value is still
preserved verbatim inside ``raw_json`` for live rows.

Idempotent: ADD COLUMN is guarded by a schema probe.

Usage:
    uv run python scripts/migrate_0059_position_value.py \\
        --database-url "sqlite:///data/recorder_ltcusdt_phase4.db"
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger("migrate_0059")


NEW_COLUMNS: list[tuple[str, str]] = [
    ("position_value", "NUMERIC(20, 8)"),
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
            logger.info("All 0059 columns already present")

    logger.info("Migration complete")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    migrate(args.database_url)


if __name__ == "__main__":
    main()
