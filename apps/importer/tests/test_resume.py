"""End-to-end resume/idempotency tests through the importer CLI — feature 0093."""

from datetime import datetime, timedelta

from sqlalchemy import create_engine, text

from grid_db.models import Run, TickerSnapshot

import importer.main as importer_main
from importer.output_db import open_output_db, output_db_path

_T0 = datetime(2026, 7, 1, 0, 0, 0)


def _run_import(source_path, out_dir, symbols="BTCUSDT", extra=None) -> int:
    argv = [
        "--source", "db",
        "--source-url", f"sqlite:///{source_path}",
        "--symbols", symbols,
        "--out-dir", str(out_dir),
    ] + (extra or [])
    return importer_main.main(argv)


def _rows(out_dir, symbol="BTCUSDT"):
    db = open_output_db(output_db_path(str(out_dir), symbol))
    with db.get_session() as session:
        return (
            session.query(TickerSnapshot.exchange_ts, TickerSnapshot.last_price)
            .filter(TickerSnapshot.symbol == symbol)
            .order_by(TickerSnapshot.exchange_ts)
            .all()
        )


def _recording_runs(out_dir, symbol="BTCUSDT"):
    db = open_output_db(output_db_path(str(out_dir), symbol))
    with db.get_session() as session:
        return [
            (r.start_ts, r.end_ts)
            for r in session.query(Run).filter(Run.run_type == "recording").all()
        ]


