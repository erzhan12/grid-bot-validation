"""Unit tests for the event_follower fill source (feature 0072).

Covers:
- ``EventFollower.drain`` window semantics + forward-only monotonic cursor.
- ``EventFollower.match`` key-faithful selection (deterministic
  ``client_order_id`` prefix), ``order_id`` fallback, side/closest-price
  fallback + determinism.
- ``BacktestOrderManager.apply_recorded_fill`` partial-fill decrement,
  full-fill pop, placed-qty cap with pro-rated fee/pnl.
- Runner-level drain: wallet identity across one open→close cycle (recorded
  ``closed_pnl`` authoritative, EPS basis agreement), partial-fill pending
  wallet visibility between rows, one aggregated ``BacktestTrade`` per
  ``(matcher_key, recorded_order_id)`` lifecycle (triggers 1/2/3),
  no-match → no position change, zero-execution tick no-op,
  ``_prev_tick_ts`` left-edge boundary.

Runner-level tests stub the GridEngine (``on_event`` → ``[]``) to isolate
fill application from grid logic; the reactive-close/intent path runs with
the real engine in ``apps/replay/tests/test_engine_event_follower.py``.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from gridcore import CancelIntent, EventType, TickerEvent

from backtest.config import BacktestStrategyConfig
from backtest.executor import BacktestExecutor
from backtest.fill_simulator import (
    EventFollower,
    FillMode,
    RecordedExecution,
    TradeThroughFillSimulator,
)
from backtest.order_manager import BacktestOrderManager
from backtest.runner import BacktestRunner
from backtest.session import BacktestSession


T0 = datetime(2026, 6, 1, 12, 0, 0)  # naive UTC (matches SQLite-loaded rows)


def _ts(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _exec(
    exec_id: str,
    order_id: str,
    *,
    link: str | None = None,
    side: str = "Buy",
    price: str = "100",
    qty: str = "1",
    fee: str = "0.02",
    pnl: str = "0",
    at: float = 0.0,
) -> RecordedExecution:
    return RecordedExecution(
        exec_id=exec_id,
        order_link_id=link,
        order_id=order_id,
        side=side,
        exec_price=Decimal(price),
        exec_qty=Decimal(qty),
        exec_fee=Decimal(fee),
        closed_pnl=Decimal(pnl),
        exchange_ts=_ts(at),
    )


def _follower(execs: list[RecordedExecution], start_at: float = 0.0) -> EventFollower:
    return EventFollower(execs, symbol="LTCUSDT", start_ts=_ts(start_at))


def _ticker(at: float, price: str = "100") -> TickerEvent:
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="LTCUSDT",
        exchange_ts=_ts(at),
        local_ts=_ts(at),
        last_price=Decimal(price),
        mark_price=Decimal(price),
    )


class TestEventFollowerDrain:
    def test_drain_returns_window_and_advances_cursor(self):
        follower = _follower([
            _exec("e1", "o1", at=1),
            _exec("e2", "o2", at=5),
            _exec("e3", "o3", at=9),
        ])

        first = follower.drain(follower.initial_prev_ts, _ts(5))
        assert [e.exec_id for e in first] == ["e1", "e2"]
        assert follower.remaining == 1

        second = follower.drain(_ts(5), _ts(10))
        assert [e.exec_id for e in second] == ["e3"]
        assert follower.remaining == 0

        # Subsequent drains return nothing (cursor exhausted).
        assert follower.drain(_ts(10), _ts(20)) == []

    def test_execution_at_exact_start_ts_drained_on_first_tick(self):
        """Left-edge boundary: get_by_run_range loads exchange_ts >= start_ts
        inclusive; initial_prev_ts = start_ts - 1µs keeps that row in the
        first (prev, tick] window."""
        follower = _follower([_exec("e1", "o1", at=0)], start_at=0)

        drained = follower.drain(follower.initial_prev_ts, _ts(1))
        assert [e.exec_id for e in drained] == ["e1"]

    def test_non_monotonic_tick_ts_raises(self):
        follower = _follower([_exec("e1", "o1", at=1)])
        follower.drain(follower.initial_prev_ts, _ts(5))

        with pytest.raises(ValueError, match="non-monotonic"):
            follower.drain(_ts(0), _ts(4))

    def test_equal_tick_ts_is_allowed_and_empty(self):
        follower = _follower([_exec("e1", "o1", at=1)])
        follower.drain(follower.initial_prev_ts, _ts(5))
        assert follower.drain(_ts(5), _ts(5)) == []

    def test_has_pending_for_order_tracks_unconsumed_tail(self):
        follower = _follower([
            _exec("e1", "o1", at=1),
            _exec("e2", "o1", at=8),
        ])
        assert follower.has_pending_for_order("o1") is True

        follower.drain(follower.initial_prev_ts, _ts(5))
        assert follower.has_pending_for_order("o1") is True  # e2 in tail

        follower.drain(_ts(5), _ts(10))
        assert follower.has_pending_for_order("o1") is False
        assert follower.has_pending_for_order("unknown") is False


def _sim_order(order_manager: BacktestOrderManager, client_id: str, **kw):
    defaults = dict(
        symbol="LTCUSDT",
        side="Buy",
        price=Decimal("100"),
        qty=Decimal("1"),
        direction="long",
        grid_level=1,
        timestamp=T0,
    )
    defaults.update(kw)
    return order_manager.place_order(client_order_id=client_id, **defaults)


@pytest.fixture
def follower_order_manager():
    return BacktestOrderManager(
        fill_simulator=TradeThroughFillSimulator(mode=FillMode.EVENT_FOLLOWER),
        commission_rate=Decimal("0.0002"),
    )


class TestEventFollowerMatch:
    def test_key_faithful_match_on_link_prefix(self, follower_order_manager):
        order = _sim_order(follower_order_manager, "abc123")
        follower = _follower([])

        ex = _exec("e1", "bybit1", link="abc123-1748000000000")
        match = follower.match(ex, follower_order_manager.active_orders)

        assert match is not None
        assert match.replay_order_id == order.order_id  # sim_* id
        assert match.matcher_key == "abc123"
        assert match.recorded_order_id == "bybit1"

    def test_key_present_but_no_active_order_returns_none(
        self, follower_order_manager
    ):
        _sim_order(follower_order_manager, "abc123")
        follower = _follower([])

        ex = _exec("e1", "bybit1", link="other999-1748000000000")
        assert follower.match(ex, follower_order_manager.active_orders) is None

    def test_sanity_violation_logs_but_match_stands(
        self, follower_order_manager, caplog
    ):
        """Buy limit below exec_price violates price sanity; id wins."""
        order = _sim_order(
            follower_order_manager, "abc123", price=Decimal("99")
        )
        follower = _follower([])

        ex = _exec("e1", "bybit1", link="abc123-1", price="100")
        with caplog.at_level("WARNING"):
            match = follower.match(ex, follower_order_manager.active_orders)

        assert match is not None
        assert match.replay_order_id == order.order_id
        assert "price sanity violation" in caplog.text

    def test_no_link_falls_back_to_order_id(self, follower_order_manager):
        """Seeded orders are keyed by exchange order id — the order_id
        fallback covers pre-hotfix rows joining on it."""

        class _Seed:
            client_id = "seeded1"
            exchange_order_id = "bybit-ex-1"
            symbol = "LTCUSDT"
            side = "Buy"
            direction = "long"
            price = Decimal("100")
            remaining_qty = Decimal("1")
            reduce_only = False
            exchange_ts = T0

        follower_order_manager.seed_active_orders([_Seed()])
        follower = _follower([])

        ex = _exec("e1", "bybit-ex-1", link=None)
        match = follower.match(ex, follower_order_manager.active_orders)

        assert match is not None
        assert match.replay_order_id == "bybit-ex-1"
        # matcher_key mirrors LiveTradeLoader: link_prefix or ex.order_id.
        assert match.matcher_key == "bybit-ex-1"
        assert follower.no_link_id_count == 1
        assert follower.fallback_order_id_count == 1

    def test_no_link_falls_back_to_side_closest_price(
        self, follower_order_manager
    ):
        _sim_order(follower_order_manager, "far", price=Decimal("95"))
        near = _sim_order(follower_order_manager, "near", price=Decimal("100"))
        _sim_order(
            follower_order_manager, "wrongside", side="Sell",
            price=Decimal("100"),
        )
        follower = _follower([])

        ex = _exec("e1", "bybit9", link=None, side="Buy", price="100.1")
        match = follower.match(ex, follower_order_manager.active_orders)

        assert match is not None
        assert match.replay_order_id == near.order_id
        assert match.matcher_key == "bybit9"
        assert follower.fallback_price_count == 1

    def test_closest_price_tie_breaks_on_order_id(self, follower_order_manager):
        a = _sim_order(follower_order_manager, "a", price=Decimal("99"))
        _sim_order(follower_order_manager, "b", price=Decimal("101"))
        follower = _follower([])

        # Equidistant from 100 → lowest order_id wins (placement order).
        ex = _exec("e1", "bybit9", link=None, side="Buy", price="100")
        match = follower.match(ex, follower_order_manager.active_orders)
        assert match.replay_order_id == a.order_id

    def test_no_candidates_returns_none(self, follower_order_manager):
        follower = _follower([])
        ex = _exec("e1", "bybit9", link=None, side="Buy")
        assert follower.match(ex, follower_order_manager.active_orders) is None


class TestApplyRecordedFill:
    def test_partial_fill_decrements_and_keeps_active(
        self, follower_order_manager
    ):
        order = _sim_order(follower_order_manager, "abc123", qty=Decimal("1"))

        event, full = follower_order_manager.apply_recorded_fill(
            order.order_id,
            exec_price=Decimal("100"),
            exec_qty=Decimal("0.4"),
            exec_fee=Decimal("0.008"),
            closed_pnl=Decimal("0"),
            timestamp=_ts(1),
            exec_id="e1",
        )

        assert full is False
        assert event.qty == Decimal("0.4")
        assert event.fee == Decimal("0.008")
        assert event.leaves_qty == Decimal("0.6")
        assert order.order_id in follower_order_manager.active_orders
        assert follower_order_manager.active_orders[order.order_id].qty == Decimal("0.6")
        # client_order_id stays reserved until full fill.
        assert _sim_order(follower_order_manager, "abc123") is None

    def test_full_fill_pops_like_check_fills(self, follower_order_manager):
        order = _sim_order(follower_order_manager, "abc123", qty=Decimal("1"))

        follower_order_manager.apply_recorded_fill(
            order.order_id, exec_price=Decimal("100"),
            exec_qty=Decimal("0.4"), exec_fee=Decimal("0"),
            closed_pnl=Decimal("0"), timestamp=_ts(1), exec_id="e1",
        )
        event, full = follower_order_manager.apply_recorded_fill(
            order.order_id, exec_price=Decimal("100"),
            exec_qty=Decimal("0.6"), exec_fee=Decimal("0"),
            closed_pnl=Decimal("0"), timestamp=_ts(2), exec_id="e2",
        )

        assert full is True
        assert event.leaves_qty == Decimal("0")
        assert order.order_id not in follower_order_manager.active_orders
        assert order in follower_order_manager.filled_orders
        assert order.status == "filled"
        # client_order_id released for reuse.
        assert _sim_order(follower_order_manager, "abc123") is not None

    def test_qty_excess_capped_and_prorated(self, follower_order_manager):
        order = _sim_order(follower_order_manager, "abc123", qty=Decimal("0.5"))

        event, full = follower_order_manager.apply_recorded_fill(
            order.order_id,
            exec_price=Decimal("100"),
            exec_qty=Decimal("1.0"),
            exec_fee=Decimal("0.02"),
            closed_pnl=Decimal("2.0"),
            timestamp=_ts(1),
            exec_id="e1",
        )

        assert full is True
        assert event.qty == Decimal("0.5")
        assert event.fee == Decimal("0.01")       # 0.02 × 0.5/1.0
        assert event.closed_pnl == Decimal("1.0")  # 2.0 × 0.5/1.0
        assert follower_order_manager.qty_excess_divergence_count == 1

    def test_unknown_or_consumed_order_is_noop(self, follower_order_manager):
        assert follower_order_manager.apply_recorded_fill(
            "missing", exec_price=Decimal("100"), exec_qty=Decimal("1"),
            exec_fee=Decimal("0"), closed_pnl=Decimal("0"),
            timestamp=_ts(1),
        ) == (None, False)

        order = _sim_order(follower_order_manager, "abc123", qty=Decimal("1"))
        follower_order_manager.apply_recorded_fill(
            order.order_id, exec_price=Decimal("100"), exec_qty=Decimal("1"),
            exec_fee=Decimal("0"), closed_pnl=Decimal("0"),
            timestamp=_ts(1),
        )
        # Fully consumed → no longer active → no-op.
        assert follower_order_manager.apply_recorded_fill(
            order.order_id, exec_price=Decimal("100"), exec_qty=Decimal("1"),
            exec_fee=Decimal("0"), closed_pnl=Decimal("0"),
            timestamp=_ts(2),
        ) == (None, False)


class _StubEngine:
    """GridEngine stand-in: grid mutation + ticker path both no-ops.

    Isolates the runner's fill application/aggregation from grid logic;
    the reactive-close placement path runs against the real engine in the
    replay integration tests.
    """

    def on_event(self, event, limit_orders=None):
        return []


@pytest.fixture
def follower_runner():
    """Runner wired for event_follower with a stubbed engine."""
    order_manager = BacktestOrderManager(
        fill_simulator=TradeThroughFillSimulator(mode=FillMode.EVENT_FOLLOWER),
        commission_rate=Decimal("0.0002"),
    )
    executor = BacktestExecutor(order_manager=order_manager, qty_calculator=None)
    session = BacktestSession(
        session_id="test_event_follower",
        initial_balance=Decimal("10000"),
    )
    config = BacktestStrategyConfig(
        strat_id="test_ltc",
        symbol="LTCUSDT",
        tick_size=Decimal("0.01"),
        grid_count=10,
        grid_step=0.2,
        amount="x1",
        max_margin=8.0,
        commission_rate=Decimal("0.0002"),
        enable_risk_multipliers=False,
    )
    runner = BacktestRunner(
        strategy_config=config,
        executor=executor,
        session=session,
    )
    runner._engine = _StubEngine()
    return runner


def _wire_follower(runner: BacktestRunner, execs: list[RecordedExecution]):
    follower = _follower(execs)
    runner._event_follower = follower
    return follower


class TestRunnerEventFollowerDrain:
    def test_wallet_identity_one_open_close_cycle(self, follower_runner):
        """Open Buy 1 @ 100, close Sell 1 @ 110 with recorded closed_pnl=+10:
        balance = init + 10 − fees, unrealized structurally 0 at flat —
        recorded values land in the wallet exactly once."""
        session = follower_runner._session
        om = follower_runner.order_manager

        _sim_order(om, "gridopen", side="Buy", price=Decimal("100"))
        _wire_follower(follower_runner, [
            _exec("e1", "bo1", link="gridopen-1", side="Buy",
                  price="100", qty="1", fee="0.02", pnl="0", at=1),
            _exec("e2", "bo2", link="gridclose-1", side="Sell",
                  price="110", qty="1", fee="0.022", pnl="10", at=5),
        ])

        follower_runner.process_fills(_ticker(2, price="100"))
        assert len(session.trades) == 1  # open lifecycle flushed (trigger 1)
        open_trade = session.trades[0]
        assert open_trade.client_order_id == "gridopen"
        assert open_trade.order_id == "bo1"
        assert open_trade.realized_pnl == Decimal("0")
        assert open_trade.commission == Decimal("0.02")
        assert follower_runner.long_tracker.state.size == Decimal("1")

        # Reactive close order (placed by the strategy after the open fill;
        # stub engine doesn't place it, so place directly).
        _sim_order(om, "gridclose", side="Sell", price=Decimal("110"))

        follower_runner.process_fills(_ticker(6, price="110"))
        follower_runner.finalize_event_follower()

        assert len(session.trades) == 2
        close_trade = session.trades[1]
        assert close_trade.client_order_id == "gridclose"
        assert close_trade.realized_pnl == Decimal("10")  # recorded, not derived
        assert follower_runner.long_tracker.state.size == Decimal("0")
        assert session.total_realized_pnl == Decimal("10")
        assert session.total_commission == Decimal("0.042")
        # Flat position → unrealized 0 → exact wallet identity.
        assert session.current_balance == Decimal("10000") + Decimal("10") - Decimal("0.042")
        # Pending fully migrated.
        assert session._pending_realized_pnl == Decimal("0")
        assert session._pending_commission == Decimal("0")

    def test_two_partials_update_wallet_per_row_one_trade_on_flush(
        self, follower_runner
    ):
        """Two partial rows, same recorded order_id, across two ticks:
        pending wallet visible after each row (incl. engine-style
        update_equity), ONE aggregated BacktestTrade at the trigger-2
        flush, partial sum < placed qty (no dangling buffer)."""
        session = follower_runner._session
        om = follower_runner.order_manager

        _sim_order(om, "gridp1", side="Buy", price=Decimal("100"), qty=Decimal("1"))
        _wire_follower(follower_runner, [
            _exec("e1", "bo1", link="gridp1-1", side="Buy",
                  price="100", qty="0.4", fee="0.008", pnl="0", at=1),
            _exec("e2", "bo1", link="gridp1-2", side="Buy",
                  price="101", qty="0.4", fee="0.009", pnl="0", at=5),
        ])

        # Tick 1: first partial only. Tail still holds e2 for bo1 → no flush.
        follower_runner.process_fills(_ticker(2, price="100"))
        assert session.trades == []
        assert session._pending_commission == Decimal("0.008")
        # Engine-style update_equity sees the pending fee.
        unrealized = follower_runner.long_tracker.calculate_unrealized_pnl(
            Decimal("100")
        )
        session.update_equity(_ts(2), unrealized)
        assert session.current_balance == (
            Decimal("10000") + unrealized - Decimal("0.008")
        )

        # Tick 2: second partial; stream dry for bo1 → trigger-2 flush even
        # though applied 0.8 < placed 1.0 (intent-set divergence surfaces as
        # qty mismatch, not a dangling pending buffer).
        follower_runner.process_fills(_ticker(6, price="101"))

        assert len(session.trades) == 1
        trade = session.trades[0]
        assert trade.client_order_id == "gridp1"
        assert trade.order_id == "bo1"
        assert trade.qty == Decimal("0.8")
        # VWAP of 0.4@100 + 0.4@101.
        assert trade.price == Decimal("100.5")
        assert trade.commission == Decimal("0.017")
        assert session._pending_commission == Decimal("0")
        # Order still active with the un-filled remainder.
        assert om.active_orders  # gridp1 remainder 0.2

    def test_same_tick_multi_partial_no_split(self, follower_runner):
        """Both partials inside ONE tick window: intra-loop must not flush
        on the first row; trigger 2 fires once at the fixpoint → one trade."""
        session = follower_runner._session
        om = follower_runner.order_manager

        _sim_order(om, "gridp1", side="Buy", price=Decimal("100"), qty=Decimal("1"))
        _wire_follower(follower_runner, [
            _exec("e1", "bo1", link="gridp1-1", price="100", qty="0.4",
                  fee="0.008", at=1),
            _exec("e2", "bo1", link="gridp1-2", price="100", qty="0.4",
                  fee="0.008", at=2),
        ])

        follower_runner.process_fills(_ticker(3, price="100"))

        assert len(session.trades) == 1
        assert session.trades[0].qty == Decimal("0.8")

    def test_cancel_with_partial_flushes_before_cancel(self, follower_runner):
        """Trigger 3: cancel of a partially-filled order flushes its rollup
        before execute_cancel removes it from active_orders."""
        session = follower_runner._session
        om = follower_runner.order_manager

        order = _sim_order(
            om, "gridc1", side="Buy", price=Decimal("100"), qty=Decimal("1")
        )
        # Tail keeps a future row for bo1 so trigger 2 does NOT fire at the
        # tick fixpoint — only the cancel can flush.
        _wire_follower(follower_runner, [
            _exec("e1", "bo1", link="gridc1-1", price="100", qty="0.4",
                  fee="0.008", at=1),
            _exec("e2", "bo1", link="gridc1-2", price="100", qty="0.4",
                  fee="0.008", at=50),
        ])

        follower_runner.process_fills(_ticker(2, price="100"))
        assert session.trades == []  # rollup pending

        follower_runner._dispatch_intents(
            [CancelIntent(symbol="LTCUSDT", order_id=order.order_id,
                          reason="outside_grid")],
            _ts(3),
        )

        assert len(session.trades) == 1
        assert session.trades[0].qty == Decimal("0.4")
        assert order.order_id not in om.active_orders
        assert session._pending_commission == Decimal("0")

    def test_zero_execution_tick_is_clean_noop(self, follower_runner):
        """Tick window with no recorded executions: drain returns [], the
        fixpoint loop exits immediately, trigger 2 has nothing to flush,
        pending wallet stays zero, and the window still advances."""
        session = follower_runner._session
        om = follower_runner.order_manager

        _sim_order(om, "gridopen", side="Buy", price=Decimal("100"))
        # Single execution far beyond this tick's window.
        _wire_follower(follower_runner, [
            _exec("e1", "bo1", link="gridopen-1", price="100", qty="1", at=50),
        ])

        intents = follower_runner.process_fills(_ticker(2, price="100"))

        assert intents == []
        assert session.trades == []
        assert session._pending_realized_pnl == Decimal("0")
        assert session._pending_commission == Decimal("0")
        assert follower_runner.long_tracker.state.size == Decimal("0")
        assert follower_runner._follower_live_only_count == 0
        # Window advanced — the future row drains on a later tick.
        assert follower_runner._prev_tick_ts == _ts(2)

    def test_no_match_means_no_position_change(self, follower_runner):
        """A live fill with no matching active order stays live_only — the
        backtest never invents a fill."""
        session = follower_runner._session

        _wire_follower(follower_runner, [
            _exec("e1", "bo1", link="neverplaced-1", price="100", qty="1", at=1),
        ])

        follower_runner.process_fills(_ticker(2, price="100"))
        follower_runner.finalize_event_follower()

        assert session.trades == []
        assert follower_runner.long_tracker.state.size == Decimal("0")
        assert follower_runner.short_tracker.state.size == Decimal("0")
        assert follower_runner._follower_live_only_count == 1

    def test_execution_at_start_ts_drained_on_first_tick(self, follower_runner):
        """Runner-level left-edge check: exec at exactly start_ts fills."""
        om = follower_runner.order_manager
        _sim_order(om, "gridopen", side="Buy", price=Decimal("100"))
        _wire_follower(follower_runner, [
            _exec("e1", "bo1", link="gridopen-1", price="100", qty="1", at=0),
        ])

        follower_runner.process_fills(_ticker(1, price="100"))

        assert follower_runner.long_tracker.state.size == Decimal("1")
        assert len(follower_runner._session.trades) == 1

    def test_simulator_path_untouched_when_no_follower(self, follower_runner):
        """_event_follower is None ⇒ process_fills runs the simulator path
        (acceptance #5 guard at the unit level)."""
        runner = follower_runner
        runner._event_follower = None
        # No orders, simulator mode raises only if check_fill is reached per
        # order; with zero active orders process_fills is a clean no-op.
        intents = runner.process_fills(_ticker(1, price="100"))
        assert intents == []
        assert runner._session._pending_realized_pnl == Decimal("0")
