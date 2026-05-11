"""Position telemetry loader (feature 0034).

Reads ``position_snapshots`` rows filtered by ``source`` (``'live'`` or
``'backtest'``) for a given run and symbol. Returns the rows sorted by
``(side, exchange_ts)`` so the pairing pass can stream them per-side
with a monotonic two-pointer.

Raises ``PositionTelemetryNotMigratedError`` when the ``source`` column
is missing — that is the only signal that the operator forgot to apply
the 0034 schema migration. We never silently mask un-migrated DBs as
zero-pair scenarios.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from grid_db.models import PositionSnapshot


class PositionTelemetryNotMigratedError(RuntimeError):
    """The DB has ``position_snapshots`` but the 0034 columns are missing.

    Raised at the start of ``load_position_snapshots`` so the operator
    sees the migration message before any comparison runs.
    """


def _probe_schema(session: Session) -> None:
    """Raise ``PositionTelemetryNotMigratedError`` if the 0034 columns are missing.

    Uses a trivial ``SELECT source FROM position_snapshots LIMIT 1`` —
    cheap on any backend, and fails loudly with ``OperationalError``
    (SQLite) or ``ProgrammingError`` (Postgres) when the column does
    not exist.
    """
    try:
        session.execute(
            PositionSnapshot.__table__.select()
            .with_only_columns(PositionSnapshot.source)
            .limit(1)
        ).first()
    except (OperationalError, ProgrammingError) as exc:
        msg = str(exc).lower()
        if "source" in msg and ("no such column" in msg or "does not exist" in msg):
            raise PositionTelemetryNotMigratedError(
                "Run the 0034 schema migration before comparing position "
                "telemetry. Position_snapshots is missing the 'source' column. "
                "See scripts/migrate_0034_position_telemetry.py."
            ) from exc
        raise


def load_position_snapshots(
    session: Session,
    run_id: str,
    symbol: str,
    source: str,
    start_ts: Optional[datetime] = None,
    end_ts: Optional[datetime] = None,
) -> list[PositionSnapshot]:
    """Load position snapshots filtered by source, sorted by (side, exchange_ts).

    Args:
        session: Open DB session.
        run_id: Recorder run identifier (scoped per-run; cross-run mixing
            is a separate parity story).
        symbol: Trading symbol, e.g. ``'LTCUSDT'``.
        source: ``'live'`` or ``'backtest'`` — never ``None``. The
            comparator deliberately always loads one source at a time so
            the pairing pass operates on two clean per-side streams.
        start_ts: Optional inclusive lower bound on ``exchange_ts``.
        end_ts: Optional inclusive upper bound on ``exchange_ts``.

    Returns:
        List of :class:`PositionSnapshot` ordered by ``(side, exchange_ts)``.

    Raises:
        PositionTelemetryNotMigratedError: If 0034 columns are absent.
    """
    _probe_schema(session)

    query = session.query(PositionSnapshot).filter(
        PositionSnapshot.run_id == run_id,
        PositionSnapshot.symbol == symbol,
        PositionSnapshot.source == source,
    )
    if start_ts is not None:
        query = query.filter(PositionSnapshot.exchange_ts >= start_ts)
    if end_ts is not None:
        query = query.filter(PositionSnapshot.exchange_ts <= end_ts)
    return query.order_by(
        PositionSnapshot.side.asc(),
        PositionSnapshot.exchange_ts.asc(),
    ).all()


__all__ = ["PositionTelemetryNotMigratedError", "load_position_snapshots"]
