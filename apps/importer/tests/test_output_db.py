"""Tests for output DB lifecycle: WAL, run row, importlock — feature 0093."""

import logging
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from grid_db.models import Run, TickerSnapshot
from grid_db.repositories.identity import RunRepository

from importer.output_db import (
    ImportLockHeldError,
    acquire_lock,
    ensure_parents,
    ensure_run_row,
    insert_batch,
    lock_path,
    open_output_db,
    output_db_path,
    release_lock,
)

_T0 = datetime(2026, 7, 1, 0, 0, 0)


def _snapshot(ts: datetime, symbol: str = "BTCUSDT") -> TickerSnapshot:
    price = Decimal("100.00000000")
    return TickerSnapshot(
        symbol=symbol,
        exchange_ts=ts,
        local_ts=ts,
        last_price=price,
        mark_price=price,
        bid1_price=price,
        ask1_price=price,
        funding_rate=Decimal("0"),
    )


class TestOutputDbPath:
    def test_default_and_tagged_paths(self, tmp_path):
        """Stable default path; --tag appends a suffix before .db."""
        assert output_db_path(str(tmp_path), "BTCUSDT").name == "imported_BTCUSDT.db"
        assert (
            output_db_path(str(tmp_path), "BTCUSDT", "fresh").name
            == "imported_BTCUSDT_fresh.db"
        )


class TestOutputDb:
    def test_wal_enabled(self, tmp_path):
        """The output DB is opened in WAL journal mode."""
        db = open_output_db(tmp_path / "out.db")
        with db.engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert mode == "wal"

    def test_run_row_discoverable_by_replay(self, tmp_path):
        """ensure_run_row creates a recording run replay auto-discovery finds."""
        db = open_output_db(tmp_path / "out.db")
        ensure_parents(db, "BTCUSDT")
        insert_batch(db, [_snapshot(_T0), _snapshot(_T0 + timedelta(minutes=5))])
        run_id = ensure_run_row(db, "BTCUSDT", "db:sqlite:///src.db")
        assert run_id is not None
        with db.get_session() as session:
            run = RunRepository(session).get_latest_by_type("recording")
            assert run is not None
            assert run.run_id == run_id
            assert run.account_id is not None  # replay snapshot writer needs it
            assert run.start_ts == _T0
            assert run.end_ts == _T0 + timedelta(minutes=5)
            assert run.status == "completed"

    def test_run_row_zero_row_rule(self, tmp_path):
        """No ticker rows -> no run row (Run.start_ts is nullable=False)."""
        db = open_output_db(tmp_path / "out.db")
        ensure_parents(db, "BTCUSDT")
        assert ensure_run_row(db, "BTCUSDT", "src") is None
        with db.get_session() as session:
            assert session.query(Run).count() == 0

    def test_run_row_updated_not_duplicated(self, tmp_path):
        """Resume-append updates the single run row from DB-wide MIN/MAX."""
        db = open_output_db(tmp_path / "out.db")
        ensure_parents(db, "BTCUSDT")
        insert_batch(db, [_snapshot(_T0)])
        first_run_id = ensure_run_row(db, "BTCUSDT", "src")
        insert_batch(db, [_snapshot(_T0 + timedelta(hours=1))])
        second_run_id = ensure_run_row(db, "BTCUSDT", "src")
        assert second_run_id == first_run_id
        with db.get_session() as session:
            runs = session.query(Run).filter(Run.run_type == "recording").all()
            assert len(runs) == 1
            assert runs[0].start_ts == _T0  # unchanged on append
            assert runs[0].end_ts == _T0 + timedelta(hours=1)

    def test_parents_idempotent(self, tmp_path):
        """ensure_parents reruns session.get the same pinned rows."""
        db = open_output_db(tmp_path / "out.db")
        first = ensure_parents(db, "BTCUSDT")
        second = ensure_parents(db, "BTCUSDT")
        assert first == second


class TestImportLock:
    def test_second_importer_blocked(self, tmp_path):
        """A live lock (our own pid) blocks a second acquire."""
        db_path = tmp_path / "out.db"
        lock = acquire_lock(db_path)
        try:
            with pytest.raises(ImportLockHeldError, match="held by live pid"):
                acquire_lock(db_path)
        finally:
            release_lock(lock)

    def test_released_on_clean_exit(self, tmp_path):
        """After release the lock file is gone and re-acquire succeeds."""
        db_path = tmp_path / "out.db"
        lock = acquire_lock(db_path)
        release_lock(lock)
        assert not lock_path(db_path).exists()
        release_lock(lock)  # idempotent
        lock = acquire_lock(db_path)
        release_lock(lock)

    def test_stale_lock_reclaimed(self, tmp_path, caplog):
        """A dead-PID lock is reclaimed with a WARNING."""
        db_path = tmp_path / "out.db"
        proc = subprocess.Popen(["true"])
        proc.wait()  # guaranteed-dead pid
        lock_path(db_path).parent.mkdir(parents=True, exist_ok=True)
        lock_path(db_path).write_text(f"pid={proc.pid}\nstart=2026-07-01T00:00:00\n")
        with caplog.at_level(logging.WARNING):
            lock = acquire_lock(db_path)
        release_lock(lock)
        assert any("stale import lock" in r.message for r in caplog.records)

    def test_unreadable_lock_aborts(self, tmp_path):
        """A garbage lock file aborts with a manual-removal hint."""
        db_path = tmp_path / "out.db"
        lock_path(db_path).write_text("garbage\n")
        with pytest.raises(ImportLockHeldError, match="unreadable"):
            acquire_lock(db_path)

    def test_post_reclaim_race_maps_to_lock_error(self, tmp_path, monkeypatch):
        """Losing the post-reclaim O_EXCL race raises ImportLockHeldError."""
        db_path = tmp_path / "out.db"
        lock_path(db_path).write_text("pid=999999\nstart=x\n")
        # Simulate a concurrent importer re-creating the lock between
        # reclaim and re-open: reclaim becomes a no-op, file stays.
        monkeypatch.setattr(
            "importer.output_db._reclaim_or_abort", lambda path: None
        )
        with pytest.raises(ImportLockHeldError, match="concurrent importer"):
            acquire_lock(db_path)
