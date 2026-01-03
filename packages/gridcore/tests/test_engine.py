"""
Unit tests for GridEngine module.

Tests event-driven strategy engine to ensure correct intent generation.
"""

import pytest
from datetime import datetime, UTC
from decimal import Decimal
from gridcore.engine import GridEngine
from gridcore.config import GridConfig
from gridcore.events import TickerEvent, ExecutionEvent, OrderUpdateEvent, EventType
from gridcore.intents import PlaceLimitIntent, CancelIntent


class TestGridEngineBasic:
    """Basic engine functionality tests."""

    def test_engine_initialization(self):
        """Engine initializes with correct state."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        assert engine.symbol == 'BTCUSDT'
        assert engine.tick_size == Decimal('0.1')
        assert engine.config.grid_count == 50
        assert engine.last_close is None
        assert engine.last_filled_price is None
        assert len(engine.grid.grid) == 0

    def test_on_ticker_event_builds_grid(self):
        """First ticker event initializes grid."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )

        intents = engine.on_event(event, {'long': [], 'short': []})

        # Grid should be built
        assert len(engine.grid.grid) == 51
        assert engine.last_close == 100000.0

    def test_on_ticker_event_returns_place_intents(self):
        """Ticker event returns PlaceLimitIntent for grid levels."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )

        intents = engine.on_event(event, {'long': [], 'short': []})

        # Should have intents for grid levels (excluding WAIT levels)
        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        assert len(place_intents) > 0

        # Check intent structure
        for intent in place_intents:
            assert intent.symbol == 'BTCUSDT'
            assert intent.side in ['Buy', 'Sell']
            assert intent.client_order_id is not None
            assert intent.direction in ['long', 'short']

    def test_on_execution_event_updates_last_filled(self):
        """ExecutionEvent updates last_filled_price."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        # Build grid first
        ticker_event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )
        engine.on_event(ticker_event, {'long': [], 'short': []})

        # Send execution event
        exec_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id='exec123',
            order_id='order123',
            order_link_id='client123',
            side='Buy',
            price=Decimal('99800.0'),
            qty=Decimal('0.001'),
            fee=Decimal('0.05'),
            closed_pnl=Decimal('0')
        )

        intents = engine.on_event(exec_event)

        assert engine.last_filled_price == 99800.0
        # Execution events typically don't generate intents
        assert len(intents) == 0


