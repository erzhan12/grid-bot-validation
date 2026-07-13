"""Tests for live_check.ground_truth — recorded-row queries."""

from datetime import timedelta
from decimal import Decimal

import pytest

from grid_db import PositionSnapshot, PrivateExecution, TickerSnapshot

from live_check import ground_truth
from live_check.window import Window

RUN_ID = "test-run-id"


def _insert_exec(db, *, exec_id, ts, symbol="LTCUSDT", side="Buy",
                 price="80", qty="0.2", fee=None, pnl=None, link=None):
    with db.get_session() as session:
        session.add(PrivateExecution(
            run_id=RUN_ID,
            account_id="acc1",
            symbol=symbol,
            exec_id=exec_id,
            order_id=f"oid-{exec_id}",
            order_link_id=link,
            exchange_ts=ts,
            side=side,
            exec_price=Decimal(price),
            exec_qty=Decimal(qty),
            exec_fee=Decimal(fee) if fee is not None else None,
            closed_pnl=Decimal(pnl) if pnl is not None else None,
        ))


def _insert_position(db, account_id, *, ts, side, unrealised,
                     symbol="LTCUSDT", source="live"):
    with db.get_session() as session:
        session.add(PositionSnapshot(
            run_id=RUN_ID,
            account_id=account_id,
            symbol=symbol,
            exchange_ts=ts,
            local_ts=ts,
            side=side,
            size=Decimal("0.2"),
            entry_price=Decimal("80"),
            unrealised_pnl=(
                Decimal(unrealised) if unrealised is not None else None
            ),
            source=source,
        ))


def _insert_ticker(db, *, ts, symbol="LTCUSDT", price="80"):
    with db.get_session() as session:
        session.add(TickerSnapshot(
            symbol=symbol,
            exchange_ts=ts,
            local_ts=ts,
            last_price=Decimal(price),
            mark_price=Decimal(price),
            bid1_price=Decimal(price) - Decimal("0.1"),
            ask1_price=Decimal(price) + Decimal("0.1"),
            funding_rate=Decimal("0.0001"),
        ))


class TestSums:
    def test_sum_realized(self, db, seeded_run_account, ts):
        """SUM closed_pnl over the window, symbol-scoped."""
        _insert_exec(db, exec_id="e1", ts=ts, pnl="1.5", fee="0.01")
        _insert_exec(db, exec_id="e2", ts=ts + timedelta(minutes=1),
                     pnl="-0.5", fee="0.02")
        with db.get_readonly_session() as s:
            total = ground_truth.sum_realized(
                s, RUN_ID, "LTCUSDT", ts - timedelta(hours=1),
                ts + timedelta(hours=1),
            )
        assert total == Decimal("1.0")

    def test_sum_commission(self, db, seeded_run_account, ts):
        """SUM exec_fee over the window, symbol-scoped."""
        _insert_exec(db, exec_id="e1", ts=ts, fee="0.01")
        _insert_exec(db, exec_id="e2", ts=ts + timedelta(minutes=1), fee="0.02")
        with db.get_readonly_session() as s:
            total = ground_truth.sum_commission(
                s, RUN_ID, "LTCUSDT", ts - timedelta(hours=1),
                ts + timedelta(hours=1),
            )
        assert total == Decimal("0.03")

    def test_sums_coalesce_empty_window_to_zero(self, db, seeded_run_account, ts):
        """SUM over zero rows returns Decimal(0), not None."""
        with db.get_readonly_session() as s:
            assert ground_truth.sum_realized(
                s, RUN_ID, "LTCUSDT", ts, ts + timedelta(hours=1)
            ) == Decimal("0")
            assert ground_truth.sum_commission(
                s, RUN_ID, "LTCUSDT", ts, ts + timedelta(hours=1)
            ) == Decimal("0")

    def test_sums_coalesce_all_null_rows_to_zero(self, db, seeded_run_account, ts):
        """SUM over all-NULL closed_pnl/exec_fee rows returns Decimal(0)."""
        _insert_exec(db, exec_id="e1", ts=ts, pnl=None, fee=None)
        with db.get_readonly_session() as s:
            assert ground_truth.sum_realized(
                s, RUN_ID, "LTCUSDT", ts - timedelta(hours=1),
                ts + timedelta(hours=1),
            ) == Decimal("0")
            assert ground_truth.sum_commission(
                s, RUN_ID, "LTCUSDT", ts - timedelta(hours=1),
                ts + timedelta(hours=1),
            ) == Decimal("0")

    def test_multi_symbol_shared_run_isolation(self, db, seeded_run_account, ts):
        """SOL sums EXCLUDE LTC execs under the SAME run_id (symbol-scoped)."""
        _insert_exec(db, exec_id="sol1", ts=ts, symbol="SOLUSDT",
                     pnl="2.0", fee="0.05")
        _insert_exec(db, exec_id="ltc1", ts=ts, symbol="LTCUSDT",
                     pnl="9.0", fee="0.90")
        start, end = ts - timedelta(hours=1), ts + timedelta(hours=1)
        with db.get_readonly_session() as s:
            assert ground_truth.sum_realized(
                s, RUN_ID, "SOLUSDT", start, end) == Decimal("2.0")
            assert ground_truth.sum_commission(
                s, RUN_ID, "SOLUSDT", start, end) == Decimal("0.05")
            assert ground_truth.live_exec_count(
                s, RUN_ID, "SOLUSDT", start, end) == 1


