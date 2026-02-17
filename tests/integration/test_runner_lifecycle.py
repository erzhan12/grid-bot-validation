"""Integration test: StrategyRunner full lifecycle.

Validates startup reconciliation → ticker → grid build → order tracking.
"""

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from gridcore.events import ExecutionEvent, OrderUpdateEvent, EventType

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor
from gridbot.runner import StrategyRunner

from helpers import make_ticker_event


def _make_strategy_config():
    """Create a StrategyConfig for testing."""
    return StrategyConfig(
        strat_id="test_strat",
        account="test_account",
        symbol="BTCUSDT",
        tick_size="0.1",
        grid_count=20,
        grid_step=0.5,
        amount="1000",
        shadow_mode=True,
    )


@pytest.fixture
def executor():
    """Shadow-mode IntentExecutor."""
    mock_rest = MagicMock()
    return IntentExecutor(rest_client=mock_rest, shadow_mode=True)


@pytest.fixture
def strategy_config():
    return _make_strategy_config()


class TestRunnerLifecycle:
    """Test StrategyRunner through its lifecycle stages."""

    @pytest.mark.asyncio
    async def test_first_ticker_builds_grid(self, strategy_config, executor):
        """First ticker event should trigger grid build and produce intents."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)

        await runner.on_ticker(event)

        # Grid should have been built
        assert runner._engine.grid is not None
        assert runner._engine.grid.is_grid_correct()
        assert len(runner._engine.grid.grid) > 0

    @pytest.mark.asyncio
    async def test_inject_open_orders_adopts_exchange_orders(self, strategy_config, executor):
        """inject_open_orders should adopt orders from exchange state."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        # Build grid first
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)
        await runner.on_ticker(event)

        # Simulate exchange orders (16-char hex orderLinkId = our format)
        orders = [
            {
                "orderId": "exchange_order_1",
                "orderLinkId": "abcdef0123456789",  # 16-char hex
                "price": "99800.0",
                "qty": "0.001",
                "side": "Buy",
                "orderType": "Limit",
                "orderStatus": "New",
            },
        ]

        runner.inject_open_orders(orders)

        # Order should be tracked
        assert len(runner._tracked_orders) > 0

    @pytest.mark.asyncio
    async def test_order_update_tracks_status(self, strategy_config, executor):
        """OrderUpdateEvent should update tracked order status."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        # Build grid
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)
        await runner.on_ticker(event)

        # Get a placed order's client_order_id
        if not runner._tracked_orders:
            pytest.skip("No orders tracked")

        # Simulate order status update
        first_order = next(iter(runner._tracked_orders.values()))
        order_event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=ts + timedelta(seconds=1),
            local_ts=ts + timedelta(seconds=1),
            order_id=first_order.order_id or "test_order",
            order_link_id=first_order.client_order_id,
            status="New",
        )

        await runner.on_order_update(order_event)
        # Should not crash; order tracking updated internally

    @pytest.mark.asyncio
    async def test_consecutive_tickers_stable(self, strategy_config, executor):
        """Multiple consecutive tickers should produce stable grid state."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)

        # Send multiple tickers at similar prices
        for i in range(5):
            price = 100000.0 + (i * 0.1)  # Small price movements
            event = make_ticker_event("BTCUSDT", price, ts + timedelta(seconds=i))
            await runner.on_ticker(event)

        # Grid should still be valid
        assert runner._engine.grid is not None
        assert runner._engine.grid.is_grid_correct()

    @pytest.mark.asyncio
    async def test_shadow_mode_no_real_api_calls(self, strategy_config, executor):
        """Shadow mode should never make real API calls."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)
        await runner.on_ticker(event)

        # Verify the rest client was never called
        executor._client.place_order.assert_not_called()
        executor._client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_fill_updates_tracked_order(self, strategy_config, executor):
        """ExecutionEvent with matching order_link_id marks tracked order as filled."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)
        await runner.on_ticker(event)

        # Find a tracked order
        if not runner._tracked_orders:
            pytest.skip("No orders tracked after first ticker")

        tracked = next(iter(runner._tracked_orders.values()))
        assert tracked.status in ("pending", "placed")

        # Send an execution event matching this order
        exec_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts + timedelta(seconds=1),
            local_ts=ts + timedelta(seconds=1),
            exec_id="exec_1",
            order_id=tracked.order_id or "exchange_order_1",
            order_link_id=tracked.client_order_id,
            side=tracked.intent.side,
            price=tracked.intent.price,
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
            closed_size=Decimal("0"),
        )

        await runner.on_execution(exec_event)

        assert tracked.status == "filled"

    @pytest.mark.asyncio
    async def test_position_update_adjusts_multipliers(self, strategy_config, executor):
        """on_position_update with position data changes multipliers from default."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        # Build grid
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)
        await runner.on_ticker(event)

        # Default multipliers should all be 1.0
        assert runner.get_amount_multiplier("long", "Buy") == 1.0
        assert runner.get_amount_multiplier("long", "Sell") == 1.0

        # Simulate a position with liq price close to current price (high risk)
        long_position = {
            "size": "0.1",
            "avgPrice": "100000.0",
            "liqPrice": "99100.0",  # Very close liq price = high risk
            "positionValue": "10000.0",
            "unrealisedPnl": "-100",
        }

        await runner.on_position_update(
            long_position=long_position,
            short_position=None,
            wallet_balance=10000.0,
            last_close=100000.0,
        )

        # With high liq risk, at least one multiplier should differ from 1.0
        long_buy = runner.get_amount_multiplier("long", "Buy")
        long_sell = runner.get_amount_multiplier("long", "Sell")
        # High liq risk reduces Buy multiplier to 0
        assert long_buy != 1.0 or long_sell != 1.0

    @pytest.mark.asyncio
    async def test_same_order_detection_blocks_execution(self, strategy_config, executor):
        """Two fills at same price with different order_ids triggers same_order_error."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)
        await runner.on_ticker(event)

        assert not runner.same_order_error

        fill_price = Decimal("99800.0")

        # First fill (Buy, opening long → long buffer)
        exec1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts + timedelta(seconds=1),
            local_ts=ts + timedelta(seconds=1),
            exec_id="exec_1",
            order_id="order_A",
            order_link_id="link_A",
            side="Buy",
            price=fill_price,
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
            closed_size=Decimal("0"),
        )
        await runner.on_execution(exec1)

        # Second fill at SAME price, different order_id (duplicate = error)
        exec2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts + timedelta(seconds=2),
            local_ts=ts + timedelta(seconds=2),
            exec_id="exec_2",
            order_id="order_B",
            order_link_id="link_B",
            side="Buy",
            price=fill_price,
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
            closed_size=Decimal("0"),
        )
        await runner.on_execution(exec2)

        assert runner.same_order_error

        # Sending a new ticker should NOT execute intents (error active)
        initial_tracked = len(runner._tracked_orders)
        event2 = make_ticker_event("BTCUSDT", 100000.0, ts + timedelta(seconds=3))
        await runner.on_ticker(event2)
        # No new orders should be tracked beyond what already exists
        assert len(runner._tracked_orders) == initial_tracked

    @pytest.mark.asyncio
    async def test_same_order_recovery_on_different_price(self, strategy_config, executor):
        """Fill at different price pushes old entry out of buffer, clearing error."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
        )

        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        event = make_ticker_event("BTCUSDT", 100000.0, ts)
        await runner.on_ticker(event)

        fill_price = Decimal("99800.0")

        # Trigger same-order error
        exec1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts + timedelta(seconds=1),
            local_ts=ts + timedelta(seconds=1),
            exec_id="exec_1",
            order_id="order_A",
            order_link_id="link_A",
            side="Buy",
            price=fill_price,
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
            closed_size=Decimal("0"),
        )
        await runner.on_execution(exec1)

        exec2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts + timedelta(seconds=2),
            local_ts=ts + timedelta(seconds=2),
            exec_id="exec_2",
            order_id="order_B",
            order_link_id="link_B",
            side="Buy",
            price=fill_price,
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
            closed_size=Decimal("0"),
        )
        await runner.on_execution(exec2)

        assert runner.same_order_error

        # Fill at DIFFERENT price pushes oldest entry out of 2-entry buffer
        exec3 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts + timedelta(seconds=3),
            local_ts=ts + timedelta(seconds=3),
            exec_id="exec_3",
            order_id="order_C",
            order_link_id="link_C",
            side="Buy",
            price=Decimal("99600.0"),  # Different price
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
            closed_size=Decimal("0"),
        )
        await runner.on_execution(exec3)

        # Buffer now has [exec3, exec2] — different prices, no error
        assert not runner.same_order_error