class TestResume:
    def test_rerun_is_idempotent(self, tmp_path, seed_source_db, src_row):
        """Overlapping rerun inserts no duplicates (resume skip + OR IGNORE)."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        seed_source_db(
            source, [src_row(i, _T0 + timedelta(minutes=i)) for i in range(10)]
        )
        assert _run_import(source, out) == 0
        # Extend the source with 5 later rows; window overlaps fully.
        seed_source_db(
            source,
            [src_row(10 + i, _T0 + timedelta(minutes=10 + i)) for i in range(5)],
        )
        assert _run_import(source, out) == 0
        rows = _rows(out)
        assert len(rows) == 15
        assert len({ts for ts, _ in rows}) == 15

    def test_end_bound_inclusive(self, tmp_path, seed_source_db, src_row):
        """A row with exchange_ts == --end is imported."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        seed_source_db(
            source, [src_row(i, _T0 + timedelta(minutes=i)) for i in range(3)]
        )
        end = (_T0 + timedelta(minutes=2)).isoformat()
        assert _run_import(source, out, extra=["--end", end]) == 0
        assert len(_rows(out)) == 3

    def test_crash_resume_survives_committed_batches(
        self, tmp_path, seed_source_db, src_row, monkeypatch
    ):
        """Per-batch commits survive a mid-import crash; rerun resumes and heals."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        seed_source_db(
            source, [src_row(i, _T0 + timedelta(minutes=i)) for i in range(9)]
        )

        calls = {"n": 0}
        real_insert = importer_main.insert_batch

        def flaky_insert(db, snapshots):
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("simulated crash mid-import")
            return real_insert(db, snapshots)

        monkeypatch.setattr(importer_main, "insert_batch", flaky_insert)
        assert _run_import(source, out, extra=["--batch-size", "3"]) == 1
        monkeypatch.setattr(importer_main, "insert_batch", real_insert)

        # 2 batches x 3 rows committed; no run row yet (crash before step 4).
        assert len(_rows(out)) == 6
        assert _recording_runs(out) == []

        # Rerun resumes from the last committed row and heals the run row.
        assert _run_import(source, out, extra=["--batch-size", "3"]) == 0
        assert len(_rows(out)) == 9
        runs = _recording_runs(out)
        assert runs == [(_T0, _T0 + timedelta(minutes=8))]

    def test_single_run_row_across_appends(self, tmp_path, seed_source_db, src_row):
        """Append rerun keeps ONE recording row: start_ts fixed, end_ts advanced."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        seed_source_db(
            source, [src_row(i, _T0 + timedelta(minutes=i)) for i in range(3)]
        )
        assert _run_import(source, out) == 0
        seed_source_db(
            source, [src_row(3, _T0 + timedelta(hours=2))]
        )
        assert _run_import(source, out) == 0
        runs = _recording_runs(out)
        assert runs == [(_T0, _T0 + timedelta(hours=2))]

    def test_prefix_guard_aborts_with_tag_hint(
        self, tmp_path, seed_source_db, src_row, capsys
    ):
        """Default-start rerun after a mid-range first import aborts (append-only)."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        seed_source_db(
            source, [src_row(i, _T0 + timedelta(minutes=i)) for i in range(10)]
        )
        mid = (_T0 + timedelta(minutes=5)).isoformat()
        assert _run_import(source, out, extra=["--start", mid]) == 0
        assert len(_rows(out)) == 5
        # Plain rerun resolves start = source MIN < existing MIN -> abort.
        # setup_logging clears root handlers (caplog's included), so assert
        # on the stdout log stream instead.
        assert _run_import(source, out) == 1
        assert "--tag" in capsys.readouterr().out
        assert len(_rows(out)) == 5  # prefix was not silently skipped-over

    def test_zero_row_symbol_fails_others_continue(
        self, tmp_path, seed_source_db, src_row
    ):
        """All-NULL symbol creates no run row + non-zero exit; others import."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        seed_source_db(
            source,
            [src_row(1, _T0), src_row(2, _T0, symbol="ETHUSDT", last_price=None)],
        )
        assert _run_import(source, out, symbols="ETHUSDT,BTCUSDT") == 1
        assert len(_rows(out, "BTCUSDT")) == 1
        assert _recording_runs(out, "BTCUSDT") != []
        assert _rows(out, "ETHUSDT") == []
        assert _recording_runs(out, "ETHUSDT") == []

    def test_duplicate_source_timestamps_collapse_first_by_id(
        self, tmp_path, seed_source_db, src_row
    ):
        """Rows sharing (symbol, timestamp) collapse to the first by id."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        dup_ts = _T0 + timedelta(seconds=1)
        seed_source_db(
            source,
            [
                src_row(1, _T0),
                src_row(2, dup_ts, last_price=101.0),
                src_row(3, dup_ts, last_price=102.0),
                src_row(4, dup_ts, last_price=103.0),
                src_row(5, _T0 + timedelta(seconds=2)),
            ],
        )
        # batch-size 2 puts the page boundary inside the duplicate group.
        assert _run_import(source, out, extra=["--batch-size", "2"]) == 0
        rows = _rows(out)
        assert len(rows) == 3  # distinct timestamps only
        by_ts = {ts: price for ts, price in rows}
        assert str(by_ts[dup_ts]) == "101.00000000"  # first by id survived

    def test_http_source_requires_explicit_bounds(self, tmp_path, capsys):
        """--source http without --start/--end fails per symbol (no probe)."""
        argv = [
            "--source", "http",
            "--source-url", "http://unreachable.invalid",
            "--symbols", "BTCUSDT",
            "--out-dir", str(tmp_path / "out"),
        ]
        assert importer_main.main(argv) == 1  # fails before any HTTP call
        assert "pass explicit bounds" in capsys.readouterr().out

    def test_tagged_import_is_isolated(self, tmp_path, seed_source_db, src_row):
        """--tag writes a distinct file, leaving the default DB untouched."""
        source = tmp_path / "source.db"
        out = tmp_path / "out"
        seed_source_db(source, [src_row(1, _T0)])
        assert _run_import(source, out) == 0
        assert _run_import(source, out, extra=["--tag", "fresh"]) == 0
        assert output_db_path(str(out), "BTCUSDT", "fresh").exists()
        # Both DBs independently complete.
        engine = create_engine(
            f"sqlite:///{output_db_path(str(out), 'BTCUSDT', 'fresh')}"
        )
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM ticker_snapshots")
            ).scalar()
        engine.dispose()
        assert count == 1
