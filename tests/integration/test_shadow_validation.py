"""Integration test: Shadow-mode validation pipeline.

Validates that feeding identical price data through two independently
constructed paths (BacktestEngine vs manual GridEngine+OrderManager
simulation) produces identical trades that match 100% through the
comparator pipeline.

This is the key integration test for the validation framework: it proves
that the backtest faithfully reproduces the same trading decisions as
the live execution path, using deterministic client_order_id matching.
"""

import math
import pytest
from dataclasses import replace
from decimal import Decimal

from gridcore import DirectionType

from backtest.config import BacktestConfig, BacktestStrategyConfig, WindDownMode
from backtest.engine import BacktestEngine
from backtest.data_provider import InMemoryDataProvider
from backtest.executor import BacktestExecutor
from backtest.fill_simulator import TradeThroughFillSimulator
from backtest.order_manager import BacktestOrderManager
from backtest.position_tracker import BacktestPositionTracker
from backtest.runner import BacktestRunner
from backtest.session import BacktestSession

from comparator.loader import BacktestTradeLoader
from comparator.matcher import TradeMatcher
from comparator.metrics import calculate_metrics

from tests.integration.conftest import generate_price_series


# Shared configuration constants
SYMBOL = "BTCUSDT"
STRAT_ID = "shadow_test"
GRID_COUNT = 20
GRID_STEP = 0.5
TICK_SIZE = "0.1"
AMOUNT = "1000"
INITIAL_BALANCE = Decimal("100000")

# Price series parameters that reliably produce trades
START_PRICE = 100000.0
AMPLITUDE = 2000.0
NUM_TICKS = 500
INTERVAL_SECONDS = 60

# Instrument defaults (matching InstrumentInfoProvider fallback)
QTY_STEP = Decimal("0.001")


def _make_strategy_config():
    """Create BacktestStrategyConfig with shared constants."""
    return BacktestStrategyConfig(
        strat_id=STRAT_ID,
        symbol=SYMBOL,
        tick_size=TICK_SIZE,
        grid_count=GRID_COUNT,
        grid_step=GRID_STEP,
        amount=AMOUNT,
    )


def _make_backtest_config():
    """Create BacktestConfig with shared constants."""
    strategy = _make_strategy_config()
    return BacktestConfig(
        strategies=[strategy],
        initial_balance=INITIAL_BALANCE,
        wind_down_mode=WindDownMode.LEAVE_OPEN,
        enable_funding=False,
    )


def _make_price_events():
    """Generate deterministic price events."""
    return generate_price_series(
        symbol=SYMBOL,
        start_price=START_PRICE,
        amplitude=AMPLITUDE,
        num_ticks=NUM_TICKS,
        interval_seconds=INTERVAL_SECONDS,
    )


def _qty_calculator(intent, wallet_balance):
    """Match BacktestEngine's qty calculator for fixed USDT amount.

    Replicates the logic in BacktestEngine._create_qty_calculator()
    with InstrumentInfo.round_qty() rounding up to qty_step.
    """
    if intent.price <= 0:
        return Decimal("0")
    raw_qty = Decimal(AMOUNT) / intent.price
    # Round up to nearest qty_step (matching InstrumentInfo.round_qty behavior)
    steps = math.ceil(float(raw_qty) / float(QTY_STEP))
    return Decimal(str(steps)) * QTY_STEP


def _run_path_a(events):
    """Path A: BacktestEngine (orchestrated, as users would run backtests)."""
    config = _make_backtest_config()
    engine = BacktestEngine(config=config)
    provider = InMemoryDataProvider(events)
    session = engine.run(
        symbol=SYMBOL,
        start_ts=events[0].exchange_ts,
        end_ts=events[-1].exchange_ts,
        data_provider=provider,
    )
    return session


def _run_path_b(events):
    """Path B: Manual GridEngine+OrderManager simulation (shadow-mode pattern).

    Constructs BacktestRunner independently and feeds the same price events
    through the two-phase process_fills() + execute_tick() loop, mimicking
    what a shadow-mode StrategyRunner would do in production.
    """
    strategy_config = _make_strategy_config()
    fill_simulator = TradeThroughFillSimulator()
    order_manager = BacktestOrderManager(
        fill_simulator=fill_simulator,
        commission_rate=strategy_config.commission_rate,
    )
    executor = BacktestExecutor(
        order_manager=order_manager,
        qty_calculator=_qty_calculator,
    )
    session = BacktestSession(initial_balance=INITIAL_BALANCE)
    long_tracker = BacktestPositionTracker(
        direction=DirectionType.LONG,
        commission_rate=strategy_config.commission_rate,
    )
    short_tracker = BacktestPositionTracker(
        direction=DirectionType.SHORT,
        commission_rate=strategy_config.commission_rate,
    )
    runner = BacktestRunner(
        strategy_config=strategy_config,
        executor=executor,
        session=session,
        long_tracker=long_tracker,
        short_tracker=short_tracker,
    )

    # Feed events through the two-phase processing loop
    for tick in events:
        # Phase 1: Check fills (updates realized PnL)
        runner.process_fills(tick)

        # Equity update between phases (same as BacktestEngine._process_tick)
        total_unrealized = (
            long_tracker.calculate_unrealized_pnl(tick.last_price)
            + short_tracker.calculate_unrealized_pnl(tick.last_price)
        )
        session.update_equity(tick.exchange_ts, total_unrealized)

        # Phase 2: Execute tick intents (uses updated balance)
        runner.execute_tick(tick)

    return session


