"""Tests for transport A (direct-DB keyset pagination) — feature 0093."""

from datetime import datetime, timedelta, timezone

from importer.fetch_source_db import DbSource, aware_utc

_T0 = datetime(2026, 7, 1, 0, 0, 0)

# NOTE on the plan's "tz-aware source timestamps yielded naive UTC" case:
# SQLite cannot round-trip aware datetimes (the bind formatter drops
# tzinfo), so the Postgres timestamptz->naive path cannot be exercised
# against a sqlite fixture. The conversion itself (to_naive_utc) is
# unit-tested in test_config.py, the HTTP transport's aware path in
# test_fetch_http.py, and the window-bind side in TestAwareUtcBinds below.


class TestDbSource:
    def test_composite_keyset_pagination(self, tmp_path, seed_source_db, src_row):
        """Duplicate timestamps straddling a page boundary neither loop nor drop rows."""
        path = tmp_path / "source.db"
        # ids 1..5; ids 2,3,4 share one timestamp; batch_size=2 forces a
        # page boundary inside the duplicate group.
        rows = [
            src_row(1, _T0),
            src_row(2, _T0 + timedelta(seconds=1), last_price=101.0),
            src_row(3, _T0 + timedelta(seconds=1), last_price=102.0),
            src_row(4, _T0 + timedelta(seconds=1), last_price=103.0),
            src_row(5, _T0 + timedelta(seconds=2)),
        ]
        seed_source_db(path, rows)
        source = DbSource(f"sqlite:///{path}", batch_size=2)
        fetched = [
            r
            for batch in source.fetch_batches(
                "BTCUSDT", _T0, _T0 + timedelta(minutes=1)
            )
            for r in batch
        ]
        assert [r["id"] for r in fetched] == [1, 2, 3, 4, 5]

    def test_bounds_inclusive(self, tmp_path, seed_source_db, src_row):
        """Both start and end bounds are inclusive."""
        path = tmp_path / "source.db"
        rows = [src_row(i, _T0 + timedelta(seconds=i)) for i in range(5)]
        seed_source_db(path, rows)
        source = DbSource(f"sqlite:///{path}", batch_size=10)
        fetched = [
            r
            for batch in source.fetch_batches(
                "BTCUSDT", _T0 + timedelta(seconds=1), _T0 + timedelta(seconds=3)
            )
            for r in batch
        ]
        assert [r["id"] for r in fetched] == [1, 2, 3]

    def test_timestamps_yielded_naive(self, tmp_path, seed_source_db, src_row):
        """Yielded row timestamps are naive UTC (transport-boundary rule)."""
        path = tmp_path / "source.db"
        seed_source_db(path, [src_row(1, _T0)])
        source = DbSource(f"sqlite:///{path}")
        (batch,) = list(
            source.fetch_batches("BTCUSDT", _T0, _T0 + timedelta(minutes=1))
        )
        assert batch[0]["timestamp"] == _T0
        assert batch[0]["timestamp"].tzinfo is None

    def test_symbol_filter(self, tmp_path, seed_source_db, src_row):
        """Only rows for the requested symbol are yielded."""
        path = tmp_path / "source.db"
        seed_source_db(
            path,
            [src_row(1, _T0), src_row(2, _T0, symbol="ETHUSDT")],
        )
        source = DbSource(f"sqlite:///{path}")
        fetched = [
            r
            for batch in source.fetch_batches(
                "ETHUSDT", _T0, _T0 + timedelta(minutes=1)
            )
            for r in batch
        ]
        assert [r["id"] for r in fetched] == [2]

    def test_probe_range(self, tmp_path, seed_source_db, src_row):
        """probe_range returns naive (MIN, MAX); None for an absent symbol."""
        path = tmp_path / "source.db"
        rows = [src_row(i, _T0 + timedelta(minutes=i)) for i in range(3)]
        seed_source_db(path, rows)
        source = DbSource(f"sqlite:///{path}")
        probed = source.probe_range("BTCUSDT")
        assert probed == (_T0, _T0 + timedelta(minutes=2))
        assert probed[0].tzinfo is None and probed[1].tzinfo is None
        assert source.probe_range("ETHUSDT") is None

    def test_resume_comparison_no_typeerror(self, tmp_path, seed_source_db, src_row):
        """Yielded timestamps compare cleanly against a naive SQLite cursor."""
        path = tmp_path / "source.db"
        seed_source_db(
            path, [src_row(1, _T0), src_row(2, _T0 + timedelta(seconds=5))]
        )
        source = DbSource(f"sqlite:///{path}")
        resume_from = _T0  # what get_last_ticker_ts returns: naive
        kept = [
            r
            for batch in source.fetch_batches(
                "BTCUSDT", _T0, _T0 + timedelta(minutes=1)
            )
            for r in batch
            if r["timestamp"] > resume_from
        ]
        assert [r["id"] for r in kept] == [2]

    def test_source_opened_read_only(self, tmp_path, seed_source_db):
        """The source engine settings request read-only mode for SQLite."""
        path = tmp_path / "source.db"
        seed_source_db(path, [])
        source = DbSource(f"sqlite:///{path}")
        assert source._db.settings.read_only is True


class TestAwareUtcBinds:
    def test_naive_bound_becomes_aware_utc(self):
        """Window bounds bind aware UTC (session-tz hazard on timestamptz)."""
        bound = aware_utc(_T0)
        assert bound.tzinfo == timezone.utc
        assert bound.replace(tzinfo=None) == _T0

    def test_aware_bound_passthrough(self):
        """Already-aware bounds pass through unchanged."""
        aware = _T0.replace(tzinfo=timezone.utc)
        assert aware_utc(aware) is aware
