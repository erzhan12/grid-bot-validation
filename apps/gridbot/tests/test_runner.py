"""Tests for gridbot strategy runner module."""

from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch

import pytest

from gridcore import TickerEvent, ExecutionEvent, OrderUpdateEvent, EventType
from gridcore.intents import PlaceLimitIntent, CancelIntent

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult, CancelResult
from gridbot.runner import StrategyRunner, TrackedOrder


@pytest.fixture
def strategy_config():
    """Sample strategy configuration."""
    return StrategyConfig(
        strat_id="btcusdt_test",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.2,
        shadow_mode=False,
    )


@pytest.fixture
def shadow_config():
    """Shadow mode strategy configuration."""
    return StrategyConfig(
        strat_id="btcusdt_shadow",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.2,
        shadow_mode=True,
    )


@pytest.fixture
def mock_executor():
    """Create mock executor."""
    executor = Mock(spec=IntentExecutor)
    executor.shadow_mode = False
    executor.execute_place = MagicMock(
        return_value=OrderResult(success=True, order_id="order_123")
    )
    executor.execute_cancel = MagicMock(return_value=CancelResult(success=True))
    return executor


@pytest.fixture
def runner(strategy_config, mock_executor):
    """Create strategy runner."""
    return StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
    )


@pytest.fixture
def ticker_event():
    """Sample ticker event."""
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        exchange_ts=datetime.now(UTC),
        local_ts=datetime.now(UTC),
        last_price=Decimal("50000.0"),
        mark_price=Decimal("50000.0"),
        bid1_price=Decimal("49999.0"),
        ask1_price=Decimal("50001.0"),
        funding_rate=Decimal("0.0001"),
    )


class TestTrackedOrder:
    """Tests for TrackedOrder dataclass."""

    def test_create_order(self):
        """Test creating tracked order."""
        order = TrackedOrder(client_order_id="test_123")
        assert order.client_order_id == "test_123"
        assert order.order_id is None
        assert order.status == "pending"

    def test_mark_placed(self):
        """Test marking order as placed."""
        order = TrackedOrder(client_order_id="test_123")
        order.mark_placed("exchange_order_456")

        assert order.order_id == "exchange_order_456"
        assert order.status == "placed"

    def test_mark_filled(self):
        """Test marking order as filled."""
        order = TrackedOrder(client_order_id="test_123")
        order.mark_filled()
        assert order.status == "filled"

    def test_mark_cancelled(self):
        """Test marking order as cancelled."""
        order = TrackedOrder(client_order_id="test_123")
        order.mark_cancelled()
        assert order.status == "cancelled"


class TestStrategyRunnerProperties:
    """Tests for StrategyRunner properties."""

    def test_strat_id(self, runner, strategy_config):
        """Test strat_id property."""
        assert runner.strat_id == strategy_config.strat_id

    def test_symbol(self, runner, strategy_config):
        """Test symbol property."""
        assert runner.symbol == strategy_config.symbol

    def test_shadow_mode(self, runner):
        """Test shadow_mode property."""
        assert runner.shadow_mode is False

    def test_shadow_mode_enabled(self, shadow_config, mock_executor):
        """Test shadow_mode property when enabled."""
        runner = StrategyRunner(
            strategy_config=shadow_config,
            executor=mock_executor,
        )
        assert runner.shadow_mode is True


class TestStrategyRunnerTicker:
    """Tests for ticker event processing."""

    @pytest.mark.asyncio
    async def test_on_ticker_builds_grid(self, runner, ticker_event):
        """Test ticker event builds grid on first call."""
        intents = await runner.on_ticker(ticker_event)

        # Grid should be built, intents generated
        assert len(runner.engine.grid.grid) > 0

    @pytest.mark.asyncio
    async def test_on_ticker_executes_intents(self, runner, mock_executor, ticker_event):
        """Test ticker event executes returned intents."""
        await runner.on_ticker(ticker_event)

        # Executor should have been called
        assert mock_executor.execute_place.called or mock_executor.execute_cancel.called


