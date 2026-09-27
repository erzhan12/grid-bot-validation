"""Characterization tests for the 2026-09-27 audit, NOT acceptance tests.

Passing means the documented unsafe behavior was reproduced. These tests use
synthetic inputs and in-memory SQLite only. Run explicitly from the repo root:
uv run --no-sync pytest docs/audits/live_check_2026_09_27/test_audit_reproductions.py -q
"""

from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backtest.config import BacktestStrategyConfig
from backtest.data_provider import InMemoryDataProvider
from backtest.executor import BacktestExecutor
from backtest.fill_simulator import EventFollower, FillMode, RecordedExecution, TradeThroughFillSimulator
from backtest.order_manager import BacktestOrderManager
from backtest.runner import BacktestRunner
from backtest.session import BacktestSession
from comparator.loader import BacktestTradeLoader, LiveTradeLoader
from comparator.matcher import TradeMatcher
from event_saver.reconciler import GapReconciler
from grid_db import DatabaseFactory, DatabaseSettings, PositionSnapshot, PrivateExecution, TickerSnapshot, WalletSnapshot
from grid_db.models import BybitAccount, Run, Strategy, User
from gridcore import EventType, PlaceLimitIntent, TickerEvent
from gridcore.instrument_info import InstrumentInfo
from live_check import main as lc
from live_check.config import LiveCheckConfig, StratCheckConfig, VerdictThresholds
from live_check.ground_truth import GroundTruth
from live_check.shared_wallet import WalletCurvePoint, reconcile_wallet_curve
from live_check.verdict import evaluate, evaluate_shared_wallet
from live_check.window import Window
from replay.engine import ReplayEngine
from replay.config import FillSimulatorConfig, ReplayConfig, ReplayStrategyConfig
from replay.snapshot_loader import load_position_snapshots, load_wallet_seed_full

T0 = datetime(2026, 9, 27, 8)
SYMBOL = "LTCUSDT"


def tick(at, price="100"):
    ts = T0 + timedelta(seconds=at)
    return TickerEvent(event_type=EventType.TICKER, symbol=SYMBOL,
                       exchange_ts=ts, local_ts=ts, last_price=D(price), mark_price=D(price))


def execution(key="open", at=1, side="Buy", price="100", qty="1", pnl="0", link=True):
    return RecordedExecution(exec_id=f"exec-{key}", order_id=f"live-{key}",
                             order_link_id=f"{key}-1" if link else None,
                             side=side, exec_price=D(price), exec_qty=D(qty),
                             exec_fee=D("0.02"), closed_pnl=D(pnl),
                             exchange_ts=T0 + timedelta(seconds=at))


def runner_for(rows):
    om = BacktestOrderManager(TradeThroughFillSimulator(FillMode.EVENT_FOLLOWER), D("0.0002"))
    session = BacktestSession(initial_balance=D("10000"))
    config = BacktestStrategyConfig(strat_id="audit", symbol=SYMBOL,
                                   tick_size=D("0.1"), enable_risk_multipliers=False)
    with patch.object(BacktestRunner, "_load_mm_tiers", return_value=None):
        runner = BacktestRunner(config, BacktestExecutor(om, qty_calculator=None), session)
    runner._engine = MagicMock()
    runner._engine.on_event.return_value = []
    runner._event_follower = EventFollower(rows, SYMBOL, T0)
    return runner


def place(runner, key="open", qty="1", price="100", side="Buy", at=0):
    return runner.order_manager.place_order(
        client_order_id=key, symbol=SYMBOL, side=side, price=D(price), qty=D(qty),
        direction="long", grid_level=0, timestamp=T0 + timedelta(seconds=at))