class TestNetUnrealised:
    def test_net_long_plus_short(self, db, seeded_run_account, ts):
        """Long + short legs combine to one NET value per pair."""
        acc = seeded_run_account.account_id
        _insert_position(db, acc, ts=ts, side="Buy", unrealised="1.2")
        _insert_position(db, acc, ts=ts, side="Sell", unrealised="-0.4")
        with db.get_readonly_session() as s:
            net = ground_truth.net_unrealised_per_pair(
                s, RUN_ID, acc, "LTCUSDT", ts + timedelta(minutes=1)
            )
        assert net == Decimal("0.8")

    def test_last_at_or_before_selection(self, db, seeded_run_account, ts):
        """Takes the last snapshot at/before at_ts per side; later ones ignored."""
        acc = seeded_run_account.account_id
        _insert_position(db, acc, ts=ts, side="Buy", unrealised="1.0")
        _insert_position(db, acc, ts=ts + timedelta(minutes=5), side="Buy",
                         unrealised="2.0")
        _insert_position(db, acc, ts=ts + timedelta(minutes=30), side="Buy",
                         unrealised="99.0")
        with db.get_readonly_session() as s:
            net = ground_truth.net_unrealised_per_pair(
                s, RUN_ID, acc, "LTCUSDT", ts + timedelta(minutes=10)
            )
        assert net == Decimal("2.0")

    def test_null_unrealised_coerces_to_zero(self, db, seeded_run_account, ts):
        """NULL unrealised_pnl on a leg counts as 0, not a crash."""
        acc = seeded_run_account.account_id
        _insert_position(db, acc, ts=ts, side="Buy", unrealised=None)
        _insert_position(db, acc, ts=ts, side="Sell", unrealised="-0.3")
        with db.get_readonly_session() as s:
            net = ground_truth.net_unrealised_per_pair(
                s, RUN_ID, acc, "LTCUSDT", ts + timedelta(minutes=1)
            )
        assert net == Decimal("-0.3")

    def test_backtest_source_rows_excluded(self, db, seeded_run_account, ts):
        """Only source='live' rows feed ground truth."""
        acc = seeded_run_account.account_id
        _insert_position(db, acc, ts=ts, side="Buy", unrealised="5.0",
                         source="backtest")
        with db.get_readonly_session() as s:
            net = ground_truth.net_unrealised_per_pair(
                s, RUN_ID, acc, "LTCUSDT", ts + timedelta(minutes=1)
            )
        assert net == Decimal("0")


class TestProbes:
    def test_latest_ticker_ts(self, db, seeded_run_account, ts):
        """MAX exchange_ts per symbol."""
        _insert_ticker(db, ts=ts)
        _insert_ticker(db, ts=ts + timedelta(minutes=2))
        with db.get_readonly_session() as s:
            assert ground_truth.latest_ticker_ts(s, "LTCUSDT") == (
                ts + timedelta(minutes=2)
            )

    def test_latest_ticker_ts_none_when_no_rows(self, db, seeded_run_account):
        """No ticker rows for the symbol → None, not a crash."""
        with db.get_readonly_session() as s:
            assert ground_truth.latest_ticker_ts(s, "SOLUSDT") is None

    def test_get_run_start(self, db, seeded_run_account, ts):
        """Returns Run.start_ts for the guard."""
        with db.get_readonly_session() as s:
            assert ground_truth.get_run_start(s, RUN_ID) == ts - timedelta(days=1)

    def test_get_run_start_unknown_run_raises(self, db, seeded_run_account):
        """Unknown run_id is a hard error."""
        with db.get_readonly_session() as s:
            with pytest.raises(ValueError, match="not found"):
                ground_truth.get_run_start(s, "nope")

    def test_get_window_executions_raw_grain(self, db, seeded_run_account, ts):
        """Raw rows, symbol-scoped, ordered (exchange_ts, exec_id)."""
        _insert_exec(db, exec_id="b", ts=ts, link="L1")
        _insert_exec(db, exec_id="a", ts=ts, link="L1")
        _insert_exec(db, exec_id="sol", ts=ts, symbol="SOLUSDT")
        with db.get_readonly_session() as s:
            rows = ground_truth.get_window_executions(
                s, RUN_ID, "LTCUSDT", ts - timedelta(hours=1),
                ts + timedelta(hours=1),
            )
        assert [r.exec_id for r in rows] == ["a", "b"]
        assert all(r.order_link_id == "L1" for r in rows)


class TestCollect:
    def test_collect_bundles_all_fields(self, db, seeded_run_account, ts):
        """collect() gathers sums + net unrealised + count for one window."""
        acc = seeded_run_account.account_id
        _insert_exec(db, exec_id="e1", ts=ts, pnl="1.5", fee="0.01")
        _insert_position(db, acc, ts=ts, side="Buy", unrealised="0.7")
        window = Window(start=ts - timedelta(hours=1), end=ts + timedelta(hours=1))
        with db.get_readonly_session() as s:
            truth = ground_truth.collect(s, RUN_ID, acc, "LTCUSDT", window)
        assert truth.sum_realized == Decimal("1.5")
        assert truth.sum_commission == Decimal("0.01")
        assert truth.net_unrealised == Decimal("0.7")
        assert truth.live_exec_count == 1