class TestStrategyRunnerOrderTracking:
    """Tests for order tracking."""

    def test_get_limit_orders_empty(self, runner):
        """Test getting limit orders when none tracked."""
        orders = runner.get_limit_orders()
        assert orders == {"long": [], "short": []}

    def test_inject_open_orders(self, runner):
        """Test injecting open orders from exchange."""
        orders = [
            {"orderLinkId": "order_1", "orderId": "exchange_1"},
            {"orderLinkId": "order_2", "orderId": "exchange_2"},
        ]
        runner.inject_open_orders(orders)

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 2

    def test_get_tracked_order_count(self, runner):
        """Test getting tracked order counts."""
        counts = runner.get_tracked_order_count()
        assert counts == {
            "pending": 0,
            "placed": 0,
            "filled": 0,
            "cancelled": 0,
            "failed": 0,
        }


class TestStrategyRunnerExecution:
    """Tests for order execution."""

    @pytest.mark.asyncio
    async def test_execute_place_intent_success(self, runner, mock_executor):
        """Test successful order placement."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        await runner._execute_place_intent(intent)

        mock_executor.execute_place.assert_called_once_with(intent)
        assert intent.client_order_id in runner._tracked_orders
        assert runner._tracked_orders[intent.client_order_id].status == "placed"

    @pytest.mark.asyncio
    async def test_execute_place_intent_failure(self, runner, mock_executor):
        """Test failed order placement."""
        mock_executor.execute_place.return_value = OrderResult(
            success=False, error="Rate limited"
        )

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        await runner._execute_place_intent(intent)

        assert runner._tracked_orders[intent.client_order_id].status == "failed"

    @pytest.mark.asyncio
    async def test_execute_place_intent_duplicate_skipped(self, runner, mock_executor):
        """Test duplicate order placement is skipped."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        # First execution
        await runner._execute_place_intent(intent)
        call_count = mock_executor.execute_place.call_count

        # Second execution (duplicate)
        await runner._execute_place_intent(intent)

        # Should not have been called again
        assert mock_executor.execute_place.call_count == call_count

    @pytest.mark.asyncio
    async def test_execute_cancel_intent_success(self, runner, mock_executor):
        """Test successful order cancellation."""
        intent = CancelIntent(
            symbol="BTCUSDT",
            order_id="order_to_cancel",
            reason="test",
        )

        await runner._execute_cancel_intent(intent)

        mock_executor.execute_cancel.assert_called_once_with(intent)