def finish(runner, rows, price="100"):
    runner.finalize_event_follower()
    upnl = (runner.long_tracker.calculate_unrealized_pnl(D(price))
            + runner.short_tracker.calculate_unrealized_pnl(D(price)))
    runner._session.finalize(upnl)
    # Use the actual live aggregation and actual comparator, not stub matches.
    loader = LiveTradeLoader(MagicMock())
    live = [loader._aggregate_fills(row.order_link_id.split("-")[0]
                                   if row.order_link_id else row.order_id,
                                   [NS(**asdict(row), symbol=SYMBOL)]) for row in rows]
    result = NS(session=runner._session, runner=runner,
                match_result=TradeMatcher().match(live, BacktestTradeLoader().load_from_session(runner._session.trades)))
    truth = GroundTruth(sum_realized=sum((x.closed_pnl for x in rows), D(0)),
                        sum_commission=sum((x.exec_fee for x in rows), D(0)),
                        net_unrealised=D(0), live_exec_count=len(rows))
    return result, evaluate(result, truth, VerdictThresholds())


def test_wrong_independent_pnl_still_passes(caplog):
    """A wrong entry basis creates $20 calculated vs $10 recorded, yet PASS."""
    rows = [execution("close", side="Sell", price="110", pnl="10")]
    runner = runner_for(rows)
    runner.long_tracker.process_fill("Buy", D(1), D(90))
    place(runner, "close", side="Sell", price="110")
    runner.process_fills(tick(2, "110"))
    result, verdict = finish(runner, rows, "110")
    assert runner.long_tracker.state.realized_pnl == D(20)
    assert result.session.metrics.total_realized_pnl == D(10)
    assert "basis drift" in caplog.text
    assert verdict.passed


@pytest.mark.parametrize("oversized,extra", [(True, False), (False, True)])
def test_wrong_order_book_still_passes(oversized, extra):
    """10x placed qty or an extra opening order is invisible to the verdict."""
    rows = [execution()]
    runner = runner_for(rows)
    original = place(runner, qty="10" if oversized else "1")
    if extra:
        place(runner, "extra", qty="100", price="99")
    runner.process_fills(tick(2))
    _, verdict = finish(runner, rows)
    assert runner.order_manager.total_active_orders == 1
    if oversized:
        assert original.qty == D(9)
    assert verdict.qty_ok and verdict.backtest_only_count == 0 and verdict.passed


def test_missing_link_matches_impossible_buy_limit_and_passes():
    """Missing link allows Buy limit 1 to consume live fill 100 and PASS."""
    rows = [execution(link=False)]
    runner = runner_for(rows)
    place(runner, "unrelated", price="1")
    runner.process_fills(tick(2))
    _, verdict = finish(runner, rows)
    assert runner._event_follower.fallback_price_count == 1
    assert verdict.passed


def test_fixpoint_fills_order_before_its_creation_and_uses_future_tick():
    """A fill at t=1 is retried against an order created at t=2, using t=10 data."""
    rows = [execution("early", at=1), execution("trigger", at=2)]
    runner = runner_for(rows)
    place(runner, "trigger")
    synthetic = []

    def react(event, limit_orders=None):
        if event.event_type == EventType.TICKER:
            synthetic.append((event.exchange_ts, event.last_price))
            if event.exchange_ts == T0 + timedelta(seconds=2):
                return [PlaceLimitIntent(symbol=SYMBOL, side="Buy", price=D(100),
                                         qty=D(1), direction="long", grid_level=0,
                                         client_order_id="early", reduce_only=False)]
        return []

    runner._engine.on_event.side_effect = react
    runner.process_fills(tick(10, "120"))
    # Return to 100 to isolate chronology from unrealized PnL.
    _, verdict = finish(runner, rows)
    retro = next(o for o in runner.order_manager.filled_orders if o.client_order_id == "early")
    assert retro.filled_ts < retro.created_ts
    assert synthetic[0] == (T0 + timedelta(seconds=2), D(120))
    assert verdict.passed


def test_rest_backfill_drops_documented_bybit_execution_shape():
    """REST category belongs to result, not to each list item."""
    row = {"symbol": SYMBOL, "execId": "rest-1", "orderId": "order-1",
           "orderLinkId": "open-1", "side": "Buy", "execPrice": "100",
           "execQty": "1", "execFee": "0.02", "execType": "Trade",
           "execTime": "1790481601000", "closedSize": "0"}
    reconciler = GapReconciler(MagicMock(), MagicMock())
    ids = dict(user_id=uuid4(), account_id=uuid4(), run_id=uuid4())
    assert reconciler._executions_to_models(**ids, executions=[row]) == []
    # Even after category repair, an unknown REST close PnL is silently zero.
    repaired = reconciler._executions_to_models(
        **ids, executions=[row | {"category": "linear", "side": "Sell", "closedSize": "1"}])
    assert len(repaired) == 1 and repaired[0].closed_pnl == D(0)