class TestShadowModeValidation:
    """Validate shadow-mode pipeline: two independent paths produce identical trades."""

    def test_both_paths_produce_trades(self):
        """Both paths should produce trades with the oscillating price data."""
        events = _make_price_events()
        session_a = _run_path_a(events)
        session_b = _run_path_b(events)

        assert len(session_a.trades) > 0, "Path A must produce trades"
        assert len(session_b.trades) > 0, "Path B must produce trades"

    def test_trade_counts_match(self):
        """Both paths should produce the exact same number of trades."""
        events = _make_price_events()
        session_a = _run_path_a(events)
        session_b = _run_path_b(events)

        assert len(session_a.trades) == len(session_b.trades), (
            f"Path A: {len(session_a.trades)} trades, "
            f"Path B: {len(session_b.trades)} trades"
        )

    def test_client_order_ids_deterministic(self):
        """Same config + prices should produce identical client_order_ids."""
        events = _make_price_events()
        session_a = _run_path_a(events)
        session_b = _run_path_b(events)

        if len(session_a.trades) == 0:
            pytest.skip("No trades produced")

        ids_a = [t.client_order_id for t in session_a.trades]
        ids_b = [t.client_order_id for t in session_b.trades]
        assert ids_a == ids_b, "Client order IDs should match across paths"

    def test_dual_path_perfect_match(self):
        """Comparator should match 100% of trades between the two paths."""
        events = _make_price_events()
        session_a = _run_path_a(events)
        session_b = _run_path_b(events)

        if len(session_a.trades) == 0:
            pytest.skip("No trades produced")

        loader = BacktestTradeLoader()
        trades_a = loader.load_from_session(session_a.trades)
        trades_b = loader.load_from_session(session_b.trades)

        # Mark path B as "live" for matching
        trades_b_as_live = [replace(t, source="live") for t in trades_b]

        matcher = TradeMatcher()
        result = matcher.match(trades_b_as_live, trades_a)

        assert len(result.matched) == len(trades_a), (
            f"Expected {len(trades_a)} matched, got {len(result.matched)}"
        )
        assert len(result.live_only) == 0, (
            f"Expected 0 live-only, got {len(result.live_only)}"
        )
        assert len(result.backtest_only) == 0, (
            f"Expected 0 backtest-only, got {len(result.backtest_only)}"
        )

    def test_dual_path_zero_deltas(self):
        """Matched trades should have zero price/qty deltas and 100% match rate."""
        events = _make_price_events()
        session_a = _run_path_a(events)
        session_b = _run_path_b(events)

        if len(session_a.trades) == 0:
            pytest.skip("No trades produced")

        loader = BacktestTradeLoader()
        trades_a = loader.load_from_session(session_a.trades)
        trades_b = loader.load_from_session(session_b.trades)
        trades_b_as_live = [replace(t, source="live") for t in trades_b]

        matcher = TradeMatcher()
        result = matcher.match(trades_b_as_live, trades_a)
        metrics = calculate_metrics(result)

        assert metrics.match_rate == 1.0
        assert metrics.price_mean_abs_delta == 0.0
        assert metrics.qty_mean_abs_delta == 0.0
        assert metrics.cumulative_pnl_delta == 0

    def test_pnl_totals_match(self):
        """Both paths should produce identical realized PnL totals."""
        events = _make_price_events()
        session_a = _run_path_a(events)
        session_b = _run_path_b(events)

        if len(session_a.trades) == 0:
            pytest.skip("No trades produced")

        assert session_a.total_realized_pnl == session_b.total_realized_pnl, (
            f"PnL mismatch: Path A={session_a.total_realized_pnl}, "
            f"Path B={session_b.total_realized_pnl}"
        )
        assert session_a.total_commission == session_b.total_commission, (
            f"Commission mismatch: Path A={session_a.total_commission}, "
            f"Path B={session_b.total_commission}"
        )
