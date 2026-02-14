"""Tests for comparator.equity module."""

import csv
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from grid_db import (
    DatabaseFactory,
    WalletSnapshot,
    User,
    BybitAccount,
)

from comparator.equity import EquityComparator


class TestLoadLiveEquity:
    """Tests for loading live equity from WalletSnapshot."""

    def _seed_account(self, db: DatabaseFactory):
        """Seed database with user and account."""
        with db.get_session() as session:
            user = User(user_id="u1", username="test", email="t@t.com")
            session.add(user)
            session.flush()

            account = BybitAccount(
                account_id="acc1", user_id="u1",
                account_name="main", environment="testnet",
            )
            session.add(account)
            session.flush()

    def _add_snapshot(self, db, ts, balance):
        """Add a wallet snapshot."""
        with db.get_session() as session:
            snap = WalletSnapshot(
                account_id="acc1",
                exchange_ts=ts,
                local_ts=ts,
                coin="USDT",
                wallet_balance=balance,
                available_balance=balance,
            )
            session.add(snap)

    def test_load_live_equity(self, db):
        """Loads wallet snapshots as equity points."""
        self._seed_account(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self._add_snapshot(db, ts, Decimal("10000"))
        self._add_snapshot(db, ts + timedelta(hours=1), Decimal("10050"))
        self._add_snapshot(db, ts + timedelta(hours=2), Decimal("10030"))

        eq = EquityComparator()
        with db.get_session() as session:
            points = eq.load_live(
                session, "acc1", "USDT",
                ts - timedelta(hours=1), ts + timedelta(hours=3),
            )

        assert len(points) == 3
        assert points[0][1] == Decimal("10000")
        assert points[2][1] == Decimal("10030")

    def test_empty_range(self, db):
        """No snapshots in range returns empty list."""
        self._seed_account(db)

        eq = EquityComparator()
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        with db.get_session() as session:
            points = eq.load_live(
                session, "acc1", "USDT", ts, ts + timedelta(hours=1),
            )

        assert points == []


class TestLoadBacktestEquity:
    """Tests for loading backtest equity from CSV and session."""

    def test_load_from_csv(self, tmp_path):
        """Loads equity curve from CSV."""
        csv_path = tmp_path / "equity.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "equity", "return_pct"])
            writer.writerow(["2025-01-15T12:00:00+00:00", "10000", "0.0000"])
            writer.writerow(["2025-01-15T13:00:00+00:00", "10050", "0.5000"])

        eq = EquityComparator()
        points = eq.load_backtest_from_csv(csv_path)

        assert len(points) == 2
        assert points[0][1] == Decimal("10000")
        assert points[1][1] == Decimal("10050")

    def test_csv_aware_timestamps_normalized_to_naive(self, tmp_path):
        """Equity CSV with aware timestamps produces naive UTC points."""
        csv_path = tmp_path / "equity.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "equity", "return_pct"])
            writer.writerow(["2025-01-15T12:00:00+00:00", "10000", "0.0000"])
            writer.writerow(["2025-01-15T17:00:00+05:00", "10050", "0.5000"])

        eq = EquityComparator()
        points = eq.load_backtest_from_csv(csv_path)

        for ts, _ in points:
            assert ts.tzinfo is None
        assert points[0][0] == datetime(2025, 1, 15, 12, 0, 0)
        assert points[1][0] == datetime(2025, 1, 15, 12, 0, 0)

    def test_load_from_session(self):
        """Loads equity curve from session data."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        curve = [
            (ts + timedelta(hours=1), Decimal("10050")),
            (ts, Decimal("10000")),
        ]

        eq = EquityComparator()
        points = eq.load_backtest_from_session(curve)

        # Should be sorted by timestamp
        assert points[0][1] == Decimal("10000")
        assert points[1][1] == Decimal("10050")


class TestResampleToCommonGrid:
    """Tests for resampling equity curves."""

    def test_basic_resampling(self):
        """Resamples both curves to common grid."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        live = [
            (ts, Decimal("10000")),
            (ts + timedelta(minutes=30), Decimal("10010")),
            (ts + timedelta(hours=1, minutes=15), Decimal("10020")),
        ]
        bt = [
            (ts + timedelta(minutes=10), Decimal("10000")),
            (ts + timedelta(hours=1, minutes=30), Decimal("10025")),
        ]

        eq = EquityComparator()
        resampled = eq.resample(live, bt, interval=timedelta(hours=1))

        assert len(resampled) == 2
        # First bucket: live has 10010 (last in bucket), bt has 10000
        assert resampled[0][1] == Decimal("10010")
        assert resampled[0][2] == Decimal("10000")

    def test_empty_inputs(self):
        """Empty inputs return empty list."""
        eq = EquityComparator()
        assert eq.resample([], []) == []

    def test_one_side_empty(self):
        """One empty curve still resamples the other."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        live = [(ts, Decimal("10000"))]

        eq = EquityComparator()
        resampled = eq.resample(live, [], interval=timedelta(hours=1))

        assert len(resampled) == 1
        assert resampled[0][1] == Decimal("10000")
        assert resampled[0][2] is None


class TestComputeEquityMetrics:
    """Tests for equity divergence metrics."""

    def test_identical_curves(self):
        """Identical curves have zero divergence."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        resampled = [
            (ts, Decimal("10000"), Decimal("10000")),
            (ts + timedelta(hours=1), Decimal("10050"), Decimal("10050")),
        ]

        eq = EquityComparator()
        max_div, mean_div, corr = eq.compute_metrics(resampled)

        assert max_div == Decimal("0")
        assert mean_div == Decimal("0")
        assert corr == 1.0

    def test_divergent_curves(self):
        """Computes divergence correctly."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        resampled = [
            (ts, Decimal("10000"), Decimal("10010")),
            (ts + timedelta(hours=1), Decimal("10050"), Decimal("10070")),
        ]

        eq = EquityComparator()
        max_div, mean_div, corr = eq.compute_metrics(resampled)

        assert max_div == Decimal("20")
        assert mean_div == Decimal("15")  # (10 + 20) / 2

    def test_partial_data(self):
        """Rows with None values are skipped."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        resampled = [
            (ts, Decimal("10000"), None),
            (ts + timedelta(hours=1), Decimal("10050"), Decimal("10050")),
        ]

        eq = EquityComparator()
        max_div, mean_div, corr = eq.compute_metrics(resampled)

        # Only one valid pair
        assert max_div == Decimal("0")

    def test_empty_resampled(self):
        """Empty resampled returns zeros."""
        eq = EquityComparator()
        max_div, mean_div, corr = eq.compute_metrics([])

        assert max_div == Decimal("0")
        assert corr == 0.0


class TestExportEquityComparison:
    """Tests for equity CSV export."""

    def test_export_csv(self, tmp_path):
        """Exports resampled data to CSV."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        resampled = [
            (ts, Decimal("10000"), Decimal("10010")),
            (ts + timedelta(hours=1), Decimal("10050"), None),
        ]

        eq = EquityComparator()
        path = tmp_path / "equity.csv"
        eq.export(resampled, path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["live_equity"] == "10000"
        assert rows[0]["backtest_equity"] == "10010"
        assert rows[0]["divergence"] == "10"
        assert rows[1]["backtest_equity"] == ""
        assert rows[1]["divergence"] == ""

    def test_creates_parent_dirs(self, tmp_path):
        """Creates parent directories if missing."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        resampled = [(ts, Decimal("10000"), Decimal("10000"))]

        eq = EquityComparator()
        path = tmp_path / "sub" / "dir" / "equity.csv"
        eq.export(resampled, path)

        assert path.exists()