@pytest.fixture
def db():
    database = DatabaseFactory(DatabaseSettings(database_url="sqlite:///:memory:"))
    database.create_tables()
    with database.get_session() as s:
        user = User(username="audit", email="audit@example.invalid")
        s.add(user)
        s.flush()
        account = BybitAccount(user_id=user.user_id, account_name="audit", environment="testnet")
        s.add(account)
        s.flush()
        strategy = Strategy(account_id=account.account_id, strategy_type="GridStrategy", symbol=SYMBOL, config_json={})
        s.add(strategy)
        s.flush()
        s.add(Run(run_id="audit-run", user_id=user.user_id, account_id=account.account_id,
                  strategy_id=strategy.strategy_id, run_type="recording", start_ts=T0 - timedelta(days=2)))
        account_id = str(account.account_id)
    yield NS(db=database, account_id=account_id, run_id="audit-run")
    database.engine.dispose()


def config():
    strat = StratCheckConfig(strat_id="audit", symbol=SYMBOL, tick_size="0.1",
                             grid_count=20, grid_step=0.4, amount="100",
                             min_total_margin=3, max_margin=5)
    return LiveCheckConfig(strats=[strat], run_id=None)


def test_fresh_public_ticker_masks_stale_private_data(db):
    """Watch prints PASS with private snapshots a day old and a fresh ticker."""
    rows = [execution()]
    runner = runner_for(rows)
    place(runner)
    runner.process_fills(tick(2))
    result, _ = finish(runner, rows)
    with db.db.get_session() as s:
        e = rows[0]
        s.add(PrivateExecution(run_id=db.run_id, account_id=db.account_id, symbol=SYMBOL,
                               exec_id=e.exec_id, order_id=e.order_id, order_link_id=e.order_link_id,
                               exchange_ts=e.exchange_ts, side=e.side, exec_price=e.exec_price,
                               exec_qty=e.exec_qty, exec_fee=e.exec_fee, closed_pnl=e.closed_pnl))
        s.add(TickerSnapshot(symbol=SYMBOL, exchange_ts=T0 + timedelta(minutes=8),
                             local_ts=T0 + timedelta(minutes=8), last_price=D(100), mark_price=D(100),
                             bid1_price=D("99.9"), ask1_price=D("100.1"), funding_rate=D(0)))
        for side in ("Buy", "Sell"):
            s.add(PositionSnapshot(run_id=db.run_id, account_id=db.account_id,
                                   symbol=SYMBOL, side=side, size=D(0), entry_price=D(0),
                                   unrealised_pnl=D(0), source="live", exchange_ts=T0 - timedelta(days=1),
                                   local_ts=T0 - timedelta(days=1)))
    with patch.object(lc.runner, "run_strat", return_value=result):
        lines = lc.watch_tick(config(), db.db, db.run_id, db.account_id,
                              timedelta(minutes=10), timedelta(minutes=2),
                              timedelta(minutes=5), now=T0 + timedelta(minutes=10))
    assert "✓" in lines[0] and "SKIP" not in lines[0]


def test_zero_available_wallet_is_discarded_and_other_symbol_satisfies_precheck(db):
    """A fully margined wallet is rejected; missing target positions become flat."""
    with db.db.get_session() as s:
        s.add(WalletSnapshot(run_id=db.run_id, account_id=db.account_id, coin="USDT",
                             wallet_balance=D(100), available_balance=D(0), total_available_balance=D(0),
                             total_equity=D(100), exchange_ts=T0 - timedelta(minutes=1),
                             local_ts=T0 - timedelta(minutes=1)))
        s.add(PositionSnapshot(run_id=db.run_id, account_id=db.account_id,
                               symbol="SOLUSDT", side="Buy", size=D(1), entry_price=D(100),
                               exchange_ts=T0 - timedelta(minutes=1), source="live",
                               local_ts=T0 - timedelta(minutes=1)))
    with db.db.get_readonly_session() as s:
        ReplayEngine._seed_pre_check(s, db.run_id, T0)
        assert load_wallet_seed_full(s, db.run_id, db.account_id, T0) is None
        long, short = load_position_snapshots(s, db.run_id, db.account_id, SYMBOL, T0)
        assert long.size == short.size == D(0)


