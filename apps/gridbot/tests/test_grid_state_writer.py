"""Tests for ``GridStateWriter`` (feature 0047).

Real SQLite session — the partial unique index and FIFO id ordering are
both dialect-level behaviours that mocked sessions cannot exercise.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from grid_db.database import DatabaseFactory
from grid_db.models import (
    BybitAccount,
    GridStateSnapshot,
    Run,
    Strategy,
    User,
)
from grid_db.repositories import GridStateSnapshotRepository
from grid_db.settings import DatabaseSettings
from gridbot.writers.grid_state_writer import GridStateWriter
from gridcore.persistence import grid_fingerprint, grid_fingerprint_hash


@pytest.fixture
def db() -> DatabaseFactory:
    """Fresh in-memory SQLite DB with seeded parent rows for each test."""
    settings = DatabaseSettings(db_type="sqlite", db_name=":memory:")
    db = DatabaseFactory(settings)
    db.create_tables()
    with db.get_session() as sess:
        sess.add_all([
            User(user_id="u1", username="u1"),
            BybitAccount(
                account_id="acc1", user_id="u1",
                account_name="n", environment="mainnet",
            ),
            Strategy(
                strategy_id="s1", account_id="acc1",
                strategy_type="GridStrategy", symbol="LTCUSDT",
                config_json={},
            ),
            Run(
                run_id="run1", user_id="u1", account_id="acc1",
                strategy_id="s1", run_type="live",
                # 0062: explicit past start_ts so get_at_or_before's
                # ``start_ts <= at_ts`` guard doesn't exclude this run for
                # the fixed-past ``at_ts`` queried below (default utc_now()
                # would be > at_ts).
                start_ts=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ])
    return db


@pytest.fixture
def grid() -> list[dict]:
    return [
        {"side": "Buy", "price": 100.0},
        {"side": "Wait", "price": 101.0},
        {"side": "Sell", "price": 102.0},
    ]


def _make_writer(db, run_ids: dict[str, str]) -> GridStateWriter:
    writer = GridStateWriter(db, run_id_provider=lambda sid: run_ids.get(sid))
    writer.start()
    return writer


def _rows(db) -> list[dict]:
    """Return snapshots as plain dicts so attributes survive session close."""
    with db.get_session() as sess:
        rows = (
            sess.query(GridStateSnapshot)
            .order_by(GridStateSnapshot.id)
            .all()
        )
        return [
            {
                "id": r.id,
                "run_id": r.run_id,
                "account_id": r.account_id,
                "strat_id": r.strat_id,
                "symbol": r.symbol,
                "exchange_ts": r.exchange_ts,
                "local_ts": r.local_ts,
                "grid_json": r.grid_json,
                "grid_step": r.grid_step,
                "grid_count": r.grid_count,
                "raw_fingerprint": r.raw_fingerprint,
            }
            for r in rows
        ]


class TestGridStateWriter:
    def test_happy_path_insert(self, db, grid):
        writer = _make_writer(db, {"strat1": "run1"})
        writer.write(
            strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
            account_id="acc1", symbol="LTCUSDT",
            exchange_ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        writer.flush(timeout=5.0)
        writer.stop()

        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run1"
        assert rows[0]["account_id"] == "acc1"
        assert rows[0]["strat_id"] == "strat1"
        assert rows[0]["symbol"] == "LTCUSDT"
        assert rows[0]["grid_step"] == Decimal("0.5")
        assert rows[0]["grid_count"] == 3
        assert rows[0]["raw_fingerprint"] == grid_fingerprint_hash(grid, 0.5, 3)
        assert len(rows[0]["grid_json"]) == 3

    def test_drop_when_run_id_provider_returns_none(self, db, grid, caplog):
        # No mapping → provider returns None → snapshot dropped with INFO.
        writer = _make_writer(db, {})
        with caplog.at_level("INFO"):
            writer.write(
                strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
                account_id="acc1", symbol="LTCUSDT",
                exchange_ts=datetime(2026, 1, 1, tzinfo=UTC),
            )
        writer.flush(timeout=5.0)
        writer.stop()

        assert _rows(db) == []
        assert writer.get_stats()["total_dropped_no_run_id"] == 1
        assert any(
            "run_id not yet set" in rec.message for rec in caplog.records
        )

    def test_drop_when_exchange_ts_none(self, db, grid):
        writer = _make_writer(db, {"strat1": "run1"})
        writer.write(
            strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
            account_id="acc1", symbol="LTCUSDT",
            exchange_ts=None,
        )
        writer.flush(timeout=5.0)
        writer.stop()

        assert _rows(db) == []
        assert writer.get_stats()["total_dropped_no_ts"] == 1

    def test_in_memory_dedup_skips_identical_successive_writes(self, db, grid):
        writer = _make_writer(db, {"strat1": "run1"})
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        for _ in range(3):
            writer.write(
                strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
                account_id="acc1", symbol="LTCUSDT", exchange_ts=ts,
            )
        writer.flush(timeout=5.0)
        writer.stop()

        rows = _rows(db)
        assert len(rows) == 1
        # Two of the three writes hit the in-memory dedupe gate before enqueue.
        assert writer.get_stats()["total_dedup_skipped"] >= 2

    def test_partial_index_conflict_do_nothing(self, db, grid):
        """Race-double-insert simulation: bypass dedupe by using two writers.

        The partial unique index ``uq_grid_state_snapshots_fingerprint_at_ts``
        must no-op the second insert (rowcount=0), not raise. NULL fingerprint
        rows are NOT blocked by the constraint (sub-assert).
        """
        # Manually insert two identical rows via the repository to bypass
        # the writer's pre-enqueue dedupe gate.
        snap = GridStateSnapshot(
            run_id="run1", account_id="acc1", strat_id="strat1",
            symbol="LTCUSDT",
            exchange_ts=datetime(2026, 1, 1, tzinfo=UTC),
            local_ts=datetime(2026, 1, 1, tzinfo=UTC),
            grid_json=grid, grid_step=Decimal("0.5"), grid_count=3,
            raw_fingerprint=grid_fingerprint_hash(grid, 0.5, 3),
        )
        with db.get_session() as sess:
            repo = GridStateSnapshotRepository(sess)
            assert repo.insert(snap) == 1
            # Reuse another instance with identical fields — partial unique
            # blocks via ON CONFLICT DO NOTHING.
            snap_dup = GridStateSnapshot(
                run_id=snap.run_id, account_id=snap.account_id,
                strat_id=snap.strat_id, symbol=snap.symbol,
                exchange_ts=snap.exchange_ts, local_ts=snap.local_ts,
                grid_json=snap.grid_json, grid_step=snap.grid_step,
                grid_count=snap.grid_count,
                raw_fingerprint=snap.raw_fingerprint,
            )
            assert repo.insert(snap_dup) == 0
            # NULL fingerprint rows fall outside the partial unique scope.
            snap_null1 = GridStateSnapshot(
                run_id=snap.run_id, account_id=snap.account_id,
                strat_id=snap.strat_id, symbol=snap.symbol,
                exchange_ts=snap.exchange_ts, local_ts=snap.local_ts,
                grid_json=snap.grid_json, grid_step=snap.grid_step,
                grid_count=snap.grid_count,
                raw_fingerprint=None,
            )
            snap_null2 = GridStateSnapshot(
                run_id=snap.run_id, account_id=snap.account_id,
                strat_id=snap.strat_id, symbol=snap.symbol,
                exchange_ts=snap.exchange_ts, local_ts=snap.local_ts,
                grid_json=snap.grid_json, grid_step=snap.grid_step,
                grid_count=snap.grid_count,
                raw_fingerprint=None,
            )
            assert repo.insert(snap_null1) == 1
            assert repo.insert(snap_null2) == 1

        rows = _rows(db)
        assert len(rows) == 3  # 1 hashed + 2 nulls

    def test_flush_and_stop_drain_queue(self, db, grid):
        writer = _make_writer(db, {"strat1": "run1"})
        for i in range(5):
            grid_variant = [{"side": "Buy", "price": 100.0 + i}, *grid[1:]]
            writer.write(
                strat_id="strat1", grid=grid_variant,
                grid_step=0.5, grid_count=3,
                account_id="acc1", symbol="LTCUSDT",
                exchange_ts=datetime(2026, 1, 1, 0, i, tzinfo=UTC),
            )
        writer.flush(timeout=5.0)
        writer.stop()
        assert len(_rows(db)) == 5

    def test_writer_preserves_fifo_for_same_strat_same_ts(self, db):
        """Two snapshots with identical (run,account,strat,exchange_ts) but
        different ``grid_json`` payloads must INSERT in enqueue order.

        Critical for the loader's ``ORDER BY exchange_ts DESC, id DESC``
        tie-break to pick the FINAL notify of a multi-notify outer mutation
        (e.g. ``update_grid`` out-of-bounds path emits post-rebuild then
        post-side-assignment).
        """
        writer = _make_writer(db, {"strat1": "run1"})
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        # Different grids → different fingerprints → both bypass the
        # in-memory dedupe gate.
        intermediate = [
            {"side": "Buy", "price": 100.0}, {"side": "Wait", "price": 101.0},
            {"side": "Sell", "price": 102.0},
        ]
        final = [
            {"side": "Buy", "price": 100.0}, {"side": "Buy", "price": 101.0},
            {"side": "Wait", "price": 102.0}, {"side": "Sell", "price": 103.0},
        ]
        writer.write(
            strat_id="strat1", grid=intermediate, grid_step=0.5, grid_count=3,
            account_id="acc1", symbol="LTCUSDT", exchange_ts=ts,
        )
        writer.write(
            strat_id="strat1", grid=final, grid_step=0.5, grid_count=4,
            account_id="acc1", symbol="LTCUSDT", exchange_ts=ts,
        )
        writer.flush(timeout=5.0)
        writer.stop()

        rows = _rows(db)
        assert len(rows) == 2
        # First row inserted (smaller id) is the intermediate payload.
        assert rows[0]["grid_count"] == 3
        # Larger id is the final post-side-assignment payload — what the
        # loader's id DESC tie-break must pick.
        assert rows[1]["grid_count"] == 4
        assert rows[0]["id"] < rows[1]["id"]

    def test_dedupe_rolled_back_after_insert_failure(self, db, grid):
        """0047 P1: a transient DB failure on the first write must NOT
        block the same state from being re-enqueued and persisted.

        Without rollback, ``_last_fingerprint`` keeps the failed payload's
        tuple and the next identical mutation hits the dedupe gate — but
        no DB row exists, so replay has no seed.
        """
        writer = _make_writer(db, {"strat1": "run1"})

        # Force the FIRST insert to fail by patching the repository class
        # on the module the worker imports it from. We restore before the
        # second write so the retry can land.
        from gridbot.writers import grid_state_writer as gsw

        original_repo = gsw.GridStateSnapshotRepository
        call_count = {"n": 0}

        class FlakyRepo(original_repo):
            def insert(self, snapshot):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("simulated transient DB failure")
                return super().insert(snapshot)

        ts = datetime(2026, 1, 1, tzinfo=UTC)
        gsw.GridStateSnapshotRepository = FlakyRepo
        try:
            writer.write(
                strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
                account_id="acc1", symbol="LTCUSDT", exchange_ts=ts,
            )
            writer.flush(timeout=5.0)
            assert writer.get_stats()["total_errors"] == 1
            # Second write of the SAME state must NOT be dedup'd — rollback
            # cleared the in-memory gate.
            writer.write(
                strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
                account_id="acc1", symbol="LTCUSDT", exchange_ts=ts,
            )
            writer.flush(timeout=5.0)
        finally:
            gsw.GridStateSnapshotRepository = original_repo
            writer.stop()

        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["raw_fingerprint"] == grid_fingerprint_hash(grid, 0.5, 3)

    def test_get_at_or_before_picks_largest_id_on_tie(self, db):
        """End-to-end: writer enqueues two same-ts payloads; loader's
        ``get_at_or_before`` returns the row with the larger ``id``."""
        writer = _make_writer(db, {"strat1": "run1"})
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        intermediate = [
            {"side": "Buy", "price": 100.0}, {"side": "Wait", "price": 101.0},
        ]
        final = [
            {"side": "Buy", "price": 100.0}, {"side": "Sell", "price": 101.0},
        ]
        writer.write(
            strat_id="strat1", grid=intermediate, grid_step=0.5, grid_count=2,
            account_id="acc1", symbol="LTCUSDT", exchange_ts=ts,
        )
        writer.write(
            strat_id="strat1", grid=final, grid_step=0.5, grid_count=2,
            account_id="acc1", symbol="LTCUSDT", exchange_ts=ts,
        )
        writer.flush(timeout=5.0)
        writer.stop()

        with db.get_session() as sess:
            repo = GridStateSnapshotRepository(sess)
            picked = repo.get_at_or_before(
                "acc1", "strat1", "LTCUSDT", datetime(2026, 1, 2, tzinfo=UTC),
            )
            assert picked is not None
            assert picked.grid_json == final

    def test_get_last_fingerprint_returns_none_when_empty(self, db, grid):
        writer = _make_writer(db, {"strat1": "run1"})
        assert writer.get_last_fingerprint("run1", "acc1", "strat1") is None
        writer.stop()

    def test_get_last_fingerprint_returns_tuple_and_exchange_ts_from_latest_row(
        self, db, grid,
    ):
        writer = _make_writer(db, {"strat1": "run1"})
        ts_early = datetime(2026, 1, 1, tzinfo=UTC)
        ts_late = datetime(2026, 1, 2, tzinfo=UTC)
        grid_early = [{"side": "Buy", "price": 100.0}, *grid[1:]]
        grid_late = [{"side": "Buy", "price": 200.0}, *grid[1:]]
        writer.write(
            strat_id="strat1", grid=grid_early, grid_step=0.5, grid_count=3,
            account_id="acc1", symbol="LTCUSDT", exchange_ts=ts_early,
        )
        writer.write(
            strat_id="strat1", grid=grid_late, grid_step=0.5, grid_count=3,
            account_id="acc1", symbol="LTCUSDT", exchange_ts=ts_late,
        )
        writer.flush(timeout=5.0)
        writer.stop()

        result = writer.get_last_fingerprint("run1", "acc1", "strat1")
        assert result is not None
        fp, exchange_ts = result
        assert fp == grid_fingerprint(grid_late, 0.5, 3)
        assert exchange_ts.replace(tzinfo=UTC) == ts_late

    def test_get_last_fingerprint_propagates_db_errors(self, db, grid):
        writer = _make_writer(db, {"strat1": "run1"})
        from gridbot.writers import grid_state_writer as gsw

        class BrokenRepo(gsw.GridStateSnapshotRepository):
            def get_latest(self, run_id, account_id, strat_id):
                raise RuntimeError("simulated DB outage")

        original = gsw.GridStateSnapshotRepository
        gsw.GridStateSnapshotRepository = BrokenRepo
        try:
            with pytest.raises(RuntimeError, match="simulated DB outage"):
                writer.get_last_fingerprint("run1", "acc1", "strat1")
        finally:
            gsw.GridStateSnapshotRepository = original
            writer.stop()

    def test_prime_fingerprint_blocks_identical_subsequent_write(self, db, grid):
        writer = _make_writer(db, {"strat1": "run1"})
        fp = grid_fingerprint(grid, 0.5, 3)
        writer.prime_fingerprint(("run1", "acc1", "strat1"), fp)
        writer.write(
            strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
            account_id="acc1", symbol="LTCUSDT",
            exchange_ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        writer.flush(timeout=5.0)
        writer.stop()

        assert _rows(db) == []
        assert writer.get_stats()["total_dedup_skipped"] == 1

    def test_flush_returns_true_on_clean_drain_false_on_timeout(self, db, grid, caplog):
        import threading

        writer = _make_writer(db, {"strat1": "run1"})
        writer.write(
            strat_id="strat1", grid=grid, grid_step=0.5, grid_count=3,
            account_id="acc1", symbol="LTCUSDT",
            exchange_ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert writer.flush(timeout=5.0) is True

        block_event = threading.Event()
        from gridbot.writers import grid_state_writer as gsw

        original_insert = gsw.GridStateWriter._insert_one

        def slow_insert(self, snapshot, scope, fp_tuple):
            block_event.wait(timeout=10.0)
            return original_insert(self, snapshot, scope, fp_tuple)

        gsw.GridStateWriter._insert_one = slow_insert
        try:
            writer.write(
                strat_id="strat1", grid=[{"side": "Buy", "price": 99.0}, *grid[1:]],
                grid_step=0.5, grid_count=3,
                account_id="acc1", symbol="LTCUSDT",
                exchange_ts=datetime(2026, 1, 2, tzinfo=UTC),
            )
            with caplog.at_level("WARNING"):
                assert writer.flush(timeout=0.1) is False
            assert any("timed out" in rec.message for rec in caplog.records)
            block_event.set()
            assert writer.flush(timeout=5.0) is True
        finally:
            gsw.GridStateWriter._insert_one = original_insert
            writer.stop()