class TestStrategyRunnerPositionUpdate:
    """Tests for position updates."""

    @pytest.mark.asyncio
    async def test_on_position_update_calculates_ratio(self, runner):
        """Test position update calculates position ratio."""
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}
        short_pos = {"size": "0.5", "avgPrice": "50000", "liqPrice": "60000"}

        await runner.on_position_update(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        # Long ratio survives; short's is overwritten by calculate_amount_multiplier
        assert runner._long_position.position_ratio == 2.0  # 1.0 / 0.5

    @pytest.mark.asyncio
    async def test_on_position_update_no_short(self, runner):
        """Test position update with no short position."""
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}

        await runner.on_position_update(
            long_position=long_pos,
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        # Long's ratio is overwritten by calculate_amount_multiplier;
        # short's survives since calculate is not called (short_state is None)
        assert runner._short_position.position_ratio == float("inf")

    @pytest.mark.asyncio
    async def test_on_position_update_no_positions(self, runner):
        """Test position update with no positions."""
        await runner.on_position_update(
            long_position=None,
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        assert runner._long_position.position_ratio == 1.0
        assert runner._short_position.position_ratio == 1.0

    @pytest.mark.asyncio
    async def test_on_position_update_stores_both_multiplier_keys(self, runner):
        """Test that both Buy and Sell multipliers are stored per direction."""
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}
        short_pos = {"size": "0.5", "avgPrice": "50000", "liqPrice": "60000"}

        await runner.on_position_update(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        # Both dicts should have Buy and Sell keys
        long_mult = runner._long_position.get_amount_multiplier()
        short_mult = runner._short_position.get_amount_multiplier()
        assert "Buy" in long_mult
        assert "Sell" in long_mult
        assert "Buy" in short_mult
        assert "Sell" in short_mult

    @pytest.mark.asyncio
    async def test_on_position_update_no_positions_keeps_default_multipliers(self, runner):
        """Test multipliers stay at defaults when no positions exist."""
        await runner.on_position_update(
            long_position=None,
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        assert runner._long_position.get_amount_multiplier() == {"Buy": 1.0, "Sell": 1.0}
        assert runner._short_position.get_amount_multiplier() == {"Buy": 1.0, "Sell": 1.0}

    def test_get_amount_multiplier_long(self, runner):
        """Test get_amount_multiplier returns correct value for long direction."""
        runner._long_position.amount_multiplier = {"Buy": 2.0, "Sell": 1.5}

        assert runner.get_amount_multiplier("long", "Buy") == 2.0
        assert runner.get_amount_multiplier("long", "Sell") == 1.5

    def test_get_amount_multiplier_short(self, runner):
        """Test get_amount_multiplier returns correct value for short direction."""
        runner._short_position.amount_multiplier = {"Buy": 1.5, "Sell": 2.0}

        assert runner.get_amount_multiplier("short", "Buy") == 1.5
        assert runner.get_amount_multiplier("short", "Sell") == 2.0

    def test_get_amount_multiplier_raises_on_invalid_side(self, runner):
        """Test get_amount_multiplier raises KeyError for unknown side."""
        with pytest.raises(KeyError):
            runner.get_amount_multiplier("long", "Unknown")
        with pytest.raises(KeyError):
            runner.get_amount_multiplier("short", "Unknown")


class TestStrategyRunnerOrderUpdate:
    """Tests for order update events."""

    @pytest.mark.asyncio
    async def test_on_order_update_fills_tracked(self, runner, mock_executor):
        """Test order update marks tracked order as filled."""
        # First place an order
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        await runner._execute_place_intent(intent)

        # Simulate fill event
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id=intent.client_order_id,
            status="Filled",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
        )

        await runner.on_order_update(event)

        assert runner._tracked_orders[intent.client_order_id].status == "filled"

    @pytest.mark.asyncio
    async def test_on_order_update_cancels_tracked(self, runner, mock_executor):
        """Test order update marks tracked order as cancelled."""
        # First place an order
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        await runner._execute_place_intent(intent)

        # Simulate cancel event
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id=intent.client_order_id,
            status="Cancelled",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
        )

        await runner.on_order_update(event)

        assert runner._tracked_orders[intent.client_order_id].status == "cancelled"


class TestStrategyRunnerFailureCallback:
    """Tests for failure callback."""

    @pytest.mark.asyncio
    async def test_on_intent_failed_called(self, strategy_config, mock_executor):
        """Test failure callback is called on execution failure."""
        callback = Mock()
        mock_executor.execute_place.return_value = OrderResult(
            success=False, error="Network error"
        )

        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            on_intent_failed=callback,
        )

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        await runner._execute_place_intent(intent)

        callback.assert_called_once_with(intent, "Network error")


