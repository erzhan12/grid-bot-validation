"""Transport A: direct SQLAlchemy read-only access to ``ticker_data``.

Feature 0093. Streams the source table keyset-paginated on the composite
cursor ``(timestamp, id)``: a bare ``timestamp >= cursor`` with LIMIT would
loop forever, and a bare strict ``>`` on timestamp alone could skip rows
sharing the cursor timestamp across a page boundary; the ``id`` tiebreaker
fixes both (same pattern as the source project's own reader).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    func,
    or_,
    select,
)

from grid_db.database import DatabaseFactory
from grid_db.settings import DatabaseSettings

from importer.config import to_naive_utc

logger = logging.getLogger(__name__)

# Lightweight Core mapping of the source table — typed binds and result
# conversion work on both SQLite (str <-> datetime) and Postgres. Source
# columns not needed by the mapping (sizes, 24h stats) are not selected.
_metadata = MetaData()
ticker_data = Table(
    "ticker_data",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("symbol", String),
    Column("timestamp", DateTime),
    Column("last_price", Float),
    Column("mark_price", Float),
    Column("bid1_price", Float),
    Column("ask1_price", Float),
    Column("funding_rate", Float),
)


def aware_utc(dt: datetime) -> datetime:
    """Bind window bounds as aware UTC.

    A naive bind against a Postgres ``timestamptz`` column is interpreted
    in the SESSION timezone, silently shifting the import window on
    non-UTC servers. An aware bind pins the absolute instant; SQLite's
    bind formatter ignores tzinfo and writes the same UTC wall-clock, so
    this is safe on both dialects.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class DbSource:
    """Keyset-paginated read-only reader over the source ``ticker_data`` table."""

    def __init__(self, url: str, batch_size: int = 10000):
        # read_only rewrites file-backed SQLite URLs to mode=ro; the flag is
        # inert for Postgres (reads happen via get_readonly_session either way).
        self._db = DatabaseFactory(
            DatabaseSettings(database_url=url, read_only=True)
        )
        self._batch_size = batch_size

    def fetch_batches(
        self, symbol: str, start: datetime, end: datetime
    ) -> Iterator[list[dict]]:
        """Yield 10k-row batches ordered by (timestamp, id), both bounds inclusive."""
        t = ticker_data
        start_bind, end_bind = aware_utc(start), aware_utc(end)
        with self._db.get_readonly_session() as session:
            cursor: Optional[tuple[datetime, int]] = None
            while True:
                query = select(
                    t.c.id,
                    t.c.symbol,
                    t.c.timestamp,
                    t.c.last_price,
                    t.c.mark_price,
                    t.c.bid1_price,
                    t.c.ask1_price,
                    t.c.funding_rate,
                ).where(t.c.symbol == symbol, t.c.timestamp <= end_bind)
                if cursor is None:
                    query = query.where(t.c.timestamp >= start_bind)
                else:
                    cts, cid = cursor
                    # Expanded row-value form of (timestamp, id) > (cts, cid).
                    query = query.where(
                        or_(
                            t.c.timestamp > cts,
                            and_(t.c.timestamp == cts, t.c.id > cid),
                        )
                    )
                query = query.order_by(t.c.timestamp, t.c.id).limit(
                    self._batch_size
                )
                rows = session.execute(query).all()
                if not rows:
                    return
                # Cursor uses the DB-native (pre-normalization) timestamp so
                # the next page's comparison matches stored values.
                cursor = (rows[-1]._mapping["timestamp"], rows[-1]._mapping["id"])
                batch = []
                for row in rows:
                    mapped = dict(row._mapping)
                    mapped["timestamp"] = to_naive_utc(mapped["timestamp"])
                    batch.append(mapped)
                yield batch

    def probe_range(self, symbol: str) -> Optional[tuple[datetime, datetime]]:
        """MIN/MAX ``timestamp`` for a symbol; None when the source has no rows."""
        t = ticker_data
        with self._db.get_readonly_session() as session:
            min_ts, max_ts = session.execute(
                select(func.min(t.c.timestamp), func.max(t.c.timestamp)).where(
                    t.c.symbol == symbol
                )
            ).one()
        if min_ts is None or max_ts is None:
            return None
        return to_naive_utc(min_ts), to_naive_utc(max_ts)
