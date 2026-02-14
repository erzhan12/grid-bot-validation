"""Tests for comparator.main CLI module."""

import csv
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch

from grid_db import (
    DatabaseFactory,
    DatabaseSettings,
    PrivateExecution,
    User,
    BybitAccount,
    Strategy,
    Run,
    WalletSnapshot,
)

from gridcore.position import DirectionType, SideType

from comparator.config import ComparatorConfig
from comparator.loader import NormalizedTrade
from comparator.main import parse_args, _parse_datetime, run, main


class TestParseArgs:
    """Tests for CLI argument parsing."""

    def test_required_args_with_csv(self):
        """Parses required args in CSV mode."""
        args = parse_args([
            "--run-id", "run_1",
            "--backtest-trades", "trades.csv",
            "--start", "2025-01-01",
            "--end", "2025-01-31",
        ])

        assert args.run_id == "run_1"
        assert args.backtest_trades == "trades.csv"
        assert args.backtest_config is None
        assert args.start == "2025-01-01"
        assert args.end == "2025-01-31"

    def test_required_args_with_config(self):
        """Parses required args in config mode."""
        args = parse_args([
            "--run-id", "run_1",
            "--backtest-config", "backtest.yaml",
            "--start", "2025-01-01",
            "--end", "2025-01-31",
        ])

        assert args.backtest_config == "backtest.yaml"
        assert args.backtest_trades is None

    def test_mutually_exclusive(self):
        """Cannot specify both --backtest-trades and --backtest-config."""
        with pytest.raises(SystemExit):
            parse_args([
                "--run-id", "run_1",
                "--backtest-trades", "trades.csv",
                "--backtest-config", "backtest.yaml",
                "--start", "2025-01-01",
                "--end", "2025-01-31",
            ])

    def test_missing_backtest_source_exits(self):
        """Must specify one of --backtest-trades or --backtest-config."""
        with pytest.raises(SystemExit):
            parse_args([
                "--run-id", "run_1",
                "--start", "2025-01-01",
                "--end", "2025-01-31",
            ])

    def test_optional_defaults(self):
        """Optional args have correct defaults."""
        args = parse_args([
            "--run-id", "run_1",
            "--backtest-trades", "trades.csv",
            "--start", "2025-01-01",
            "--end", "2025-01-31",
        ])

        assert args.symbol is None
        assert args.database_url == "sqlite:///gridbot.db"
        assert args.output == "results/comparison"
        assert args.backtest_equity is None
        assert args.coin == "USDT"
        assert args.debug is False

    def test_all_optional_args(self):
        """All optional args can be overridden."""
        args = parse_args([
            "--run-id", "run_1",
            "--backtest-trades", "trades.csv",
            "--start", "2025-01-01",
            "--end", "2025-01-31",
            "--symbol", "ETHUSDT",
            "--database-url", "postgresql://localhost/grid",
            "--output", "/tmp/results",
            "--backtest-equity", "equity.csv",
            "--coin", "BTC",
            "--debug",
        ])

        assert args.symbol == "ETHUSDT"
        assert args.database_url == "postgresql://localhost/grid"
        assert args.output == "/tmp/results"
        assert args.backtest_equity == "equity.csv"
        assert args.coin == "BTC"
        assert args.debug is True

    def test_missing_required_exits(self):
        """Missing --run-id exits."""
        with pytest.raises(SystemExit):
            parse_args([
                "--backtest-trades", "trades.csv",
                "--start", "2025-01-01",
                "--end", "2025-01-31",
            ])