class TestGridEngineOrderPlacement:
    """Order placement intent generation tests."""

    def test_place_order_eligibility_buy(self):
        """Verify Buy orders only placed below market."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )

        intents = engine.on_event(event, {'long': [], 'short': []})

        # Check all Buy intents are below market price
        buy_intents = [i for i in intents if isinstance(i, PlaceLimitIntent) and i.side == 'Buy']
        for intent in buy_intents:
            assert float(intent.price) < 100000.0

    def test_place_order_eligibility_sell(self):
        """Verify Sell orders only placed above market."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )

        intents = engine.on_event(event, {'long': [], 'short': []})

        # Check all Sell intents are above market price
        sell_intents = [i for i in intents if isinstance(i, PlaceLimitIntent) and i.side == 'Sell']
        for intent in sell_intents:
            assert float(intent.price) > 100000.0

    def test_place_order_min_distance_check(self):
        """Orders too close to market price (< grid_step/2) are not placed."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )

        intents = engine.on_event(event, {'long': [], 'short': []})

        # All intents should be at least grid_step/2 away from market
        # 0.2% / 2 = 0.1% minimum
        min_distance_pct = 0.1
        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]

        for intent in place_intents:
            distance_pct = abs(float(intent.price) - 100000.0) / 100000.0 * 100
            assert distance_pct >= min_distance_pct * 0.99  # Allow tiny floating point error


class TestGridEngineCancellation:
    """Order cancellation intent generation tests."""

    def test_cancel_intent_side_mismatch(self):
        """Wrong side order gets CancelIntent."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        # Build grid
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )
        engine.on_event(event, {'long': [], 'short': []})

        # Create a limit order with wrong side (Sell at buy price)
        # Find a buy level price
        buy_level = next(g for g in engine.grid.grid if g['side'] == 'Buy')
        wrong_side_order = {
            'orderId': 'order123',
            'price': str(buy_level['price']),
            'side': 'Sell',  # Wrong! Should be Buy
            'qty': '0.001'
        }

        # Process ticker event with this wrong order
        intents = engine.on_event(event, {'long': [wrong_side_order], 'short': []})

        # Should have cancel intent for side mismatch
        cancel_intents = [i for i in intents if isinstance(i, CancelIntent) and i.reason == 'side_mismatch']
        assert len(cancel_intents) >= 1
        assert any(i.order_id == 'order123' for i in cancel_intents)

    def test_cancel_intent_outside_grid(self):
        """Price outside grid range gets CancelIntent."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        # Build grid
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )
        engine.on_event(event, {'long': [], 'short': []})

        # Create order way outside grid
        outside_order = {
            'orderId': 'outside123',
            'price': '150000.0',  # Way above grid
            'side': 'Sell',
            'qty': '0.001'
        }

        intents = engine.on_event(event, {'long': [outside_order], 'short': []})

        # Should have cancel intent for outside grid
        cancel_intents = [i for i in intents if isinstance(i, CancelIntent) and i.reason == 'outside_grid']
        assert len(cancel_intents) >= 1
        assert any(i.order_id == 'outside123' for i in cancel_intents)

    def test_too_many_orders_triggers_rebuild(self):
        """More than grid_count + 10 orders triggers rebuild (mass cancel and grid rebuild)."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        # Build grid first
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )
        engine.on_event(event, {'long': [], 'short': []})

        # Store original grid state
        original_grid_size = len(engine.grid.grid)
        original_center_price = next((g['price'] for g in engine.grid.grid if g['side'] == engine.grid.WAIT), None)

        # Create too many orders (> 60)
        too_many_orders = [
            {
                'orderId': f'order{i}',
                'price': str(100000.0 + i * 10),
                'side': 'Sell',
                'qty': '0.001'
            }
            for i in range(70)
        ]

        # Trigger rebuild with new price
        new_price_event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('105000.0'),  # New price
            mark_price=Decimal('105000.0'),
            bid1_price=Decimal('104999.0'),
            ask1_price=Decimal('105001.0'),
            funding_rate=Decimal('0.0001')
        )
        intents = engine.on_event(new_price_event, {'long': too_many_orders, 'short': []})

        # Should get rebuild cancel intents
        cancel_intents = [i for i in intents if isinstance(i, CancelIntent) and i.reason == 'rebuild']
        assert len(cancel_intents) == 70

        # Grid should be rebuilt (centered on new price)
        new_center_price = next((g['price'] for g in engine.grid.grid if g['side'] == engine.grid.WAIT), None)
        assert new_center_price is not None
        # Center should be near the new price (within a few steps)
        assert abs(new_center_price - 105000.0) / 105000.0 < 0.01
        # Grid size should remain the same
        assert len(engine.grid.grid) == original_grid_size