class TestSameOrderDetection:
    """Tests for same-order detection (bbu2-style safety check)."""

    def test_same_order_error_initial_state(self, runner):
        """Test same_order_error is False initially."""
        assert runner.same_order_error is False

    def test_no_error_with_single_execution(self, runner):
        """Test no error detected with single execution."""
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Opening position
        )

        runner._check_same_orders(event)

        assert runner.same_order_error is False

    def test_no_error_with_different_prices(self, runner):
        """Test no error when executions are at different prices."""
        # First execution
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Second execution at different price
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("49000.0"),  # Different price
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is False

    def test_no_error_with_same_order_id_partial_fills(self, runner):
        """Test no error when same order_id fills multiple times (partial fills)."""
        # First partial fill
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",  # Same order ID
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Second partial fill (same order ID, same price)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_1",  # Same order ID - partial fill OK
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is False

    def test_error_detected_with_different_order_ids_same_price(self, runner):
        """Test error detected when different orders fill at same price."""
        # First execution
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Second execution - DIFFERENT order_id but SAME price = ERROR
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",  # Different order ID
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),  # Same price = duplicate!
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True

    def test_no_error_different_sides(self, runner):
        """Test no error when same price but different sides."""
        # Buy execution
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Opening long
        )
        runner._check_same_orders(event1)

        # Sell execution - same price but different side (and direction)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Sell",
            price=Decimal("50000.0"),  # Same price
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Opening short - goes to short buffer
        )
        runner._check_same_orders(event2)

        # Different buffers (long vs short), so no error
        assert runner.same_order_error is False

    def test_reset_same_order_error(self, runner):
        """Test reset_same_order_error clears flag and buffers."""
        # Trigger error
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True
        assert len(runner._recent_executions_long) == 2

        # Reset
        runner.reset_same_order_error()

        assert runner.same_order_error is False
        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 0

    def test_buffer_max_length(self, runner):
        """Test execution buffer keeps only last 2 entries (matches bbu2 [:2])."""
        for i in range(4):
            event = ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol="BTCUSDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                exec_id=f"exec_{i}",
                order_id=f"order_{i}",
                order_link_id=f"link_{i}",
                side="Buy",
                price=Decimal(str(50000 + i * 100)),  # Different prices
                qty=Decimal("0.1"),
                fee=Decimal("0.5"),
                closed_pnl=Decimal("0"),
            )
            runner._check_same_orders(event)

        # Buffer should only keep 2 entries
        assert len(runner._recent_executions_long) == 2

    def test_direction_separation_long(self, runner):
        """Test executions go to correct buffer - long direction."""
        # Buy opening (long buffer)
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Not closing = opening long
        )
        runner._check_same_orders(event)

        assert len(runner._recent_executions_long) == 1
        assert len(runner._recent_executions_short) == 0

    def test_direction_separation_short(self, runner):
        """Test executions go to correct buffer - short direction."""
        # Sell opening (short buffer)
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Sell",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Not closing = opening short
        )
        runner._check_same_orders(event)

        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 1

    def test_closing_long_goes_to_long_buffer(self, runner):
        """Test Sell with closed_size (closing long) goes to long buffer."""
        # Sell closing long position
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Sell",
            price=Decimal("51000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("100.0"),
            closed_size=Decimal("0.1"),  # Non-zero = closing position
        )
        runner._check_same_orders(event)

        # Sell with closed_size != 0 = closing long = goes to long buffer
        assert len(runner._recent_executions_long) == 1
        assert len(runner._recent_executions_short) == 0

    def test_closing_short_goes_to_short_buffer(self, runner):
        """Test Buy with closed_size (closing short) goes to short buffer."""
        # Buy closing short position
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("100.0"),
            closed_size=Decimal("0.1"),  # Non-zero = closing position
        )
        runner._check_same_orders(event)

        # Buy with closed_size != 0 = closing short = goes to short buffer
        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 1

    def test_same_order_error_auto_clears_on_clean_execution(self, runner):
        """Test error auto-clears when a new fill at a different price arrives.

        Matches bbu2 behavior: only the 2 most recent fills per side are
        compared ([:2]). One clean fill at a different price pushes the
        older problematic entry out of the 2-entry buffer, clearing the error.
        """
        # Trigger error: two different orders at same price
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),  # Same price, different order = ERROR
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)
        assert runner.same_order_error is True

        # One clean fill at a different price clears the error
        # Buffer becomes [49000, 50000(order_2)] - different prices, no error
        event3 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_3",
            order_id="order_3",
            order_link_id="link_3",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event3)

        assert runner.same_order_error is False

    def test_opposite_side_fill_does_not_clear_error(self, runner):
        """Test that a fill on the opposite side does NOT clear a same-order error.

        Regression test: _check_same_orders must check BOTH buffers (like bbu2).
        If only the current side's buffer is checked, a clean long fill would
        reset the flag and silently clear a short-side error.
        """
        # Trigger error on the SHORT side (Sell + not closing = opening short)
        short_event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_s1",
            order_id="order_s1",
            order_link_id="short1",
            side="Sell",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(short_event1)

        short_event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_s2",
            order_id="order_s2",  # Different order ID
            order_link_id="short2",
            side="Sell",
            price=Decimal("50000.0"),  # Same price = ERROR
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(short_event2)
        assert runner.same_order_error is True

        # Now a clean LONG fill arrives (Buy + not closing = opening long)
        long_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_l1",
            order_id="order_l1",
            order_link_id="long1",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(long_event)

        # Error must still be active - the short-side error is not cleared
        assert runner.same_order_error is True

    def test_on_ticker_skips_intents_but_updates_engine_when_error(self, runner):
        """Test on_ticker still passes event to engine but skips intent execution.

        Engine must always see ticker events to keep last_close fresh,
        but no orders are placed while same-order error is active.
        """
        import asyncio

        # Force same-order error
        runner._same_order_error = True

        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal("50000.0"),
        )

        # Mock _execute_intents to verify it's not called
        execute_called = False

        async def mock_execute(intents):
            nonlocal execute_called
            execute_called = True

        runner._execute_intents = mock_execute

        asyncio.get_event_loop().run_until_complete(runner.on_ticker(ticker))

        # Intents must not have been executed
        assert execute_called is False
        # Error still active
        assert runner.same_order_error is True

    def test_on_execution_updates_grid_but_skips_intents_when_error(self, runner):
        """Test on_execution passes event to engine but skips intent execution when error."""
        import asyncio

        # First, trigger same-order error via two duplicate fills
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)
        assert runner.same_order_error is True

        # Now process another execution through on_execution
        # Engine will get the event (grid update) but intents should not execute
        event3 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_3",
            order_id="order_3",
            order_link_id="ghi789",
            side="Buy",
            price=Decimal("50000.0"),  # Same price keeps error active
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )

        # Mock _execute_intents to verify it's not called
        execute_called = False
        original_execute = runner._execute_intents

        async def mock_execute(intents):
            nonlocal execute_called
            execute_called = True
            await original_execute(intents)

        runner._execute_intents = mock_execute

        asyncio.get_event_loop().run_until_complete(runner.on_execution(event3))

        # Error should still be active (3 consecutive same-price fills)
        assert runner.same_order_error is True
        # _execute_intents should not have been called
        assert execute_called is False

    def test_same_order_error_sends_telegram_alert(self, runner):
        """Test Telegram notification is sent when same-order error detected."""
        mock_notifier = Mock()
        runner._notifier = mock_notifier

        # Trigger error
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True
        mock_notifier.alert.assert_called_once()
        call_args = mock_notifier.alert.call_args
        assert "SAME ORDER ERROR" in call_args[0][0]
        assert "50000.0" in call_args[0][0]

    def test_on_order_update_skips_intents_when_same_order_error(self, runner):
        """Test on_order_update does not execute intents when same-order error active."""
        import asyncio

        runner._same_order_error = True

        order_event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_1",
            order_link_id="abc123",
            status="Filled",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            leaves_qty=Decimal("0"),
        )

        execute_called = False
        original_execute = runner._execute_intents

        async def mock_execute(intents):
            nonlocal execute_called
            execute_called = True
            await original_execute(intents)

        runner._execute_intents = mock_execute

        asyncio.get_event_loop().run_until_complete(runner.on_order_update(order_event))

        assert runner.same_order_error is True
        assert execute_called is False

    def test_partial_fill_skipped_from_buffer(self, runner):
        """Test partial fills (leavesQty != 0) are not added to buffer.

        Matches bbu2 handle_execution filter: leavesQty == '0'.
        Only fully filled orders enter the same-order detection buffer.
        """
        # Partial fill (leavesQty > 0) should NOT enter buffer
        partial_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partially filled
        )
        runner._check_same_orders(partial_event)

        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 0

    def test_partial_fill_does_not_trigger_same_order_error(self, runner):
        """Test that partial fills don't cause false same-order detection.

        Two partial fills at the same price with different order IDs should
        NOT trigger same-order error because they are filtered out.
        """
        # First partial fill
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partial
        )
        runner._check_same_orders(event1)

        # Second partial fill - different order, same price
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partial
        )
        runner._check_same_orders(event2)

        # Neither entered the buffer, no error
        assert runner.same_order_error is False
        assert len(runner._recent_executions_long) == 0

    def test_fully_filled_enters_buffer_partial_does_not(self, runner):
        """Test mixed: fully filled enters buffer, partial does not."""
        # Fully filled (leavesQty == 0) enters buffer
        full_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0"),  # Fully filled
        )
        runner._check_same_orders(full_event)
        assert len(runner._recent_executions_long) == 1

        # Partial fill (leavesQty > 0) does NOT enter buffer
        partial_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partial - skipped
        )
        runner._check_same_orders(partial_event)

        # Buffer still has only 1 entry (the fully filled one)
        assert len(runner._recent_executions_long) == 1
        assert runner.same_order_error is False