def test_watch_never_rediscovers_run_after_recorder_restart():
    """Two watch ticks stay on the original recording run."""
    observed = []
    args = NS(watch="1s", last="4h", lag="2m")
    with patch.object(lc, "_resolve_run", side_effect=[("old", "account", T0 - timedelta(days=2)),
                                                    ("new", "account", T0 - timedelta(days=1))]) as resolve, \
         patch.object(lc, "compute_window", return_value=Window(T0, T0 + timedelta(hours=4))), \
         patch.object(lc, "watch_tick", side_effect=lambda c, d, r, *a: observed.append(r) or []), \
         patch.object(lc.time, "sleep", side_effect=[None, KeyboardInterrupt]):
        with pytest.raises(KeyboardInterrupt):
            lc.run_watch(config(), args, MagicMock())
    assert observed == ["old", "old"]
    assert resolve.call_count == 1


def test_shared_equity_gate_passes_when_only_first_sample_is_covered():
    """A matching start sample masks unvalidated $100 terminal divergence."""
    replay = [NS(exchange_ts=T0, total_equity=D(100), total_margin_balance=D(100), account_mm_rate=D(0)),
              NS(exchange_ts=T0 + timedelta(hours=4), total_equity=D(200),
                 total_margin_balance=D(200), account_mm_rate=D(0))]
    recorded = [WalletCurvePoint(T0, D(100), D(100), D(0))]
    diff = reconcile_wallet_curve(replay, recorded)
    verdict = evaluate_shared_wallet({"audit": NS(passed=True)}, diff, VerdictThresholds())
    assert diff.equity_points == 1 and diff.final_equity_delta == 0
    assert verdict.passed


def test_full_replay_accepts_tenfold_amount_with_real_grid_engine(db):
    """Full replay, real strategy/matcher: wrong amount remains a green verdict."""
    cfg = ReplayConfig(database_url="sqlite:///:memory:", run_id=db.run_id,
                       symbol=SYMBOL, start_ts=T0, end_ts=T0 + timedelta(seconds=3),
                       strategy=ReplayStrategyConfig(strat_id="audit", tick_size=D("0.1"),
                                                     grid_step=0.4, grid_count=20, amount="100",
                                                     enable_risk_multipliers=False),
                       fill_simulator=FillSimulatorConfig(mode="event_follower"), enable_funding=False)
    info = InstrumentInfo(SYMBOL, D("0.01"), D("0.1"), D("0.01"), D("100000"))
    with patch("replay.engine.InstrumentInfoProvider") as provider:
        provider.return_value.get.return_value = info
        reference = ReplayEngine(cfg, db.db, emit_backtest_snapshots=False).run(InMemoryDataProvider([tick(0)]))
        order = max((o for o in reference.runner.order_manager.active_orders.values()
                     if o.side == "Buy" and o.direction == "long"), key=lambda o: o.price)
        with db.db.get_session() as s:
            s.add(PrivateExecution(run_id=db.run_id, account_id=db.account_id, symbol=SYMBOL,
                                   exec_id="actual", order_id="live-order", order_link_id=order.client_order_id + "-1",
                                   exchange_ts=T0 + timedelta(seconds=1), side="Buy", exec_price=order.price,
                                   exec_qty=order.qty, exec_fee=D("0.02"), closed_pnl=D(0)))
        cfg.strategy.amount = "1000"
        result = ReplayEngine(cfg, db.db, emit_backtest_snapshots=False).run(
            InMemoryDataProvider([tick(0), tick(2, str(order.price))]))
    truth = GroundTruth(D(0), D("0.02"), D(0), 1)
    verdict = evaluate(result, truth, VerdictThresholds())
    leftover = result.runner.order_manager.get_order_by_client_id(order.client_order_id)
    assert leftover.qty > order.qty * 8
    assert verdict.passed and verdict.qty_mismatch_count == 0
