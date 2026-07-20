"""Tests for the per-day density report — feature 0093."""

from datetime import datetime, timedelta
from decimal import Decimal

from grid_db.models import TickerSnapshot

from importer.density import compute_density, log_density_report
from importer.output_db import insert_batch, open_output_db

_T0 = datetime(2026, 7, 1, 0, 0, 0)


def _snapshot(ts: datetime) -> TickerSnapshot:
    price = Decimal("100.00000000")
    return TickerSnapshot(
        symbol="BTCUSDT",
        exchange_ts=ts,
        local_ts=ts,
        last_price=price,
        mark_price=price,
        bid1_price=price,
        ask1_price=price,
        funding_rate=Decimal("0"),
    )


def _seed(tmp_path, timestamps):
    db = open_output_db(tmp_path / "out.db")
    insert_batch(db, [_snapshot(ts) for ts in timestamps])
    return db


class TestDensity:
    def test_per_day_counts(self, tmp_path):
        """Ticks are counted per UTC day."""
        db = _seed(
            tmp_path,
            [_T0, _T0 + timedelta(seconds=1), _T0 + timedelta(days=1)],
        )
        days = compute_density(db, "BTCUSDT")
        assert [(d.day.isoformat(), d.tick_count) for d in days] == [
            ("2026-07-01", 2),
            ("2026-07-02", 1),
        ]

    def test_gap_detection_over_60s(self, tmp_path):
        """Consecutive deltas > 60 s are reported as gaps; <= 60 s are not."""
        db = _seed(
            tmp_path,
            [
                _T0,
                _T0 + timedelta(seconds=60),  # exactly 60 -> not a gap
                _T0 + timedelta(seconds=121),  # 61 s -> gap
            ],
        )
        (day,) = compute_density(db, "BTCUSDT")
        assert day.gaps == [
            (_T0 + timedelta(seconds=60), _T0 + timedelta(seconds=121))
        ]

    def test_low_density_flag(self, tmp_path):
        """A day under 0.5 ticks/s over its covered span is LOW-DENSITY."""
        sparse = _seed(tmp_path, [_T0, _T0 + timedelta(seconds=1000)])
        (day,) = compute_density(sparse, "BTCUSDT")
        assert day.ticks_per_second < 0.5
        assert day.low_density

    def test_dense_day_not_flagged(self, tmp_path):
        """A day at >= 0.5 ticks/s is not flagged."""
        ticks = [_T0 + timedelta(seconds=i) for i in range(100)]
        db = _seed(tmp_path, ticks)
        (day,) = compute_density(db, "BTCUSDT")
        assert day.ticks_per_second >= 0.5
        assert not day.low_density

    def test_report_returns_low_density_presence(self, tmp_path, caplog):
        """log_density_report returns True and warns when a low day exists."""
        import logging

        db = _seed(tmp_path, [_T0, _T0 + timedelta(seconds=1000)])
        days = compute_density(db, "BTCUSDT")
        with caplog.at_level(logging.INFO):
            assert log_density_report("BTCUSDT", days) is True
        assert any("LOW-DENSITY" in r.message for r in caplog.records)

    def test_empty_db(self, tmp_path):
        """No rows -> empty report."""
        db = open_output_db(tmp_path / "out.db")
        assert compute_density(db, "BTCUSDT") == []