class TestParseDatetime:
    """Tests for _parse_datetime helper."""

    def test_date_only(self):
        """Date-only string parsed to midnight UTC."""
        dt = _parse_datetime("2025-01-15")
        assert dt == datetime(2025, 1, 15, 0, 0, 0, tzinfo=timezone.utc)

    def test_iso_with_time(self):
        """ISO datetime with time preserved."""
        dt = _parse_datetime("2025-01-15T14:30:00")
        assert dt == datetime(2025, 1, 15, 14, 30, 0, tzinfo=timezone.utc)

    def test_iso_with_timezone_normalized_to_utc(self):
        """ISO datetime with non-UTC timezone is normalized to UTC."""
        dt = _parse_datetime("2025-01-15T14:30:00+05:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)
        assert dt == datetime(2025, 1, 15, 9, 30, 0, tzinfo=timezone.utc)

    def test_end_of_day_date_only(self):
        """Date-only with end_of_day=True set to 23:59:59.999999."""
        dt = _parse_datetime("2025-01-15", end_of_day=True)
        assert dt.hour == 23
        assert dt.minute == 59
        assert dt.second == 59
        assert dt.microsecond == 999999

    def test_end_of_day_with_time_preserved(self):
        """ISO with time is NOT modified by end_of_day=True."""
        dt = _parse_datetime("2025-01-15T14:30:00", end_of_day=True)
        assert dt.hour == 14
        assert dt.minute == 30

    def test_adds_utc_if_no_timezone(self):
        """Naive datetime gets UTC timezone."""
        dt = _parse_datetime("2025-01-15")
        assert dt.tzinfo == timezone.utc


class TestRun:
    """Integration tests for run() function."""

    @pytest.fixture
    def db(self):
        """Create fresh in-memory database."""
        settings = DatabaseSettings(db_type="sqlite", db_name=":memory:", echo_sql=False)
        database = DatabaseFactory(settings)
        database.create_tables()
        yield database
        database.drop_tables()

    def _seed_live_data(self, db: DatabaseFactory):
        """Seed database with user, account, strategy, run, and executions.

        Returns naive base timestamp (SQLite strips timezone info).
        """
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        with db.get_session() as session:
            session.add(User(user_id="u1", username="test", email="t@t.com"))
            session.flush()
            session.add(BybitAccount(
                account_id="acc1", user_id="u1",
                account_name="main", environment="testnet",
            ))
            session.flush()
            session.add(Strategy(
                strategy_id="s1", account_id="acc1",
                strategy_type="GridStrategy", symbol="BTCUSDT",
                config_json={"grid_step": 0.2},
            ))
            session.flush()
            session.add(Run(
                run_id="run_1", user_id="u1", account_id="acc1",
                strategy_id="s1", run_type="live",
            ))
            session.flush()
            session.add(PrivateExecution(
                run_id="run_1", account_id="acc1", symbol="BTCUSDT",
                exec_id="e1", order_id="oid_1", order_link_id="order_a",
                exchange_ts=ts, side="Buy",
                exec_price=Decimal("100000"), exec_qty=Decimal("0.001"),
                exec_fee=Decimal("0.02"), closed_pnl=Decimal("0"),
            ))
            session.add(PrivateExecution(
                run_id="run_1", account_id="acc1", symbol="BTCUSDT",
                exec_id="e2", order_id="oid_2", order_link_id="order_b",
                exchange_ts=ts + timedelta(hours=1), side="Sell",
                exec_price=Decimal("100200"), exec_qty=Decimal("0.001"),
                exec_fee=Decimal("0.02"), closed_pnl=Decimal("0.2"),
            ))
        return ts

    def test_run_returns_0_on_success(self, db, tmp_path):
        """run() returns 0 and produces CSV output on successful comparison."""
        ts = self._seed_live_data(db)
        # SQLite strips timezone — use naive timestamps for config and trades
        ts_naive = ts.replace(tzinfo=None)

        config = ComparatorConfig(
            run_id="run_1",
            database_url="sqlite:///:memory:",
            start_ts=ts_naive - timedelta(hours=1),
            end_ts=ts_naive + timedelta(hours=2),
            output_dir=str(tmp_path),
        )
        bt_trades = [
            NormalizedTrade(
                client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                price=Decimal("100000"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                timestamp=ts_naive, source="backtest", direction=DirectionType.LONG,
            ),
            NormalizedTrade(
                client_order_id="order_b", symbol="BTCUSDT", side=SideType.SELL,
                price=Decimal("100200"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0.2"),
                timestamp=ts_naive + timedelta(hours=1), source="backtest", direction=DirectionType.LONG,
            ),
        ]

        # Patch DatabaseFactory to return our in-memory db
        with patch("comparator.main.DatabaseFactory", return_value=db):
            result = run(config, bt_trades)

        assert result == 0
        assert (tmp_path / "matched_trades.csv").exists()
        assert (tmp_path / "unmatched_trades.csv").exists()
        assert (tmp_path / "validation_metrics.csv").exists()

    def test_run_returns_1_no_backtest_trades(self, tmp_path):
        """run() returns 1 when backtest trades list is empty."""
        config = ComparatorConfig(
            run_id="run_1",
            output_dir=str(tmp_path),
        )
        result = run(config, [])
        assert result == 1

    def test_run_returns_1_no_live_trades(self, db, tmp_path):
        """run() returns 1 when no live trades found in DB."""
        # DB exists but has no executions for this run_id
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        config = ComparatorConfig(
            run_id="nonexistent_run",
            database_url="sqlite:///:memory:",
            start_ts=ts - timedelta(hours=1),
            end_ts=ts + timedelta(hours=2),
            output_dir=str(tmp_path),
        )

        bt_trades = [
            NormalizedTrade(
                client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                price=Decimal("100000"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                timestamp=ts, source="backtest", direction=None,
            ),
        ]

        with patch("comparator.main.DatabaseFactory", return_value=db):
            result = run(config, bt_trades)

        assert result == 1

    def test_main_csv_mode_end_to_end(self, db, tmp_path):
        """main() in CSV mode loads trades and produces reports."""
        self._seed_live_data(db)

        # Write backtest trades CSV (naive timestamps to match SQLite output)
        csv_path = tmp_path / "bt_trades.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "timestamp", "symbol", "side", "direction",
                "price", "qty", "notional", "realized_pnl", "commission",
                "order_id", "client_order_id", "strat_id",
            ])
            writer.writerow([
                "t1", "2025-01-15T12:00:00", "BTCUSDT", "Buy", "long",
                "100000", "0.001", "100", "0", "0.02",
                "oid_1", "order_a", "strat_1",
            ])

        output_dir = tmp_path / "output"
        argv = [
            "--run-id", "run_1",
            "--backtest-trades", str(csv_path),
            "--start", "2025-01-14",
            "--end", "2025-01-16",
            "--output", str(output_dir),
        ]

        with patch("comparator.main.DatabaseFactory", return_value=db):
            result = main(argv)

        assert result == 0
        assert (output_dir / "matched_trades.csv").exists()

    def test_main_returns_2_on_exception(self, tmp_path):
        """main() returns 2 when comparison raises an exception."""
        csv_path = tmp_path / "bt_trades.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "timestamp", "symbol", "side", "direction",
                "price", "qty", "notional", "realized_pnl", "commission",
                "order_id", "client_order_id", "strat_id",
            ])
            writer.writerow([
                "t1", "2025-01-15T12:00:00+00:00", "BTCUSDT", "Buy", "long",
                "100000", "0.001", "100", "0", "0.02",
                "oid_1", "order_a", "strat_1",
            ])

        argv = [
            "--run-id", "run_1",
            "--backtest-trades", str(csv_path),
            "--start", "2025-01-14",
            "--end", "2025-01-16",
            "--output", str(tmp_path / "output"),
        ]

        # Force run() to raise by making DatabaseFactory raise
        with patch("comparator.main.DatabaseFactory", side_effect=Exception("db error")):
            result = main(argv)

        assert result == 2

    def test_run_with_equity_comparison(self, db, tmp_path):
        """run() includes equity comparison when backtest_equity is provided."""
        ts = self._seed_live_data(db)
        ts_naive = ts.replace(tzinfo=None)

        # Add wallet snapshots for equity curve
        with db.get_session() as session:
            session.add(WalletSnapshot(
                account_id="acc1", exchange_ts=ts_naive,
                local_ts=ts_naive, coin="USDT",
                wallet_balance=Decimal("10000"), available_balance=Decimal("9000"),
            ))
            session.add(WalletSnapshot(
                account_id="acc1",
                exchange_ts=ts_naive + timedelta(hours=1),
                local_ts=ts_naive + timedelta(hours=1), coin="USDT",
                wallet_balance=Decimal("10050"), available_balance=Decimal("9050"),
            ))

        config = ComparatorConfig(
            run_id="run_1",
            database_url="sqlite:///:memory:",
            start_ts=ts_naive - timedelta(hours=1),
            end_ts=ts_naive + timedelta(hours=2),
            output_dir=str(tmp_path),
        )
        bt_trades = [
            NormalizedTrade(
                client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                price=Decimal("100000"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                timestamp=ts_naive, source="backtest", direction=DirectionType.LONG,
            ),
            NormalizedTrade(
                client_order_id="order_b", symbol="BTCUSDT", side=SideType.SELL,
                price=Decimal("100200"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0.2"),
                timestamp=ts_naive + timedelta(hours=1), source="backtest", direction=DirectionType.LONG,
            ),
        ]

        bt_equity = [
            (ts_naive, Decimal("10000")),
            (ts_naive + timedelta(hours=1), Decimal("10060")),
        ]

        with patch("comparator.main.DatabaseFactory", return_value=db):
            result = run(config, bt_trades, backtest_equity=bt_equity)

        assert result == 0
        assert (tmp_path / "equity_comparison.csv").exists()

    def test_run_equity_skipped_when_run_not_found(self, db, tmp_path):
        """run() skips equity comparison when Run record is not in DB."""
        ts = self._seed_live_data(db)
        ts_naive = ts.replace(tzinfo=None)

        # Use a different config run_id that has no Run record, but pass
        # live trades directly (mock the live loader path to control what's returned).
        config = ComparatorConfig(
            run_id="run_nonexistent",
            database_url="sqlite:///:memory:",
            start_ts=ts_naive - timedelta(hours=1),
            end_ts=ts_naive + timedelta(hours=2),
            output_dir=str(tmp_path),
        )
        bt_trades = [
            NormalizedTrade(
                client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                price=Decimal("100000"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                timestamp=ts_naive, source="backtest", direction=DirectionType.LONG,
            ),
        ]
        live_trades = [
            NormalizedTrade(
                client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                price=Decimal("100000"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                timestamp=ts_naive, source="live", direction=DirectionType.LONG,
            ),
        ]

        bt_equity = [(ts_naive, Decimal("10000"))]

        with patch("comparator.main.DatabaseFactory", return_value=db), \
             patch("comparator.loader.LiveTradeLoader.load", return_value=live_trades):
            result = run(config, bt_trades, backtest_equity=bt_equity)

        # Should succeed but skip equity (run_nonexistent has no Run record)
        assert result == 0
        assert not (tmp_path / "equity_comparison.csv").exists()

    def test_run_with_backtest_equity_csv_path(self, db, tmp_path):
        """run() loads backtest equity from CSV path when provided."""
        ts = self._seed_live_data(db)
        ts_naive = ts.replace(tzinfo=None)

        # Add wallet snapshots
        with db.get_session() as session:
            session.add(WalletSnapshot(
                account_id="acc1", exchange_ts=ts_naive,
                local_ts=ts_naive, coin="USDT",
                wallet_balance=Decimal("10000"), available_balance=Decimal("9000"),
            ))

        # Write backtest equity CSV
        equity_csv = tmp_path / "bt_equity.csv"
        with open(equity_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "equity", "return_pct"])
            writer.writerow([ts_naive.isoformat(), "10000", "0.0"])

        config = ComparatorConfig(
            run_id="run_1",
            database_url="sqlite:///:memory:",
            start_ts=ts_naive - timedelta(hours=1),
            end_ts=ts_naive + timedelta(hours=2),
            output_dir=str(tmp_path),
        )
        bt_trades = [
            NormalizedTrade(
                client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                price=Decimal("100000"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                timestamp=ts_naive, source="backtest", direction=DirectionType.LONG,
            ),
        ]

        with patch("comparator.main.DatabaseFactory", return_value=db):
            result = run(config, bt_trades, backtest_equity_path=str(equity_csv))

        assert result == 0

    def test_main_config_mode_with_mocked_backtest(self, db, tmp_path):
        """main() in config mode runs backtest from config and compares."""
        self._seed_live_data(db)

        ts_naive = datetime(2025, 1, 15, 12, 0, 0)

        output_dir = tmp_path / "output"
        argv = [
            "--run-id", "run_1",
            "--backtest-config", "dummy.yaml",
            "--start", "2025-01-14",
            "--end", "2025-01-16",
            "--symbol", "BTCUSDT",
            "--output", str(output_dir),
        ]

        with patch("comparator.main.DatabaseFactory", return_value=db), \
             patch("comparator.main._load_backtest_from_config") as mock_load:
            mock_load.return_value = (
                [NormalizedTrade(
                    client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                    price=Decimal("100000"), qty=Decimal("0.001"),
                    fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                    timestamp=ts_naive, source="backtest", direction=DirectionType.LONG,
                )],
                [(ts_naive, Decimal("10000"))],
            )
            result = main(argv)

        assert result == 0
        mock_load.assert_called_once()

    def test_main_csv_mode_with_aware_timestamps(self, db, tmp_path):
        """main() works when CSV has aware timestamps but DB returns naive."""
        self._seed_live_data(db)

        csv_path = tmp_path / "bt_trades.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "timestamp", "symbol", "side", "direction",
                "price", "qty", "notional", "realized_pnl", "commission",
                "order_id", "client_order_id", "strat_id",
            ])
            # Aware timestamp in CSV (DB will return naive)
            writer.writerow([
                "t1", "2025-01-15T12:00:00+00:00", "BTCUSDT", "Buy", "long",
                "100000", "0.001", "100", "0", "0.02",
                "oid_1", "order_a", "strat_1",
            ])

        output_dir = tmp_path / "output"
        argv = [
            "--run-id", "run_1",
            "--backtest-trades", str(csv_path),
            "--start", "2025-01-14",
            "--end", "2025-01-16",
            "--output", str(output_dir),
        ]

        with patch("comparator.main.DatabaseFactory", return_value=db):
            result = main(argv)

        # Previously would crash with TypeError on timestamp subtraction
        assert result == 0
        assert (output_dir / "matched_trades.csv").exists()

    def test_config_mode_requires_symbol(self, tmp_path):
        """Config mode returns 1 when --symbol is omitted."""
        output_dir = tmp_path / "output"
        argv = [
            "--run-id", "run_1",
            "--backtest-config", "dummy.yaml",
            "--start", "2025-01-14",
            "--end", "2025-01-16",
            # --symbol NOT provided
            "--output", str(output_dir),
        ]

        result = main(argv)
        assert result == 1

    def test_run_filters_backtest_trades_by_symbol(self, db, tmp_path):
        """run() filters backtest trades by config.symbol so both sides match."""
        ts = self._seed_live_data(db)
        ts_naive = ts.replace(tzinfo=None)

        config = ComparatorConfig(
            run_id="run_1",
            database_url="sqlite:///:memory:",
            start_ts=ts_naive - timedelta(hours=1),
            end_ts=ts_naive + timedelta(hours=2),
            symbol="BTCUSDT",
            output_dir=str(tmp_path),
        )

        bt_trades = [
            NormalizedTrade(
                client_order_id="order_a", symbol="BTCUSDT", side=SideType.BUY,
                price=Decimal("100000"), qty=Decimal("0.001"),
                fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                timestamp=ts_naive, source="backtest", direction=DirectionType.LONG,
            ),
            # This ETHUSDT trade should be filtered out
            NormalizedTrade(
                client_order_id="order_eth", symbol="ETHUSDT", side=SideType.BUY,
                price=Decimal("3000"), qty=Decimal("0.1"),
                fee=Decimal("0.01"), realized_pnl=Decimal("0"),
                timestamp=ts_naive, source="backtest", direction=DirectionType.LONG,
            ),
        ]

        with patch("comparator.main.DatabaseFactory", return_value=db):
            result = run(config, bt_trades)

        assert result == 0

        # Check metrics CSV: backtest_only should be 0 for the ETH trade
        # (it was filtered, not counted as phantom)
        import csv as csv_mod
        with open(tmp_path / "validation_metrics.csv") as f:
            rows = {r["metric"]: r["value"] for r in csv_mod.DictReader(f)}
        # ETH trade filtered out, so no phantom backtest-only count from it
        assert rows["backtest_only_count"] == "0"