class TestGridEngineOrderTracking:
    """Order tracking tests."""

    def test_order_update_event_tracks_orders(self):
        """OrderUpdateEvent tracks pending orders."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        # New order
        order_event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id='order123',
            order_link_id='client123',
            status='New',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            leaves_qty=Decimal('0.001')
        )

        engine.on_event(order_event)
        assert 'client123' in engine.pending_orders
        assert engine.pending_orders['client123'] == 'order123'

        # Filled order
        filled_event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id='order123',
            order_link_id='client123',
            status='Filled',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            leaves_qty=Decimal('0')
        )

        engine.on_event(filled_event)
        assert 'client123' not in engine.pending_orders


class TestGridEngineEdgeCases:
    """Edge case tests."""

    def test_engine_handles_no_limit_orders(self):
        """Engine works when no existing limit orders."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )

        # No limit orders
        intents = engine.on_event(event, {'long': [], 'short': []})

        # Should still generate place intents
        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        assert len(place_intents) > 0

    def test_engine_handles_empty_grid(self):
        """Engine builds grid on first ticker even when starting empty."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

        assert len(engine.grid.grid) == 0

        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0'),
            mark_price=Decimal('100000.0'),
            bid1_price=Decimal('99999.0'),
            ask1_price=Decimal('100001.0'),
            funding_rate=Decimal('0.0001')
        )

        engine.on_event(event, {'long': [], 'short': []})

        assert len(engine.grid.grid) == 51


class TestEventModelValidation:
    """Test event model validation and constraints."""

    def test_ticker_event_validates_event_type(self):
        """TickerEvent enforces event_type=TICKER."""
        # Valid event
        valid_event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('100000.0')
        )
        assert valid_event.event_type == EventType.TICKER

        # Invalid event type should raise ValueError
        with pytest.raises(ValueError, match="TickerEvent must have event_type=TICKER"):
            TickerEvent(
                event_type=EventType.EXECUTION,  # Wrong type
                symbol='BTCUSDT',
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                last_price=Decimal('100000.0')
            )

    def test_execution_event_validates_event_type(self):
        """ExecutionEvent enforces event_type=EXECUTION."""
        # Valid event
        valid_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id='exec123',
            order_id='order123',
            order_link_id='client123',
            side='Buy',
            price=Decimal('100000.0'),
            qty=Decimal('0.001')
        )
        assert valid_event.event_type == EventType.EXECUTION

        # Invalid event type should raise ValueError
        with pytest.raises(ValueError, match="ExecutionEvent must have event_type=EXECUTION"):
            ExecutionEvent(
                event_type=EventType.TICKER,  # Wrong type
                symbol='BTCUSDT',
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                exec_id='exec123',
                order_id='order123',
                order_link_id='client123',
                side='Buy',
                price=Decimal('100000.0'),
                qty=Decimal('0.001')
            )

    def test_order_update_event_validates_event_type(self):
        """OrderUpdateEvent enforces event_type=ORDER_UPDATE."""
        # Valid event
        valid_event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id='order123',
            order_link_id='client123',
            status='New',
            side='Buy',
            price=Decimal('100000.0'),
            qty=Decimal('0.001')
        )
        assert valid_event.event_type == EventType.ORDER_UPDATE

        # Invalid event type should raise ValueError
        with pytest.raises(ValueError, match="OrderUpdateEvent must have event_type=ORDER_UPDATE"):
            OrderUpdateEvent(
                event_type=EventType.TICKER,  # Wrong type
                symbol='BTCUSDT',
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                order_id='order123',
                order_link_id='client123',
                status='New',
                side='Buy',
                price=Decimal('100000.0'),
                qty=Decimal('0.001')
            )


class TestDeterministicClientOrderId:
    """Tests for deterministic client_order_id generation."""

    def test_same_inputs_produce_same_id(self):
        """Same order parameters produce identical client_order_id."""
        intent1 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            grid_level=10,
            direction='long',
        )

        intent2 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            grid_level=10,
            direction='long',
        )

        assert intent1.client_order_id == intent2.client_order_id

    def test_different_inputs_produce_different_id(self):
        """Different order parameters produce different client_order_id."""
        intent1 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            grid_level=10,
            direction='long',
        )

        intent2 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Sell',  # Different side
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            grid_level=10,
            direction='long',
        )

        assert intent1.client_order_id != intent2.client_order_id

    def test_client_order_id_is_16_chars(self):
        """Client order ID should be 16 characters (hex digest truncated)."""
        intent = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            grid_level=10,
            direction='long',
        )

        assert len(intent.client_order_id) == 16
        # Should be valid hex
        int(intent.client_order_id, 16)

    def test_qty_does_not_affect_id(self):
        """Quantity is not part of the idempotency key (execution layer sets it)."""
        intent1 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.001'),
            grid_level=10,
            direction='long',
        )

        intent2 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99000.0'),
            qty=Decimal('0.002'),  # Different qty
            grid_level=10,
            direction='long',
        )

        # IDs should be the same - qty doesn't affect idempotency
        assert intent1.client_order_id == intent2.client_order_id
