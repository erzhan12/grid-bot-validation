"""Tests for comparator.loader module."""

import csv
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from grid_db import (
    DatabaseFactory,
    PrivateExecution,
    User,
    BybitAccount,
    Strategy,
    Run,
    RunType,
)
from gridcore.position import DirectionType, SideType

from comparator.loader import LiveTradeLoader, BacktestTradeLoader, _normalize_ts


# --- LiveTradeLoader ---


class TestLiveTradeLoader:
    """Tests for LiveTradeLoader."""

    def _seed_data(self, db: DatabaseFactory, run_id: str = "run_1"):
        """Seed database with test executions. Returns run_id."""
        with db.get_session() as session:
            user = User(user_id="u1", username="test", email="t@t.com")
            session.add(user)
            session.flush()

            account = BybitAccount(
                account_id="acc1", user_id="u1", account_name="main", environment="testnet"
            )
            session.add(account)
            session.flush()

            strategy = Strategy(
                strategy_id="s1",
                account_id="acc1",
                strategy_type="GridStrategy",
                symbol="BTCUSDT",
                config_json={"grid_step": 0.2},
            )
            session.add(strategy)
            session.flush()

            run = Run(
                run_id=run_id,
                user_id="u1",
                account_id="acc1",
                strategy_id="s1",
                run_type=RunType.LIVE,
            )
            session.add(run)
            session.flush()
        return run_id

    def _add_execution(
        self, db, run_id, exec_id, order_link_id, side, price, qty, fee, pnl, ts,
        order_id=None,
    ):
        """Add a single execution to the database."""
        with db.get_session() as session:
            ex = PrivateExecution(
                run_id=run_id,
                account_id="acc1",
                symbol="BTCUSDT",
                exec_id=exec_id,
                order_id=order_id or f"oid_{exec_id}",
                order_link_id=order_link_id,
                exchange_ts=ts,
                side=side,
                exec_price=price,
                exec_qty=qty,
                exec_fee=fee,
                closed_pnl=pnl,
            )
            session.add(ex)

    def test_load_single_execution(self, db):
        """Single execution maps to one NormalizedTrade."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self._add_execution(
            db, run_id, "e1", "client_1", "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"),
            ts,
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert len(trades) == 1
        t = trades[0]
        assert t.client_order_id == "client_1"
        assert t.side == SideType.BUY
        assert t.price == Decimal("100000")
        assert t.qty == Decimal("0.001")
        assert t.fee == Decimal("0.02")
        assert t.source == "live"

    def test_aggregates_partial_fills(self, db):
        """Multiple executions with same order_link_id + order_id are aggregated."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Two partial fills for the same order (same order_id)
        self._add_execution(
            db, run_id, "e1", "client_1", "Buy",
            Decimal("100000"), Decimal("0.0005"), Decimal("0.01"), Decimal("0"),
            ts, order_id="oid_shared",
        )
        self._add_execution(
            db, run_id, "e2", "client_1", "Buy",
            Decimal("100002"), Decimal("0.0005"), Decimal("0.01"), Decimal("0"),
            ts + timedelta(seconds=1), order_id="oid_shared",
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert len(trades) == 1
        t = trades[0]
        assert t.qty == Decimal("0.001")
        assert t.fee == Decimal("0.02")
        # VWAP: (100000 * 0.0005 + 100002 * 0.0005) / 0.001 = 100001
        assert t.price == Decimal("100001")
        # SQLite strips timezone info, so compare without tz
        expected_ts = ts + timedelta(seconds=1)
        assert t.timestamp.replace(tzinfo=None) == expected_ts.replace(tzinfo=None)

    def test_skips_null_order_link_id(self, db):
        """Executions without order_link_id are skipped."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        self._add_execution(
            db, run_id, "e1", None, "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"),
            ts,
        )
        self._add_execution(
            db, run_id, "e2", "client_1", "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"),
            ts,
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert len(trades) == 1
        assert trades[0].client_order_id == "client_1"

    def test_symbol_filter(self, db):
        """Symbol filter only returns matching trades."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        self._add_execution(
            db, run_id, "e1", "client_1", "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"),
            ts,
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            # Filter for non-matching symbol
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1), symbol="ETHUSDT")

        assert len(trades) == 0

    def test_empty_result(self, db):
        """No executions returns empty list."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts, ts + timedelta(hours=1))

        assert trades == []

    def test_pnl_aggregation(self, db):
        """Partial fills sum their closed_pnl values."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        self._add_execution(
            db, run_id, "e1", "client_1", "Sell",
            Decimal("100200"), Decimal("0.0005"), Decimal("0.01"), Decimal("0.05"),
            ts, order_id="oid_shared",
        )
        self._add_execution(
            db, run_id, "e2", "client_1", "Sell",
            Decimal("100200"), Decimal("0.0005"), Decimal("0.01"), Decimal("0.05"),
            ts + timedelta(seconds=1), order_id="oid_shared",
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert trades[0].realized_pnl == Decimal("0.10")

    def test_direction_inferred_opening_buy(self, db):
        """Buy with zero closed_pnl inferred as long."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self._add_execution(
            db, run_id, "e1", "client_1", "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"), ts,
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert trades[0].direction == DirectionType.LONG

    def test_direction_inferred_closing_sell(self, db):
        """Sell with non-zero closed_pnl inferred as long (closing long)."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self._add_execution(
            db, run_id, "e1", "client_1", "Sell",
            Decimal("100200"), Decimal("0.001"), Decimal("0.02"), Decimal("0.2"), ts,
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert trades[0].direction == DirectionType.LONG

    def test_direction_inferred_opening_sell(self, db):
        """Sell with zero closed_pnl inferred as short (opening short)."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self._add_execution(
            db, run_id, "e1", "client_1", "Sell",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"), ts,
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert trades[0].direction == DirectionType.SHORT

    def test_direction_inferred_closing_buy(self, db):
        """Buy with non-zero closed_pnl inferred as short (closing short)."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self._add_execution(
            db, run_id, "e1", "client_1", "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("-0.1"), ts,
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        assert trades[0].direction == DirectionType.SHORT

    def test_reused_client_order_id_separated_by_order_id(self, db):
        """Same order_link_id with different order_ids creates separate trades."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        # First lifecycle: order_link_id="client_1", order_id="oid_1"
        self._add_execution(
            db, run_id, "e1", "client_1", "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"),
            ts,
        )
        # Second lifecycle: same order_link_id, different order_id
        self._add_execution(
            db, run_id, "e2", "client_1", "Buy",
            Decimal("100000"), Decimal("0.001"), Decimal("0.02"), Decimal("0"),
            ts + timedelta(hours=1),
        )

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=2))

        # Should produce 2 separate trades, not 1 aggregated
        assert len(trades) == 2
        assert trades[0].client_order_id == "client_1"
        assert trades[1].client_order_id == "client_1"
        assert trades[0].occurrence == 0
        assert trades[1].occurrence == 1

    def test_partial_fills_same_order_id_still_aggregated(self, db):
        """Same order_link_id + same order_id are still aggregated as partial fills."""
        run_id = self._seed_data(db)
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Two partial fills for the same order (same order_id via "oid_e1")
        with db.get_session() as session:
            ex1 = PrivateExecution(
                run_id=run_id, account_id="acc1", symbol="BTCUSDT",
                exec_id="e1", order_id="oid_A", order_link_id="client_1",
                exchange_ts=ts, side="Buy",
                exec_price=Decimal("100000"), exec_qty=Decimal("0.0005"),
                exec_fee=Decimal("0.01"), closed_pnl=Decimal("0"),
            )
            ex2 = PrivateExecution(
                run_id=run_id, account_id="acc1", symbol="BTCUSDT",
                exec_id="e2", order_id="oid_A", order_link_id="client_1",
                exchange_ts=ts + timedelta(seconds=1), side="Buy",
                exec_price=Decimal("100002"), exec_qty=Decimal("0.0005"),
                exec_fee=Decimal("0.01"), closed_pnl=Decimal("0"),
            )
            session.add_all([ex1, ex2])

        with db.get_session() as session:
            loader = LiveTradeLoader(session)
            trades = loader.load(run_id, ts - timedelta(hours=1), ts + timedelta(hours=1))

        # Same order_id → aggregated into 1 trade
        assert len(trades) == 1
        assert trades[0].qty == Decimal("0.001")
        assert trades[0].occurrence == 0


# --- BacktestTradeLoader ---


class TestBacktestTradeLoader:
    """Tests for BacktestTradeLoader."""

    def test_load_from_csv(self, tmp_path):
        """Load trades from CSV file."""
        csv_path = tmp_path / "trades.csv"
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
                "oid_1", "client_1", "strat_1",
            ])
            writer.writerow([
                "t2", "2025-01-15T13:00:00+00:00", "BTCUSDT", "Sell", "long",
                "100200", "0.001", "100.2", "0.2", "0.02",
                "oid_2", "client_2", "strat_1",
            ])

        loader = BacktestTradeLoader()
        trades = loader.load_from_csv(csv_path)

        assert len(trades) == 2
        assert trades[0].client_order_id == "client_1"
        assert trades[0].price == Decimal("100000")
        assert trades[0].source == "backtest"
        assert trades[1].realized_pnl == Decimal("0.2")

    def test_load_from_session(self):
        """Load trades from BacktestSession.trades list."""
        from backtest.session import BacktestTrade

        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        trades = [
            BacktestTrade(
                trade_id="t1", symbol="BTCUSDT", side="Buy",
                price=Decimal("100000"), qty=Decimal("0.001"),
                direction="long", timestamp=ts, order_id="oid_1",
                client_order_id="client_1", realized_pnl=Decimal("0"),
                commission=Decimal("0.02"),
            ),
        ]

        loader = BacktestTradeLoader()
        result = loader.load_from_session(trades)

        assert len(result) == 1
        assert result[0].client_order_id == "client_1"
        assert result[0].source == "backtest"

    def test_empty_csv(self, tmp_path):
        """Empty CSV returns empty list."""
        csv_path = tmp_path / "empty.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "timestamp", "symbol", "side", "direction",
                "price", "qty", "notional", "realized_pnl", "commission",
                "order_id", "client_order_id", "strat_id",
            ])

        loader = BacktestTradeLoader()
        trades = loader.load_from_csv(csv_path)
        assert trades == []

    def test_occurrence_assigned_for_reused_ids(self, tmp_path):
        """Reused client_order_id in CSV gets sequential occurrence indices."""
        csv_path = tmp_path / "trades.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "timestamp", "symbol", "side", "direction",
                "price", "qty", "notional", "realized_pnl", "commission",
                "order_id", "client_order_id", "strat_id",
            ])
            # Same client_order_id, different timestamps (reuse)
            writer.writerow([
                "t1", "2025-01-15T12:00:00+00:00", "BTCUSDT", "Buy", "long",
                "100000", "0.001", "100", "0", "0.02",
                "oid_1", "client_reused", "strat_1",
            ])
            writer.writerow([
                "t2", "2025-01-15T14:00:00+00:00", "BTCUSDT", "Buy", "long",
                "100000", "0.001", "100", "0", "0.02",
                "oid_2", "client_reused", "strat_1",
            ])
            writer.writerow([
                "t3", "2025-01-15T13:00:00+00:00", "BTCUSDT", "Sell", "long",
                "100200", "0.001", "100.2", "0.2", "0.02",
                "oid_3", "client_unique", "strat_1",
            ])

        loader = BacktestTradeLoader()
        trades = loader.load_from_csv(csv_path)

        assert len(trades) == 3
        reused = [t for t in trades if t.client_order_id == "client_reused"]
        assert reused[0].occurrence == 0
        assert reused[1].occurrence == 1
        unique = [t for t in trades if t.client_order_id == "client_unique"]
        assert unique[0].occurrence == 0

    def test_sorted_by_timestamp(self, tmp_path):
        """Trades are sorted by timestamp regardless of file order."""
        csv_path = tmp_path / "trades.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "timestamp", "symbol", "side", "direction",
                "price", "qty", "notional", "realized_pnl", "commission",
                "order_id", "client_order_id", "strat_id",
            ])
            # Write in reverse order
            writer.writerow([
                "t2", "2025-01-15T14:00:00+00:00", "BTCUSDT", "Sell", "long",
                "100200", "0.001", "100.2", "0.2", "0.02",
                "oid_2", "client_2", "strat_1",
            ])
            writer.writerow([
                "t1", "2025-01-15T12:00:00+00:00", "BTCUSDT", "Buy", "long",
                "100000", "0.001", "100", "0", "0.02",
                "oid_1", "client_1", "strat_1",
            ])

        loader = BacktestTradeLoader()
        trades = loader.load_from_csv(csv_path)

        assert trades[0].client_order_id == "client_1"
        assert trades[1].client_order_id == "client_2"

    def test_csv_aware_timestamps_normalized_to_naive(self, tmp_path):
        """CSV timestamps with timezone info are normalized to naive UTC."""
        csv_path = tmp_path / "trades.csv"
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
                "oid_1", "client_1", "strat_1",
            ])
            # Non-UTC timezone: 17:00+05:00 = 12:00 UTC
            writer.writerow([
                "t2", "2025-01-15T17:00:00+05:00", "BTCUSDT", "Sell", "long",
                "100200", "0.001", "100.2", "0.2", "0.02",
                "oid_2", "client_2", "strat_1",
            ])

        loader = BacktestTradeLoader()
        trades = loader.load_from_csv(csv_path)

        for t in trades:
            assert t.timestamp.tzinfo is None
        assert trades[0].timestamp == datetime(2025, 1, 15, 12, 0, 0)
        assert trades[1].timestamp == datetime(2025, 1, 15, 12, 0, 0)


# --- _normalize_ts ---


class TestNormalizeTs:
    """Tests for _normalize_ts helper."""

    def test_aware_utc_becomes_naive(self):
        """UTC-aware datetime is stripped to naive."""
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = _normalize_ts(dt)
        assert result == datetime(2025, 1, 15, 12, 0, 0)
        assert result.tzinfo is None

    def test_aware_non_utc_converted_to_naive_utc(self):
        """Non-UTC aware datetime is converted to UTC then stripped."""
        tz_plus5 = timezone(timedelta(hours=5))
        dt = datetime(2025, 1, 15, 17, 0, 0, tzinfo=tz_plus5)  # 17:00+05:00 = 12:00 UTC
        result = _normalize_ts(dt)
        assert result == datetime(2025, 1, 15, 12, 0, 0)
        assert result.tzinfo is None

    def test_naive_passthrough(self):
        """Naive datetime passes through unchanged."""
        dt = datetime(2025, 1, 15, 12, 0, 0)
        result = _normalize_ts(dt)
        assert result == dt
        assert result.tzinfo is None
