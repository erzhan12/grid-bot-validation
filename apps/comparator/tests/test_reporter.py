"""Tests for comparator.reporter module."""

import csv
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from comparator.matcher import MatchedTrade, MatchResult
from comparator.metrics import calculate_metrics
from comparator.reporter import ComparatorReporter


class TestComparatorReporter:
    """Tests for ComparatorReporter."""

    @pytest.fixture
    def reporter(self, sample_match_result):
        """Reporter with sample data."""
        metrics = calculate_metrics(sample_match_result)
        return ComparatorReporter(sample_match_result, metrics)

    def test_export_matched_trades(self, reporter, tmp_path):
        """Matched trades CSV has correct columns and row count."""
        path = tmp_path / "matched.csv"
        reporter.export_matched_trades(path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert "client_order_id" in rows[0]
        assert "live_price" in rows[0]
        assert "backtest_price" in rows[0]
        assert "price_delta" in rows[0]
        assert "pnl_delta" in rows[0]

    def test_export_unmatched_trades(self, reporter, tmp_path):
        """Unmatched trades CSV includes live-only and backtest-only."""
        path = tmp_path / "unmatched.csv"
        reporter.export_unmatched_trades(path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2  # 1 live-only + 1 backtest-only
        sources = {r["source"] for r in rows}
        assert sources == {"live_only", "backtest_only"}

    def test_export_metrics(self, reporter, tmp_path):
        """Metrics CSV has metric/value format."""
        path = tmp_path / "metrics.csv"
        reporter.export_metrics(path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        metrics_dict = {r["metric"]: r["value"] for r in rows}
        assert "matched_count" in metrics_dict
        assert metrics_dict["matched_count"] == "2"
        assert "match_rate" in metrics_dict
        assert "pnl_correlation" in metrics_dict

    def test_export_all(self, reporter, tmp_path):
        """export_all creates all expected files."""
        paths = reporter.export_all(tmp_path)

        assert "matched_trades" in paths
        assert "unmatched_trades" in paths
        assert "validation_metrics" in paths

        for path in paths.values():
            assert path.exists()

    def test_print_summary(self, reporter, capsys):
        """print_summary outputs to stdout without error."""
        reporter.print_summary()
        captured = capsys.readouterr()

        assert "BACKTEST vs LIVE COMPARISON" in captured.out
        assert "Match rate:" in captured.out
        assert "PnL COMPARISON" in captured.out

    def test_export_creates_parent_dirs(self, reporter, tmp_path):
        """Export creates parent directories if they don't exist."""
        nested = tmp_path / "deep" / "nested" / "dir"
        reporter.export_metrics(nested / "metrics.csv")

        assert (nested / "metrics.csv").exists()

    def test_export_all_includes_equity_when_present(self, sample_match_result, tmp_path):
        """export_all includes equity_comparison.csv when equity data provided."""
        metrics = calculate_metrics(sample_match_result)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        equity_data = [
            (ts, Decimal("10000"), Decimal("10010")),
            (ts + timedelta(hours=1), Decimal("10050"), Decimal("10070")),
        ]

        reporter = ComparatorReporter(sample_match_result, metrics, equity_data=equity_data)
        paths = reporter.export_all(tmp_path)

        assert "equity_comparison" in paths
        assert paths["equity_comparison"].exists()

        # Verify CSV content
        with open(paths["equity_comparison"]) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["live_equity"] == "10000"
        assert rows[0]["divergence"] == "10"

    def test_export_all_no_equity_without_data(self, sample_match_result, tmp_path):
        """export_all omits equity when no equity data provided."""
        metrics = calculate_metrics(sample_match_result)
        reporter = ComparatorReporter(sample_match_result, metrics)
        paths = reporter.export_all(tmp_path)

        assert "equity_comparison" not in paths

    def test_export_matched_trades_with_reused_ids(self, make_trade, ts, tmp_path):
        """Reused client_order_id rows each get their correct delta in CSV."""
        # Two matched pairs with the same client_order_id but different occurrences
        live_0 = make_trade(
            client_order_id="reused", price=Decimal("100"), qty=Decimal("1"),
            source="live", timestamp=ts, direction="long",
        )
        live_0.occurrence = 0
        bt_0 = make_trade(
            client_order_id="reused", price=Decimal("100"), qty=Decimal("1"),
            source="backtest", timestamp=ts, direction="long",
        )
        bt_0.occurrence = 0

        live_1 = make_trade(
            client_order_id="reused", price=Decimal("200"), qty=Decimal("2"),
            source="live", timestamp=ts + timedelta(hours=1), direction="long",
        )
        live_1.occurrence = 1
        bt_1 = make_trade(
            client_order_id="reused", price=Decimal("205"), qty=Decimal("2.1"),
            source="backtest", timestamp=ts + timedelta(hours=1), direction="long",
        )
        bt_1.occurrence = 1

        match_result = MatchResult(
            matched=[
                MatchedTrade(live=live_0, backtest=bt_0),
                MatchedTrade(live=live_1, backtest=bt_1),
            ],
            live_only=[], backtest_only=[],
        )
        metrics = calculate_metrics(match_result)
        reporter = ComparatorReporter(match_result, metrics)

        path = tmp_path / "matched.csv"
        reporter.export_matched_trades(path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        # First occurrence: prices match (delta=0), occurrence=0
        assert rows[0]["occurrence"] == "0"
        assert rows[0]["price_delta"] == "0"
        # Second occurrence: 205-200=5, occurrence=1
        assert rows[1]["occurrence"] == "1"
        assert rows[1]["price_delta"] == "5"

    def test_export_unmatched_includes_occurrence(self, make_trade, ts, tmp_path):
        """Unmatched trades CSV includes occurrence column."""
        live_0 = make_trade(client_order_id="a", source="live", timestamp=ts)
        live_0.occurrence = 0
        live_1 = make_trade(client_order_id="a", source="live",
                            timestamp=ts + timedelta(hours=1))
        live_1.occurrence = 1

        match_result = MatchResult(
            matched=[], live_only=[live_0, live_1], backtest_only=[],
        )
        metrics = calculate_metrics(match_result)
        reporter = ComparatorReporter(match_result, metrics)

        path = tmp_path / "unmatched.csv"
        reporter.export_unmatched_trades(path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["occurrence"] == "0"
        assert rows[1]["occurrence"] == "1"
