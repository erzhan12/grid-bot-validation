"""Tests for the grid_db read-only open (Phase 1B(a)).

FILE-BACKED temp SQLite only — mode=ro is skipped for :memory: URLs, so an
in-memory test would pass vacuously without exercising the read-only path.
"""

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from grid_db import DatabaseFactory, DatabaseSettings, TickerSnapshot


def _insert_ticker(db, ts):
    with db.get_session() as session:
        session.add(TickerSnapshot(
            symbol="LTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            last_price=Decimal("80"),
            mark_price=Decimal("80"),
            bid1_price=Decimal("79.9"),
            ask1_price=Decimal("80.1"),
            funding_rate=Decimal("0.0001"),
        ))


def _count(session) -> int:
    return session.query(func.count(TickerSnapshot.id)).scalar()


@pytest.fixture
def file_db(tmp_path):
    """Writable file-backed DB with one ticker row."""
    url = f"sqlite:///{tmp_path}/recorder.db"
    db = DatabaseFactory(DatabaseSettings(database_url=url))
    db.create_tables()
    _insert_ticker(db, datetime(2026, 7, 1, 12, 0, 0))
    return db, url


class TestReadOnlyOpen:
    def test_url_rewritten_to_mode_ro(self, file_db):
        """read_only=True rewrites the engine URL to a mode=ro URI (no immutable)."""
        _, url = file_db
        ro = DatabaseFactory(DatabaseSettings(database_url=url, read_only=True))
        rendered = str(ro.engine.url)
        assert "mode=ro" in rendered
        assert "uri=true" in rendered
        assert "immutable" not in rendered

    def test_select_succeeds(self, file_db):
        """Reads work on the read-only open."""
        _, url = file_db
        ro = DatabaseFactory(DatabaseSettings(database_url=url, read_only=True))
        with ro.get_readonly_session() as session:
            assert _count(session) == 1

    def test_write_raises_operational_error(self, file_db):
        """INSERT via the read-only factory raises (readonly database)."""
        _, url = file_db
        ro = DatabaseFactory(DatabaseSettings(database_url=url, read_only=True))
        with ro.get_readonly_session() as session:
            _insert = TickerSnapshot(
                symbol="LTCUSDT",
                exchange_ts=datetime(2026, 7, 1, 13, 0, 0),
                local_ts=datetime(2026, 7, 1, 13, 0, 0),
                last_price=Decimal("81"),
                mark_price=Decimal("81"),
                bid1_price=Decimal("80.9"),
                ask1_price=Decimal("81.1"),
                funding_rate=Decimal("0.0001"),
            )
            session.add(_insert)
            with pytest.raises(OperationalError):
                session.flush()

    def test_new_writer_rows_visible_after_ro_open(self, file_db):
        """mode=ro (no immutable=1) sees rows committed AFTER the ro open.

        This is the --watch/freshness regression guard: immutable=1 would
        freeze the snapshot and hide the second row.
        """
        writer, url = file_db
        ro = DatabaseFactory(DatabaseSettings(database_url=url, read_only=True))
        with ro.get_readonly_session() as session:
            assert _count(session) == 1  # ro connection now open
        _insert_ticker(writer, datetime(2026, 7, 1, 12, 5, 0))
        with ro.get_readonly_session() as session:
            assert _count(session) == 2

    def test_memory_url_not_rewritten(self):
        """:memory: URLs skip the mode=ro rewrite (nothing to protect)."""
        db = DatabaseFactory(
            DatabaseSettings(db_type="sqlite", db_name=":memory:", read_only=True)
        )
        assert "mode=ro" not in str(db.engine.url)
