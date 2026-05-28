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
        assert "meta.fill_mode" not in metrics_dict

    def test_export_metrics_includes_cur_realised_pnl_final_delta(
        self, sample_match_result, tmp_path,
    ):
        """0056: export_metrics emits cur_realised_pnl_final_delta row."""
        metrics = calculate_metrics(sample_match_result)
        metrics.cur_realised_pnl_final_delta = Decimal("3.25")
        reporter = ComparatorReporter(sample_match_result, metrics)
        path = tmp_path / "metrics.csv"
        reporter.export_metrics(path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        metrics_dict = {r["metric"]: r["value"] for r in rows}
        assert metrics_dict["cur_realised_pnl_final_delta"] == "3.25"

    def test_print_summary_includes_cur_realised_final(
        self, sample_match_result, capsys,
    ):
        """0056: print_summary surfaces the cur_realised_pnl_final_delta line."""
        metrics = calculate_metrics(sample_match_result)
        metrics.cur_realised_pnl_final_delta = Decimal("-0.75")
        reporter = ComparatorReporter(sample_match_result, metrics)
        reporter.print_summary()
        captured = capsys.readouterr()
        assert "Cur realised final" in captured.out
        assert "-0.75" in captured.out

    def test_export_metrics_includes_0059_rows(
        self, sample_match_result, tmp_path,
    ):
        """0059: export_metrics emits the nine new per-snapshot rows."""
        metrics = calculate_metrics(sample_match_result)
        metrics.upnl_usdt_mean_abs_delta = Decimal("1.1")
        metrics.upnl_usdt_max_abs_delta = Decimal("2.2")
        metrics.cur_realised_usdt_mean_abs_delta = Decimal("3.3")
        metrics.cur_realised_usdt_max_abs_delta = Decimal("4.4")
        metrics.cum_realised_usdt_mean_abs_delta = Decimal("5.5")
        metrics.cum_realised_usdt_max_abs_delta = Decimal("6.6")
        metrics.pos_value_usdt_mean_abs_delta = Decimal("7.7")
        metrics.pos_value_usdt_max_abs_delta = Decimal("8.8")
        metrics.pos_value_final_delta = Decimal("9.9")
        reporter = ComparatorReporter(sample_match_result, metrics)
        path = tmp_path / "metrics.csv"
        reporter.export_metrics(path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        d = {r["metric"]: r["value"] for r in rows}
        assert d["upnl_usdt_mean_abs_delta"] == "1.1"
        assert d["upnl_usdt_max_abs_delta"] == "2.2"
        assert d["cur_realised_usdt_mean_abs_delta"] == "3.3"
        assert d["cur_realised_usdt_max_abs_delta"] == "4.4"
        assert d["cum_realised_usdt_mean_abs_delta"] == "5.5"
        assert d["cum_realised_usdt_max_abs_delta"] == "6.6"
        assert d["pos_value_usdt_mean_abs_delta"] == "7.7"
        assert d["pos_value_usdt_max_abs_delta"] == "8.8"
        assert d["pos_value_final_delta"] == "9.9"
        # Pre-existing final-delta rows must remain distinct and unchanged.
        assert "cur_realised_pnl_final_delta" in d
        assert "cum_realised_pnl_final_delta" in d

    def test_print_summary_includes_0059_lines(
        self, sample_match_result, capsys,
    ):
        """0059: print_summary surfaces the new per-snapshot lines, distinctly."""
        metrics = calculate_metrics(sample_match_result)
        metrics.upnl_usdt_mean_abs_delta = Decimal("1.1")
        metrics.pos_value_final_delta = Decimal("9.9")
        reporter = ComparatorReporter(sample_match_result, metrics)
        reporter.print_summary()
        out = capsys.readouterr().out
        assert "Upnl mean |delta|" in out
        assert "Upnl max |delta|" in out
        assert "Cur realised mean |delta|" in out
        assert "Cum realised mean |delta|" in out
        assert "Pos value mean |delta|" in out
        assert "Pos value max |delta|" in out
        assert "Pos value final" in out
        # Labels must remain distinct from the pre-existing final-only lines.
        assert "Cur realised final" in out
        assert "Cum realised final" in out

    def test_export_metrics_includes_metadata(self, sample_match_result, tmp_path):
        """Metrics CSV includes optional metadata with meta. prefix."""
        metrics = calculate_metrics(sample_match_result)
        reporter = ComparatorReporter(
            sample_match_result,
            metrics,
            metadata={"fill_mode": "book_touch"},
        )
        path = tmp_path / "metrics.csv"

        reporter.export_metrics(path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        metrics_dict = {r["metric"]: r["value"] for r in rows}
        assert metrics_dict["meta.fill_mode"] == "book_touch"

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

    def test_export_position_comparison_writes_expected_columns(
        self, sample_match_result, tmp_path,
    ):
        """0034: export_position_comparison writes the documented columns."""
        from grid_db.models import PositionSnapshot
        from comparator.position_metrics import PositionComparator

        metrics = calculate_metrics(sample_match_result)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        def _snap(side, source):
            return PositionSnapshot(
                run_id="r", account_id="a", symbol="BTCUSDT",
                exchange_ts=ts, local_ts=ts, side=side,
                size=Decimal("1"), entry_price=Decimal("100"),
                liq_price=Decimal("90"), unrealised_pnl=Decimal("0"),
                source=source, mark_price=Decimal("101"),
                position_im=Decimal("10"), position_mm=Decimal("0.5"),
                cum_realised_pnl=Decimal("5"),
                position_value=Decimal("100"),
            )

        pairs = PositionComparator().pair_and_compare(
            [_snap("Buy", "live")], [_snap("Buy", "backtest")],
        )
        reporter = ComparatorReporter(
            sample_match_result, metrics, position_pairs=pairs,
        )
        out = tmp_path / "position_comparison.csv"
        reporter.export_position_comparison(out)

        with open(out) as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
            rows = list(reader)
        assert "im_delta" in header
        assert "mm_delta" in header
        assert "liq_delta" in header
        assert "unrealised_delta" in header
        assert "cum_realised_delta" in header
        # 0056: cycle-scoped realized PnL columns.
        assert "live_cur_realised" in header
        assert "bt_cur_realised" in header
        assert "cur_realised_delta" in header
        # 0059: stored upnl parity + position value columns.
        assert "live_upnl_usdt" in header
        assert "bt_upnl_usdt" in header
        assert "upnl_usdt_delta" in header
        assert "live_position_value" in header
        assert "bt_position_value" in header
        assert "pos_value_delta" in header
        assert len(rows) == 1
        assert rows[0]["side"] == "Buy"

    def test_export_all_includes_position_comparison_when_pairs_present(
        self, sample_match_result, tmp_path,
    ):
        """0034: export_all adds position_comparison.csv when pairs exist."""
        from grid_db.models import PositionSnapshot
        from comparator.position_metrics import PositionComparator

        metrics = calculate_metrics(sample_match_result)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        snap = PositionSnapshot(
            run_id="r", account_id="a", symbol="BTCUSDT",
            exchange_ts=ts, local_ts=ts, side="Buy",
            size=Decimal("1"), entry_price=Decimal("100"),
            liq_price=Decimal("90"), unrealised_pnl=Decimal("0"),
            source="live", mark_price=Decimal("101"),
            position_im=Decimal("10"), position_mm=Decimal("0.5"),
            cum_realised_pnl=Decimal("5"),
        )
        snap_bt = PositionSnapshot(
            run_id="r", account_id="a", symbol="BTCUSDT",
            exchange_ts=ts, local_ts=ts, side="Buy",
            size=Decimal("1"), entry_price=Decimal("100"),
            liq_price=Decimal("90"), unrealised_pnl=Decimal("0"),
            source="backtest", mark_price=Decimal("101"),
            position_im=Decimal("10"), position_mm=Decimal("0.5"),
            cum_realised_pnl=Decimal("5"),
        )
        pairs = PositionComparator().pair_and_compare([snap], [snap_bt])
        reporter = ComparatorReporter(
            sample_match_result, metrics, position_pairs=pairs,
        )
        paths = reporter.export_all(tmp_path)
        assert "position_comparison" in paths
        assert paths["position_comparison"].exists()

    def test_export_all_omits_position_comparison_when_no_pairs(
        self, sample_match_result, tmp_path,
    ):
        """0034: export_all omits position_comparison.csv when no matched pairs."""
        metrics = calculate_metrics(sample_match_result)
        reporter = ComparatorReporter(
            sample_match_result, metrics, position_pairs=[],
        )
        paths = reporter.export_all(tmp_path)
        assert "position_comparison" not in paths

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


class TestCsvInjectionSanitization:
    """Metadata values that look like spreadsheet formulas must be neutered."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("=cmd|'/c calc'!A1", "'=cmd|'/c calc'!A1"),
            ("+SUM(A1:A2)", "'+SUM(A1:A2)"),
            ("-1+1", "'-1+1"),
            ("@SUM(A1)", "'@SUM(A1)"),
            ("strict_cross", "strict_cross"),  # unchanged
            ("", ""),  # empty stays empty
        ],
    )
    def test_meta_value_sanitization(
        self, sample_match_result, tmp_path, raw, expected
    ):
        metrics = calculate_metrics(sample_match_result)
        reporter = ComparatorReporter(
            sample_match_result, metrics, metadata={"injected": raw}
        )

        path = tmp_path / "metrics.csv"
        reporter.export_metrics(path)

        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        metrics_dict = {r["metric"]: r["value"] for r in rows}
        assert metrics_dict["meta.injected"] == expected
