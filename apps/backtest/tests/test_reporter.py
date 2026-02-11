"""Tests for BacktestReporter."""

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.reporter import BacktestReporter
from backtest.session import BacktestSession, BacktestTrade


class TestBacktestReporter:
    """Tests for BacktestReporter."""

    @pytest.fixture
    def session_with_data(self) -> BacktestSession:
        """Create a session with trades and equity data."""
        session = BacktestSession(initial_balance=Decimal("10000"))

        # Add trades
        trade1 = BacktestTrade(
            trade_id="t1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("50000"),
            qty=Decimal("0.1"),
            direction="long",
            timestamp=datetime(2025, 1, 1, 10, 0),
            order_id="o1",
            client_order_id="c1",
            realized_pnl=Decimal("0"),
            commission=Decimal("5"),
            strat_id="strat1",
        )
        trade2 = BacktestTrade(
            trade_id="t2",
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("51000"),
            qty=Decimal("0.1"),
            direction="long",
            timestamp=datetime(2025, 1, 1, 12, 0),
            order_id="o2",
            client_order_id="c2",
            realized_pnl=Decimal("100"),
            commission=Decimal("5.1"),
            strat_id="strat1",
        )
        session.record_trade(trade1)
        session.record_trade(trade2)

        # Add equity points
        session.update_equity(datetime(2025, 1, 1, 10, 0), Decimal("0"))
        session.update_equity(datetime(2025, 1, 1, 11, 0), Decimal("50"))
        session.update_equity(datetime(2025, 1, 1, 12, 0), Decimal("90"))

        # Finalize
        session.finalize(Decimal("0"))

        return session

    def test_init_finalizes_session(self):
        """Reporter should finalize session if not already done."""
        session = BacktestSession(initial_balance=Decimal("1000"))
        assert session.metrics is None

        reporter = BacktestReporter(session)
        assert reporter.metrics is not None

    def test_export_trades(self, session_with_data, tmp_path):
        """Should export trades to CSV."""
        reporter = BacktestReporter(session_with_data)
        output_path = tmp_path / "trades.csv"

        reporter.export_trades(output_path)

        assert output_path.exists()
        with open(output_path) as f:
            reader = csv.reader(f)
            rows = list(reader)

        # Header + 2 trades
        assert len(rows) == 3
        assert rows[0][0] == "trade_id"
        assert rows[1][0] == "t1"
        assert rows[2][0] == "t2"

    def test_export_trades_creates_parent_dirs(self, session_with_data, tmp_path):
        """Should create parent directories if needed."""
        reporter = BacktestReporter(session_with_data)
        output_path = tmp_path / "nested" / "dir" / "trades.csv"

        reporter.export_trades(output_path)

        assert output_path.exists()

    def test_export_equity_curve(self, session_with_data, tmp_path):
        """Should export equity curve to CSV."""
        reporter = BacktestReporter(session_with_data)
        output_path = tmp_path / "equity.csv"

        reporter.export_equity_curve(output_path)

        assert output_path.exists()
        with open(output_path) as f:
            reader = csv.reader(f)
            rows = list(reader)

        # Header + 3 equity points
        assert len(rows) == 4
        assert rows[0] == ["timestamp", "equity", "return_pct"]

    def test_export_metrics(self, session_with_data, tmp_path):
        """Should export metrics summary to CSV."""
        reporter = BacktestReporter(session_with_data)
        output_path = tmp_path / "metrics.csv"

        reporter.export_metrics(output_path)

        assert output_path.exists()
        with open(output_path) as f:
            reader = csv.reader(f)
            rows = list(reader)

        # Header + metrics rows
        assert len(rows) > 10
        assert rows[0] == ["metric", "value"]

        # Check some metrics are present
        metrics_dict = {row[0]: row[1] for row in rows[1:]}
        assert "initial_balance" in metrics_dict
        assert "final_balance" in metrics_dict
        assert "return_pct" in metrics_dict
        assert "sharpe_ratio" in metrics_dict
        assert "turnover" in metrics_dict

    def test_export_all(self, session_with_data, tmp_path):
        """Should export all data to directory."""
        reporter = BacktestReporter(session_with_data)
        output_dir = tmp_path / "output"

        paths = reporter.export_all(output_dir)

        assert "trades" in paths
        assert "equity_curve" in paths
        assert "metrics" in paths

        for path in paths.values():
            assert path.exists()

    def test_export_all_with_prefix(self, session_with_data, tmp_path):
        """Should use prefix in file names."""
        reporter = BacktestReporter(session_with_data)
        output_dir = tmp_path / "output"

        paths = reporter.export_all(output_dir, prefix="test")

        for path in paths.values():
            assert path.name.startswith("test_")

    def test_get_summary_dict(self, session_with_data):
        """Should return metrics as dictionary."""
        reporter = BacktestReporter(session_with_data)

        summary = reporter.get_summary_dict()

        assert isinstance(summary, dict)
        assert summary["total_trades"] == 2
        assert summary["initial_balance"] == 10000.0
        assert "return_pct" in summary
        assert "sharpe_ratio" in summary
        assert "turnover" in summary
        assert "long_profit_factor" in summary
        assert "short_profit_factor" in summary
