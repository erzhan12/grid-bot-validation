"""Tests for the OHLC cross-check core (rebuild + compare) — feature 0093."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from grid_db.models import TickerSnapshot

from importer.output_db import insert_batch, open_output_db
from importer.validate import (
    OhlcBucket,
    _coerce_kline_ts,
    check_ohlc,
    compare_ohlc,
    rebuild_ohlc,
    recorder_overlap_probe,
    smoke_replay,
)

_T0 = datetime(2026, 7, 1, 10, 0, 0)
_TICK = Decimal("0.1")


def _ticks_two_minutes():
    """Two minutes of ticks: minute A 100->102 (H 103, L 99), minute B flat 105."""
    return [
        (_T0 + timedelta(seconds=1), Decimal("100.00000000")),
        (_T0 + timedelta(seconds=20), Decimal("103.00000000")),
        (_T0 + timedelta(seconds=40), Decimal("99.00000000")),
        (_T0 + timedelta(seconds=59), Decimal("102.00000000")),
        (_T0 + timedelta(seconds=61), Decimal("105.00000000")),
    ]


def _matching_klines(buckets):
    """Klines identical to the rebuilt buckets."""
    return {
        key: OhlcBucket(open=b.open, high=b.high, low=b.low, close=b.close)
        for key, b in buckets.items()
    }


class TestRebuildOhlc:
    def test_buckets_by_absolute_utc_minute(self):
        """Ticks bucket by epoch-minute with correct OHLC per bucket."""
        buckets = rebuild_ohlc(_ticks_two_minutes())
        assert len(buckets) == 2
        key_a = min(buckets)
        bucket_a = buckets[key_a]
        assert bucket_a.open == Decimal("100.00000000")
        assert bucket_a.high == Decimal("103.00000000")
        assert bucket_a.low == Decimal("99.00000000")
        assert bucket_a.close == Decimal("102.00000000")
        bucket_b = buckets[max(buckets)]
        assert bucket_b.open == bucket_b.close == Decimal("105.00000000")
        assert max(buckets) - key_a == 1  # consecutive minutes


class TestCompareOhlc:
    def test_exact_match_passes(self):
        """Identical buckets pass at threshold 0.99."""
        buckets = rebuild_ohlc(_ticks_two_minutes())
        result = compare_ohlc(buckets, _matching_klines(buckets), _TICK, 0.99)
        assert result.passed
        assert result.matched == result.compared == 2
        assert result.extra_keys == []

    def test_high_low_within_one_tick_tolerated(self):
        """High/low within 1 tick_size of the kline still count as matching."""
        buckets = rebuild_ohlc(_ticks_two_minutes())
        klines = _matching_klines(buckets)
        key = min(klines)
        klines[key].high += _TICK  # intra-minute extreme dropped by batching
        klines[key].low -= _TICK
        result = compare_ohlc(buckets, klines, _TICK, 0.99)
        assert result.passed

    def test_high_beyond_one_tick_mismatches(self):
        """High/low beyond 1 tick_size makes the bucket mismatch."""
        buckets = rebuild_ohlc(_ticks_two_minutes())
        klines = _matching_klines(buckets)
        key = min(klines)
        klines[key].high += _TICK * 2
        result = compare_ohlc(buckets, klines, _TICK, 0.99)
        assert not result.passed
        assert result.mismatched_keys == [key]

    def test_open_close_must_match_exactly(self):
        """Open/close use exact Decimal equality — below threshold fails."""
        buckets = rebuild_ohlc(_ticks_two_minutes())
        klines = _matching_klines(buckets)
        klines[min(klines)].open += Decimal("0.00000001")
        assert not compare_ohlc(buckets, klines, _TICK, 0.99).passed
        # A permissive threshold accepts the same data (1/2 matched).
        assert compare_ohlc(buckets, klines, _TICK, 0.5).passed

    def test_whole_hour_shift_fails_on_key_set(self):
        """An hour-shifted tick set fails on bucket-KEY-set mismatch, not values."""
        ticks = _ticks_two_minutes()
        klines = _matching_klines(rebuild_ohlc(ticks))
        shifted = [(ts + timedelta(hours=1), price) for ts, price in ticks]
        result = compare_ohlc(rebuild_ohlc(shifted), klines, _TICK, 0.0)
        assert not result.passed  # even with a zero value-threshold
        assert len(result.extra_keys) == 2  # every bucket outside kline keys
        assert result.compared == 0

    def test_sparse_minutes_are_legitimate(self):
        """Kline minutes without imported ticks do NOT fail the key-set check."""
        buckets = rebuild_ohlc(_ticks_two_minutes())
        klines = _matching_klines(buckets)
        # Source klines cover extra minutes the sparse import never ticked.
        extra_key = max(klines) + 1
        klines[extra_key] = OhlcBucket(
            open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1")
        )
        assert compare_ohlc(buckets, klines, _TICK, 0.99).passed


class TestCoerceKlineTs:
    def test_aware_datetime_to_naive_utc(self):
        """Aware datetimes are converted to naive UTC."""
        aware = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
        assert _coerce_kline_ts(aware) == datetime(2026, 7, 1, 10, 0)

    def test_iso_z_string(self):
        """ISO-8601 Z strings parse to naive UTC."""
        assert _coerce_kline_ts("2026-07-01T10:00:00Z") == datetime(2026, 7, 1, 10, 0)

    def test_epoch_seconds_and_millis(self):
        """Numeric epochs: > 1e12 treated as milliseconds, else seconds."""
        expected = datetime(2026, 7, 1, 10, 0)
        epoch_s = int(expected.replace(tzinfo=timezone.utc).timestamp())
        assert _coerce_kline_ts(epoch_s) == expected
        assert _coerce_kline_ts(epoch_s * 1000) == expected

    def test_unsupported_type_raises(self):
        """Unknown timestamp types are rejected."""
        with pytest.raises(ValueError, match="unsupported kline timestamp"):
            _coerce_kline_ts(object())


def _seed_output_ticks(tmp_path, ticks, symbol="BTCUSDT"):
    db_path = tmp_path / "imported.db"
    db = open_output_db(db_path)
    insert_batch(
        db,
        [
            TickerSnapshot(
                symbol=symbol,
                exchange_ts=ts,
                local_ts=ts,
                last_price=price,
                mark_price=price,
                bid1_price=price,
                ask1_price=price,
                funding_rate=Decimal("0"),
            )
            for ts, price in ticks
        ],
    )
    return db, db_path


def _seed_klines_db(path, buckets):
    """Write a synthetic source klines table matching the given buckets."""
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS klines ("
            "id INTEGER PRIMARY KEY, symbol TEXT, interval TEXT, "
            "start_time DATETIME, open REAL, high REAL, low REAL, close REAL)"
        )
        for key, bucket in buckets.items():
            start = datetime.fromtimestamp(key * 60, tz=timezone.utc).replace(
                tzinfo=None
            )
            conn.execute(
                text(
                    "INSERT INTO klines (symbol, interval, start_time, open, "
                    "high, low, close) VALUES (:s, '1', :t, :o, :h, :l, :c)"
                ),
                {
                    "s": "BTCUSDT",
                    "t": start.isoformat(sep=" "),
                    "o": float(bucket.open),
                    "h": float(bucket.high),
                    "l": float(bucket.low),
                    "c": float(bucket.close),
                },
            )
    engine.dispose()


class TestCheckOhlc:
    def test_end_to_end_pass_against_source_klines(self, tmp_path):
        """check_ohlc passes when source klines match the imported ticks."""
        ticks = _ticks_two_minutes()
        db, _ = _seed_output_ticks(tmp_path, ticks)
        source = tmp_path / "source.db"
        _seed_klines_db(source, rebuild_ohlc(ticks))
        bounds = (ticks[0][0], ticks[-1][0])
        assert check_ohlc(
            db, "BTCUSDT", "db", f"sqlite:///{source}", bounds, 0.99
        )

    def test_unknown_symbol_fails(self, tmp_path):
        """A symbol without a pinned exchange tick_size fails closed."""
        db, _ = _seed_output_ticks(tmp_path, _ticks_two_minutes(), symbol="XXXUSDT")
        bounds = (_T0, _T0 + timedelta(minutes=2))
        assert not check_ohlc(db, "XXXUSDT", "db", "sqlite:///none.db", bounds, 0.99)

    def test_no_ticks_on_sampled_day_fails(self, tmp_path):
        """An empty imported window fails the check."""
        db = open_output_db(tmp_path / "imported.db")
        bounds = (_T0, _T0 + timedelta(minutes=2))
        assert not check_ohlc(db, "BTCUSDT", "db", "sqlite:///none.db", bounds, 0.99)

    def test_empty_midpoint_day_falls_back_to_nearest(self, tmp_path):
        """A collector-outage day at the midpoint samples the nearest tick day."""
        # Ticks only on day 1 and day 5; midpoint (day 3) is empty.
        day1 = _ticks_two_minutes()
        day5 = [(ts + timedelta(days=4), price) for ts, price in day1]
        db, _ = _seed_output_ticks(tmp_path, day1 + day5)
        source = tmp_path / "source.db"
        _seed_klines_db(source, rebuild_ohlc(day1 + day5))
        bounds = (day1[0][0], day5[-1][0])
        assert check_ohlc(
            db, "BTCUSDT", "db", f"sqlite:///{source}", bounds, 0.99
        )


class TestSmokeReplayGuards:
    def test_unknown_symbol_fails_without_subprocess(self, tmp_path):
        """No pinned tick_size -> smoke replay fails closed, no replay spawned."""
        bounds = (_T0, _T0 + timedelta(hours=5))
        assert not smoke_replay(tmp_path / "imported.db", "XXXUSDT", bounds)


class TestSmokeReplayMocked:
    """Contract of the rendered config + result parsing (mocked subprocess)."""

    @staticmethod
    def _fake_run(captured, returncode=0, stdout="Net PnL:    1.23"):
        import yaml

        def fake_run(cmd, capture_output, text):
            captured["cmd"] = cmd
            with open(cmd[-1]) as f:
                captured["config"] = yaml.safe_load(f)
            return SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr=""
            )

        return fake_run

    def test_renders_config_and_passes(self, tmp_path, monkeypatch):
        """Rendered YAML pins db URL, symbol, 4h window, tick_size, run_id null."""
        captured = {}
        monkeypatch.setattr(
            "importer.validate.subprocess.run", self._fake_run(captured)
        )
        db_path = tmp_path / "imported_BTCUSDT.db"
        bounds = (_T0, _T0 + timedelta(hours=6))
        assert smoke_replay(db_path, "BTCUSDT", bounds) is True
        config = captured["config"]
        assert config["database_url"] == f"sqlite:///{db_path}"
        assert config["symbol"] == "BTCUSDT"
        assert config["run_id"] is None
        assert config["strategy"]["tick_size"] == "0.1"
        assert config["fill_simulator"]["mode"] == "last_cross"
        assert config["seed"]["enabled"] is False
        # 4h window anchored at the data's end.
        assert config["start_ts"] == (_T0 + timedelta(hours=2)).isoformat()
        assert config["end_ts"] == (_T0 + timedelta(hours=6)).isoformat()
        assert captured["cmd"][1:3] == ["-m", "replay.main"]

    def test_nan_metric_fails(self, tmp_path, monkeypatch):
        """A NaN in the replay summary fails the smoke check."""
        monkeypatch.setattr(
            "importer.validate.subprocess.run",
            self._fake_run({}, stdout="Net PnL:    nan"),
        )
        bounds = (_T0, _T0 + timedelta(hours=6))
        assert smoke_replay(tmp_path / "x.db", "BTCUSDT", bounds) is False

    def test_infinite_metric_fails(self, tmp_path, monkeypatch):
        """An inf Net PnL fails the smoke check (finite required)."""
        monkeypatch.setattr(
            "importer.validate.subprocess.run",
            self._fake_run({}, stdout="Net PnL:    inf"),
        )
        bounds = (_T0, _T0 + timedelta(hours=6))
        assert smoke_replay(tmp_path / "x.db", "BTCUSDT", bounds) is False

    def test_missing_metric_fails(self, tmp_path, monkeypatch):
        """A zero-exit run WITHOUT a parsable Net PnL metric fails."""
        monkeypatch.setattr(
            "importer.validate.subprocess.run",
            self._fake_run({}, stdout="replay finished\n"),
        )
        bounds = (_T0, _T0 + timedelta(hours=6))
        assert smoke_replay(tmp_path / "x.db", "BTCUSDT", bounds) is False

    def test_nonzero_exit_fails(self, tmp_path, monkeypatch):
        """A replay crash (non-zero exit) fails the smoke check."""
        monkeypatch.setattr(
            "importer.validate.subprocess.run",
            self._fake_run({}, returncode=2),
        )
        bounds = (_T0, _T0 + timedelta(hours=6))
        assert smoke_replay(tmp_path / "x.db", "BTCUSDT", bounds) is False


class TestRecorderOverlapProbe:
    def test_skipped_without_recorder_db(self, tmp_path):
        """No --recorder-db -> NOTICE skip, never gates."""
        db, _ = _seed_output_ticks(tmp_path, _ticks_two_minutes())
        bounds = (_T0, _T0 + timedelta(minutes=2))
        assert recorder_overlap_probe(db, "BTCUSDT", bounds, None) is True

    def test_skipped_when_no_overlap(self, tmp_path):
        """Disjoint recorder coverage -> NOTICE skip."""
        db, _ = _seed_output_ticks(tmp_path, _ticks_two_minutes())
        rec_ticks = [
            (_T0 + timedelta(days=30), Decimal("100.00000000")),
        ]
        _, rec_path = _seed_output_ticks(tmp_path / "rec", rec_ticks)
        bounds = (_T0, _T0 + timedelta(minutes=2))
        assert recorder_overlap_probe(db, "BTCUSDT", bounds, str(rec_path)) is True

    def test_reports_on_overlap(self, tmp_path):
        """Overlapping coverage runs the diff and returns True (informational)."""
        ticks = _ticks_two_minutes()
        db, _ = _seed_output_ticks(tmp_path, ticks)
        _, rec_path = _seed_output_ticks(tmp_path / "rec", ticks)
        bounds = (ticks[0][0], ticks[-1][0])
        assert recorder_overlap_probe(db, "BTCUSDT", bounds, str(rec_path)) is True
