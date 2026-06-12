"""Integration tests for the event_follower fill mode (feature 0072).

Equal-strategy round-trip: a replay run whose recorded ``private_executions``
were produced by the SAME strategy (mirrored by a twin "oracle" GridEngine in
the test) must reproduce live exactly:

- ``live_only_count = 0`` and ``backtest_only_count = 0`` (by construction),
- ``cumulative_pnl_delta = 0`` and ``total_backtest_pnl = Σ recorded
  closed_pnl``,
- one aggregated ``BacktestTrade`` per ``(matcher_key, order_id)`` lifecycle
  (partial-fill fixture: two execution rows, same order_id, one trade on
  both sides),
- the open→close-same-tick-window fixture passes via the iterative drain
  (the reactive close order is placed mid-drain at the fill's exchange_ts
  and matched by a later buffered execution in the same window),
- other-symbol execution rows are excluded at engine materialization.

The oracle mirrors exactly what the runner does in event_follower mode:
ticker → intents → (dedup + reduce-only gate + qty resolution) → book;
fill → engine execution event → synthetic ticker at fill ts → intents.
Identity ids depend only on (symbol, side, price, direction), so the
oracle's client ids equal the replay strategy's.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from gridcore import (
    CancelIntent,
    EventType,
    ExecutionEvent,
    GridConfig,
    GridEngine,
    PlaceLimitIntent,
    TickerEvent,
)
from grid_db.models import PrivateExecution

from backtest.data_provider import InMemoryDataProvider
from backtest.fill_simulator import FillMode

from replay.config import (
    FillSimulatorConfig,
    ReplayConfig,
    ReplayStrategyConfig,
)
from replay.engine import ReplayEngine


TS = datetime(2025, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
PRICE = Decimal("100000")
GRID_COUNT = 10
GRID_STEP = 0.2


def _make_tick(price: Decimal, ts: datetime) -> TickerEvent:
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        exchange_ts=ts,
        local_ts=ts,
        last_price=price,
        mark_price=price,
        bid1_price=price - Decimal("1"),
        ask1_price=price + Decimal("1"),
        funding_rate=Decimal("0.0001"),
    )


def _round_qty(q: Decimal) -> Decimal:
    """Same rounding as the mocked InstrumentInfo below."""
    return max(Decimal("0.001"), q.quantize(Decimal("0.001")))


class _Oracle:
    """Twin GridEngine mirroring the runner's event_follower flow.

    Maintains a mini order book with the same dedup
    (``client_order_id`` set), reduce-only close gate
    (``pos > pending_close + qty``) and fixed-USDT qty resolution the
    runner/executor apply, so the orders it tracks are exactly the orders
    the replay run will hold at each point in the recorded stream.
    """

    def __init__(self):
        self.engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            config=GridConfig(grid_count=GRID_COUNT, grid_step=GRID_STEP),
            strat_id="replay_btcusdt",
            anchor_price=None,
        )
        self.orders: dict[str, dict] = {}  # client_id -> order record
        self._counter = 0
        self.pos = Decimal("0")  # long position size

    def _qty(self, price: Decimal) -> Decimal:
        # amount="100" (fixed USDT) + mocked round_qty.
        return _round_qty(Decimal("100") / price)

    def _limit_orders(self) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {"long": [], "short": []}
        for o in self.orders.values():
            result[o["direction"]].append({
                "price": str(o["price"]),
                "qty": str(o["qty"]),
                "side": o["side"],
                "orderId": o["orderId"],
                "orderLinkId": o["clientId"],
            })
        return result

    def _pending_close(self) -> Decimal:
        return sum(
            (
                o["qty"]
                for o in self.orders.values()
                if o["reduce_only"] and o["direction"] == "long"
            ),
            Decimal("0"),
        )

    def _apply(self, intents) -> None:
        for it in intents:
            if isinstance(it, CancelIntent):
                self.orders = {
                    c: o
                    for c, o in self.orders.items()
                    if o["orderId"] != it.order_id
                }
            elif isinstance(it, PlaceLimitIntent):
                if it.client_order_id in self.orders:
                    continue  # _client_order_ids dedup
                qty = self._qty(it.price)
                if it.reduce_only and not (
                    self.pos > self._pending_close() + qty
                ):
                    continue  # runner _should_place_close gate
                self._counter += 1
                self.orders[it.client_order_id] = {
                    "orderId": f"oracle_{self._counter}",
                    "clientId": it.client_order_id,
                    "side": it.side,
                    "price": it.price,
                    "qty": qty,
                    "direction": it.direction,
                    "reduce_only": it.reduce_only,
                }

    def tick(self, event: TickerEvent) -> None:
        self._apply(self.engine.on_event(event, self._limit_orders()))

    def fill(self, client_id: str, qty: Decimal, ts: datetime) -> Decimal:
        """Mirror one applied recorded fill row; returns the fill price."""
        o = self.orders[client_id]
        price = o["price"]
        if o["direction"] == "long":
            self.pos += qty if o["side"] == "Buy" else -qty
        o["qty"] -= qty
        if o["qty"] <= 0:
            del self.orders[client_id]
        exec_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            exec_id=f"oracle_exec_{client_id}_{ts.isoformat()}",
            order_id=o["orderId"],
            order_link_id=client_id,
            side=o["side"],
            price=price,
            qty=qty,
        )
        self.engine.on_event(exec_event)  # grid mutation (returns [])
        # Reactive ticker path at the fill's exchange_ts (drain-loop mirror).
        self._apply(
            self.engine.on_event(
                _make_tick(PRICE, ts), self._limit_orders()
            )
        )
        return price

    def best_open_buy(self) -> dict:
        buys = [
            o
            for o in self.orders.values()
            if o["side"] == "Buy"
            and o["direction"] == "long"
            and not o["reduce_only"]
        ]
        return max(buys, key=lambda o: o["price"])

    def active_long_close(self) -> dict:
        closes = [
            o
            for o in self.orders.values()
            if o["reduce_only"] and o["direction"] == "long"
        ]
        assert closes, "oracle: no reactive close order was placed"
        return closes[0]


def _replay_config(ts) -> ReplayConfig:
    return ReplayConfig(
        database_url="sqlite:///:memory:",
        run_id="test-run-id",
        symbol="BTCUSDT",
        start_ts=ts,
        end_ts=ts + timedelta(hours=1),
        strategy=ReplayStrategyConfig(
            tick_size=Decimal("0.1"),
            grid_count=GRID_COUNT,
            grid_step=GRID_STEP,
            amount="100",  # fixed USDT → deterministic qty
            commission_rate=Decimal("0.0002"),
        ),
        fill_simulator=FillSimulatorConfig(mode="event_follower"),
        initial_balance=Decimal("10000"),
        enable_funding=False,
        output_dir="results/test_event_follower",
    )


def _insert_execution(db, *, exec_id, order_id, link, side, price, qty,
                      fee, pnl, ts, symbol="BTCUSDT"):
    with db.get_session() as session:
        session.add(PrivateExecution(
            run_id="test-run-id",
            account_id="acc1",
            symbol=symbol,
            exec_id=exec_id,
            order_id=order_id,
            order_link_id=link,
            exchange_ts=ts,
            side=side,
            exec_price=price,
            exec_qty=qty,
            exec_fee=fee,
            closed_pnl=pnl,
        ))


@pytest.fixture
def mock_instrument():
    with patch("replay.engine.InstrumentInfoProvider") as mock_provider_cls:
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = _round_qty
        mock_provider_cls.return_value.get.return_value = mock_info
        yield mock_provider_cls


class TestEventFollowerRoundTrip:
    def test_config_accepts_event_follower_mode(self):
        assert FillSimulatorConfig(mode="event_follower").mode == "event_follower"

    def test_equal_strategy_round_trip(self, mock_instrument, db,
                                       seeded_run_account):
        """Strategy == live ⇒ live_only=0, backtest_only=0, pnl delta 0.

        Fixture stack (all inside ONE tick window — open→close same window):
        - order A: TWO partial execution rows (same order_id) → one
          aggregated trade on both sides;
        - order B: one full fill (position now exceeds close qty, so the
          reactive close passes the gate);
        - order C: the reactive close, placed mid-drain at the fill's
          exchange_ts — its execution only matches on a later fixpoint
          iteration;
        - one ETHUSDT row with order A's link id, EARLIER than every
          BTCUSDT row: if engine materialization failed to filter by
          symbol it would consume order A prematurely and break every
          assert below (canary).
        """
        oracle = _Oracle()
        tick1 = _make_tick(PRICE, TS)
        oracle.tick(tick1)  # build grid + initial placements

        t = TS + timedelta(seconds=70)  # inside tick2's window

        # Order A: two partials at the best buy level.
        a = oracle.best_open_buy()
        a_cid, a_price = a["clientId"], a["price"]
        a_qty = a["qty"]
        assert a_qty == Decimal("0.001")
        half = Decimal("0.0005")
        oracle.fill(a_cid, half, t)
        oracle.fill(a_cid, half, t + timedelta(seconds=1))

        # Order B: full fill at the next buy level.
        b = oracle.best_open_buy()
        b_cid, b_price, b_qty = b["clientId"], b["price"], b["qty"]
        assert b_cid != a_cid
        oracle.fill(b_cid, b_qty, t + timedelta(seconds=2))

        # Order C: reactive close placed by the engine after the B fill.
        c = oracle.active_long_close()
        c_cid, c_price, c_qty = c["clientId"], c["price"], c["qty"]
        avg_entry = (a_price * a_qty + b_price * b_qty) / (a_qty + b_qty)
        close_pnl = c_qty * (c_price - avg_entry)
        oracle.fill(c_cid, c_qty, t + timedelta(seconds=3))

        # Recorded stream (the "live" side). ETH canary first.
        _insert_execution(db, exec_id="eth1", order_id="LIVE_ETH",
                          link=f"{a_cid}-9000", side="Buy",
                          price=a_price, qty=Decimal("0.001"),
                          fee=Decimal("0.001"), pnl=Decimal("0"),
                          ts=t - timedelta(seconds=5), symbol="ETHUSDT")
        _insert_execution(db, exec_id="e1a", order_id="LIVE_A",
                          link=f"{a_cid}-1001", side="Buy",
                          price=a_price, qty=half,
                          fee=Decimal("0.010"), pnl=Decimal("0"), ts=t)
        _insert_execution(db, exec_id="e1b", order_id="LIVE_A",
                          link=f"{a_cid}-1002", side="Buy",
                          price=a_price, qty=half,
                          fee=Decimal("0.010"), pnl=Decimal("0"),
                          ts=t + timedelta(seconds=1))
        _insert_execution(db, exec_id="e2", order_id="LIVE_B",
                          link=f"{b_cid}-1003", side="Buy",
                          price=b_price, qty=b_qty,
                          fee=Decimal("0.020"), pnl=Decimal("0"),
                          ts=t + timedelta(seconds=2))
        _insert_execution(db, exec_id="e3", order_id="LIVE_C",
                          link=f"{c_cid}-1004", side="Sell",
                          price=c_price, qty=c_qty,
                          fee=Decimal("0.021"), pnl=close_pnl,
                          ts=t + timedelta(seconds=3))

        ticks = [
            tick1,
            _make_tick(PRICE, TS + timedelta(minutes=2)),
            _make_tick(PRICE, TS + timedelta(minutes=3)),
        ]
        engine = ReplayEngine(config=_replay_config(TS), db=db)
        result = engine.run(data_provider=InMemoryDataProvider(ticks))

        m = result.metrics
        assert result.fill_mode == FillMode.EVENT_FOLLOWER
        # Live side: 3 aggregated trades (A partials → one), backtest: same.
        assert m.total_live_trades == 3
        assert m.total_backtest_trades == 3
        assert m.matched_count == 3
        assert m.live_only_count == 0
        assert m.backtest_only_count == 0
        # Recorded closed_pnl is authoritative on both sides.
        assert m.total_backtest_pnl == close_pnl
        assert m.cumulative_pnl_delta == Decimal("0")
        assert m.fee_delta == Decimal("0")
        # Wallet identity: init + Σ closed_pnl − Σ fees + unrealized.
        session = result.session
        assert session.total_realized_pnl == close_pnl
        assert session.total_commission == Decimal("0.061")
        # Pending fully migrated (trigger sweep ran).
        assert session._pending_realized_pnl == Decimal("0")
        assert session._pending_commission == Decimal("0")

        # Partial-fill aggregation parity: order A produced ONE backtest
        # trade with summed qty, matching the live aggregation.
        a_trades = [tr for tr in session.trades if tr.client_order_id == a_cid]
        assert len(a_trades) == 1
        assert a_trades[0].qty == a_qty
        assert a_trades[0].order_id == "LIVE_A"

    def test_differ_strategy_yields_live_only(self, mock_instrument, db,
                                              seeded_run_account):
        """A live fill the strategy never placed an order for stays
        live_only; the backtest invents nothing (null-result direction)."""
        _insert_execution(db, exec_id="e1", order_id="LIVE_X",
                          link="deadbeefdeadbeef-1", side="Buy",
                          price=Decimal("99800"), qty=Decimal("0.001"),
                          fee=Decimal("0.01"), pnl=Decimal("0"),
                          ts=TS + timedelta(seconds=70))

        ticks = [
            _make_tick(PRICE, TS),
            _make_tick(PRICE, TS + timedelta(minutes=2)),
        ]
        engine = ReplayEngine(config=_replay_config(TS), db=db)
        result = engine.run(data_provider=InMemoryDataProvider(ticks))

        m = result.metrics
        assert m.live_only_count == 1
        assert m.backtest_only_count == 0
        assert m.total_backtest_trades == 0
        assert result.session.trades == []

    def test_last_cross_default_unaffected(self, mock_instrument, db,
                                           seeded_run_account):
        """Acceptance #5: default mode stays last_cross and runs the
        simulator path even with executions present in the window."""
        _insert_execution(db, exec_id="e1", order_id="LIVE_X",
                          link="deadbeefdeadbeef-1", side="Buy",
                          price=Decimal("99800"), qty=Decimal("0.001"),
                          fee=Decimal("0.01"), pnl=Decimal("0"),
                          ts=TS + timedelta(seconds=70))
        config = _replay_config(TS)
        config = config.model_copy(
            update={"fill_simulator": FillSimulatorConfig()}
        )
        engine = ReplayEngine(config=config, db=db)
        result = engine.run(
            data_provider=InMemoryDataProvider([_make_tick(PRICE, TS)])
        )

        assert result.fill_mode == FillMode.LAST_CROSS
