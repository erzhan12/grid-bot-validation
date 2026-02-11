"""Tests for backtest runner."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gridcore import TickerEvent, EventType

from backtest.runner import BacktestRunner
from backtest.config import BacktestStrategyConfig
from backtest.fill_simulator import TradeThroughFillSimulator
from backtest.order_manager import BacktestOrderManager
from backtest.executor import BacktestExecutor
from backtest.session import BacktestSession


class TestBacktestRunner:
    """Tests for BacktestRunner."""

    @pytest.fixture
    def runner(self, sample_strategy_config, session):
        """Create a backtest runner."""
        fill_simulator = TradeThroughFillSimulator()
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=sample_strategy_config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_manager)

        return BacktestRunner(
            strategy_config=sample_strategy_config,
            executor=executor,
            session=session,
        )

    def test_init(self, runner, sample_strategy_config):
        """Runner initializes correctly."""
        assert runner.strat_id == sample_strategy_config.strat_id
        assert runner.symbol == sample_strategy_config.symbol
        assert runner.engine is not None
        assert runner.long_tracker is not None
        assert runner.short_tracker is not None

    def test_process_tick_builds_grid(self, runner, sample_ticker_event):
        """First tick builds the grid."""
        intents = runner.process_tick(sample_ticker_event)

        # Should have generated place intents for grid
        assert len(intents) > 0
        assert runner._grid_built is True

    def test_process_tick_fills_order(self, runner, sample_timestamp):
        """Order fills when price crosses."""
        # First tick builds grid at 100000
        tick1 = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=Decimal("100000"),
            mark_price=Decimal("100000"),
            bid1_price=Decimal("99999"),
            ask1_price=Decimal("100001"),
            funding_rate=Decimal("0.0001"),
        )
        runner.process_tick(tick1)

        # Get order prices
        limit_orders = runner.order_manager.get_limit_orders()
        if limit_orders["long"]:
            buy_price = Decimal(limit_orders["long"][0]["price"])

            # Second tick drops below buy price
            tick2 = TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=sample_timestamp,
                local_ts=sample_timestamp,
                last_price=buy_price - Decimal("100"),
                mark_price=buy_price - Decimal("100"),
                bid1_price=buy_price - Decimal("101"),
                ask1_price=buy_price - Decimal("99"),
                funding_rate=Decimal("0.0001"),
            )
            runner.process_tick(tick2)

            # Should have recorded a trade
            assert len(runner._session.trades) >= 1

    def test_apply_funding(self, runner, sample_ticker_event):
        """Funding is applied to positions."""
        # Build grid first
        runner.process_tick(sample_ticker_event)

        # Manually set a position (normally would be from fills)
        runner._long_tracker.process_fill(
            side="Buy",
            qty=Decimal("0.1"),
            price=Decimal("100000"),
        )

        # Apply funding
        funding = runner.apply_funding(Decimal("0.0001"), Decimal("100000"))

        # Long pays when rate > 0
        # Position value = 0.1 * 100000 = 10000
        # Funding = 10000 * 0.0001 = 1
        assert funding == Decimal("-1")

    def test_get_total_pnl(self, runner, sample_ticker_event):
        """Total PnL from both trackers."""
        runner.process_tick(sample_ticker_event)

        # Simulate some PnL (set directly for testing)
        runner._long_tracker.state.realized_pnl = Decimal("100")
        runner._short_tracker.state.realized_pnl = Decimal("50")

        total = runner.get_total_pnl()

        # Total should include both directions
        # (When set directly, no commission is deducted)
        assert total == Decimal("150")
