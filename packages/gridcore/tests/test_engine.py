"""
Unit tests for GridEngine module.

Tests event-driven strategy engine to ensure correct intent generation.
"""

import pytest
from datetime import datetime, UTC
from decimal import Decimal
from gridcore.engine import GridEngine
from gridcore.config import GridConfig
from gridcore.events import TickerEvent, ExecutionEvent, OrderUpdateEvent, PublicTradeEvent, EventType
from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridcore.grid import GridSideType


class TestGridEngineBasic:
    """Basic engine functionality tests."""

    def test_engine_initialization(self):
        """Engine initializes with correct state."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

        assert engine.symbol == 'BTCUSDT'
        assert engine.tick_size == Decimal('0.1')
        assert engine.config.grid_count == 50
        assert engine.strat_id == 'btcusdt_test'
        assert engine.last_close is None
        assert engine.last_filled_price is None
        assert len(engine.grid.grid) == 0

    def test_on_ticker_event_builds_grid(self):
        """First ticker event initializes grid."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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

        # Grid should be built
        assert len(engine.grid.grid) == 51
        assert engine.last_close == 100000.0

    def test_on_ticker_event_returns_place_intents(self):
        """Ticker event returns PlaceLimitIntent for grid levels."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        wrong_price = Decimal(str(buy_level['price']))
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

        # Check that the order was replaced with a Buy order at the SAME price
        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        replacement_intent = next(
            (i for i in place_intents if i.side == 'Buy' and i.price == wrong_price),
            None
        )
        assert replacement_intent is not None, \
            f"Expected Buy order at {wrong_price} to replace cancelled Sell"

    def test_cancel_intent_outside_grid(self):
        """Price outside grid range gets CancelIntent."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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

        intents = engine.on_event(event, {'long': [], 'short': [outside_order]})

        # Should have cancel intent for outside grid
        cancel_intents = [i for i in intents if isinstance(i, CancelIntent) and i.reason == 'outside_grid']
        assert len(cancel_intents) >= 1
        assert any(i.order_id == 'outside123' for i in cancel_intents)

    def test_too_many_orders_triggers_rebuild(self):
        """More than grid_count + 10 orders triggers rebuild (mass cancel and grid rebuild)."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        # original_center_price = next((g['price'] for g in engine.grid.grid if g['side'] == GridSideType.WAIT), None)

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
        new_center_price = next((g['price'] for g in engine.grid.grid if g['side'] == GridSideType.WAIT), None)
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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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

    def test_grid_level_does_not_affect_id(self):
        """Grid level is not part of the idempotency key (allows orders to survive rebalancing)."""
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
            grid_level=15,  # Different grid_level
            direction='long',
        )

        # IDs should be the same - grid_level doesn't affect idempotency
        # This allows orders to survive center_grid() rebalancing
        assert intent1.client_order_id == intent2.client_order_id

        # But grid_level field is still preserved for tracking
        assert intent1.grid_level == 10
        assert intent2.grid_level == 15


class TestAnchorPricePersistence:
    """Tests for anchor price persistence functionality."""

    def test_engine_initialization_with_anchor_price(self):
        """Engine initializes with anchor price parameter."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test',
            anchor_price=100000.0
        )

        assert engine._anchor_price == 100000.0
        assert engine.strat_id == 'btcusdt_test'

    def test_engine_builds_grid_from_anchor_price(self):
        """Grid is built from anchor price instead of market price when provided."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test',
            anchor_price=100000.0  # Build around 100k
        )

        # Send ticker event with different market price
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('105000.0'),  # Market at 105k
            mark_price=Decimal('105000.0'),
            bid1_price=Decimal('104999.0'),
            ask1_price=Decimal('105001.0'),
            funding_rate=Decimal('0.0001')
        )

        engine.on_event(event, {'long': [], 'short': []})

        # Grid should be built around anchor price (100k), not market price (105k)
        anchor = engine.get_anchor_price()
        assert anchor == 100000.0  # Should be exactly the anchor price

    def test_engine_builds_grid_from_market_when_no_anchor(self):
        """Grid is built from market price when no anchor provided."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test'
            # No anchor_price
        )

        # Send ticker event
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('105000.0'),
            mark_price=Decimal('105000.0'),
            bid1_price=Decimal('104999.0'),
            ask1_price=Decimal('105001.0'),
            funding_rate=Decimal('0.0001')
        )

        engine.on_event(event, {'long': [], 'short': []})

        # Grid should be built around market price
        anchor = engine.get_anchor_price()
        assert anchor == 105000.0

    def test_get_anchor_price_returns_wait_zone_price(self):
        """get_anchor_price returns the WAIT zone center price."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test'
        )

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

        # Find the actual WAIT zone price(s) from the grid
        wait_prices = [g['price'] for g in engine.grid.grid if g['side'] == GridSideType.WAIT]
        assert len(wait_prices) >= 1, "Grid should have at least one WAIT zone"

        # get_anchor_price should return the original center WAIT price
        anchor = engine.get_anchor_price()
        assert anchor is not None
        # Anchor should match the original WAIT zone (middle of WAIT zones if multiple)
        assert anchor == 100000.0
        assert anchor in wait_prices, f"Anchor {anchor} should be in WAIT zones {wait_prices}"

    def test_get_anchor_price_returns_none_when_grid_empty(self):
        """get_anchor_price returns None when grid is empty."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test'
        )

        # Don't build grid
        anchor = engine.get_anchor_price()
        assert anchor is None

    def test_anchor_price_preserves_grid_levels_on_restart(self):
        """Simulates restart scenario: anchor price preserves grid levels."""
        config = GridConfig(grid_count=50, grid_step=0.2)

        # First run: build grid at 100k
        engine1 = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test'
        )

        event1 = TickerEvent(
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
        engine1.on_event(event1, {'long': [], 'short': []})

        # Save anchor price and grid structure (simulate persistence)
        saved_anchor = engine1.get_anchor_price()
        original_grid = [(g['price'], g['side']) for g in engine1.grid.grid]
        assert len(original_grid) > 0, "Grid should be built"

        # Second run: "restart" with market at different price
        engine2 = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test',
            anchor_price=saved_anchor  # Use saved anchor
        )

        event2 = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('105000.0'),  # Market moved to 105k
            mark_price=Decimal('105000.0'),
            bid1_price=Decimal('104999.0'),
            ask1_price=Decimal('105001.0'),
            funding_rate=Decimal('0.0001')
        )
        engine2.on_event(event2, {'long': [], 'short': []})

        # Grid should be centered at saved anchor (100k), not market price (105k)
        assert engine2.get_anchor_price() == saved_anchor
        assert engine2.get_anchor_price() == 100000.0

        # Verify actual grid levels are preserved (same prices and sides)
        restarted_grid = [(g['price'], g['side']) for g in engine2.grid.grid]
        assert len(restarted_grid) == len(original_grid), \
            f"Grid size mismatch: {len(restarted_grid)} != {len(original_grid)}"
        assert restarted_grid == original_grid, \
            "Grid levels should be identical after restart with anchor_price"

    def test_anchor_price_returns_original_center_after_fills(self):
        """
        Test that anchor_price returns the original center, not based on current WAIT zones.

        After order fills, update_grid() marks the filled level as WAIT (too close to
        place new orders). The original center should become BUY/SELL based on the new
        price, but anchor_price should still return the original center price that was
        set when the grid was first built.
        """
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test'
        )

        # Build grid at 100k
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

        # Verify anchor is 100k and original center is WAIT
        original_anchor = engine.get_anchor_price()
        assert original_anchor == 100000.0
        original_center = next(g for g in engine.grid.grid if g['price'] == 100000.0)
        assert original_center['side'] == GridSideType.WAIT

        # Simulate fill at lower price (99800)
        execution_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            price=Decimal('99800.0'),
            qty=Decimal('0.01'),
            side='Buy'
        )
        engine.on_event(execution_event)

        # Update grid with price near the fill (realistic scenario)
        # Price moved to 99850 after the fill at 99800
        # Note: update_grid() is only called when there are some existing limit orders
        ticker_after_fill = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal('99850.0'),  # Price near fill, not at original center
            mark_price=Decimal('99850.0'),
            bid1_price=Decimal('99849.0'),
            ask1_price=Decimal('99851.0'),
            funding_rate=Decimal('0.0001')
        )
        # Simulate some existing limit orders (required for update_grid to be called)
        existing_limits = {
            'long': [
                {'orderId': '1', 'price': '99600.0', 'side': 'Buy'},
                {'orderId': '2', 'price': '99400.0', 'side': 'Buy'},
            ],
            'short': []
        }
        engine.on_event(ticker_after_fill, existing_limits)

        # Verify anchor_price still returns original center (100k), not filled price (99800).
        # The drift between last_close (99850) and original anchor (100000) is below
        # one grid_step, so recenter does not walk and anchor stays put.
        anchor_after_fill = engine.get_anchor_price()
        assert anchor_after_fill == 100000.0, \
            f"Expected anchor_price to remain 100000.0 after fill, got {anchor_after_fill}"

        # Filled level is marked WAIT by update_grid post-fill.
        filled_level = next(g for g in engine.grid.grid if g['price'] == 99800.0)
        assert filled_level['side'] == GridSideType.WAIT, \
            f"Filled level should be WAIT, got {filled_level['side']}"
        # Note: pre-0022 the per-tick update_grid call in _check_and_place would
        # have reassigned the original center 100000.0 from WAIT to SELL on the
        # ticker after the fill. Post-0022 below-threshold ticks are true no-ops
        # (Step 2), so 100000.0 retains its build-time WAIT status until a walk
        # rewrites the grid. This is by design — drift is measured against the
        # persisted anchor, and side reassignment only happens on walks.


class TestRestoredGrid:
    """Tests for restoring grid state on engine construction."""

    def _ticker(self, price: float) -> TickerEvent:
        return TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal(str(price)),
            mark_price=Decimal(str(price)),
            bid1_price=Decimal(str(price - 1)),
            ask1_price=Decimal(str(price + 1)),
            funding_rate=Decimal('0.0001'),
        )

    def test_restored_grid_skips_build_on_first_ticker(self):
        """Engine constructed with restored_grid does not call build_grid on
        the first ticker (the saved grid is preserved verbatim)."""
        config = GridConfig(grid_count=10, grid_step=0.2)
        restored = [
            {'side': 'Buy', 'price': 99000.0},
            {'side': 'Buy', 'price': 99500.0},
            {'side': 'Wait', 'price': 100000.0},
            {'side': 'Sell', 'price': 100500.0},
            {'side': 'Sell', 'price': 101000.0},
        ]
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test',
            restored_grid=restored,
        )

        # First ticker within the restored grid range — must not rebuild.
        engine.on_event(self._ticker(100100.0), {'long': [], 'short': []})

        prices = [g['price'] for g in engine.grid.grid]
        assert prices == [99000.0, 99500.0, 100000.0, 100500.0, 101000.0]

    def test_restored_grid_with_invalid_pattern_falls_back_to_fresh_build(self, caplog):
        """Restored grid that fails validation leaves the engine empty so the
        first ticker triggers a fresh build_grid at market price. The engine
        must log a warning that includes strat_id and symbol so operators can
        identify which strategy hit the bad-state path."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        bad = [
            {'side': 'Sell', 'price': 99.0},  # SELL before BUY — invalid
            {'side': 'Buy', 'price': 99.5},
        ]
        with caplog.at_level('WARNING'):
            engine = GridEngine(
                symbol='BTCUSDT',
                tick_size=Decimal('0.1'),
                config=config,
                strat_id='btcusdt_test',
                restored_grid=bad,
            )
        assert engine.grid.grid == []

        # Warning identifies strat_id AND symbol — the kernel of the original
        # review feedback was that failures were hard to attribute under load.
        warnings = [r.message for r in caplog.records if r.levelname == 'WARNING']
        assert any(
            'btcusdt_test' in m and 'BTCUSDT' in m and 'Restored grid failed' in m
            for m in warnings
        ), f"Expected contextual restore-failure warning, got: {warnings}"

        engine.on_event(self._ticker(100000.0), {'long': [], 'short': []})

        # Fresh grid built at market price.
        assert engine.grid.anchor_price == 100000.0

    def test_restored_grid_success_does_not_warn(self, caplog):
        """Successful restoration must NOT emit the failure warning — would
        be alert-spam noise."""
        config = GridConfig(grid_count=10, grid_step=0.2)
        good = [
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Sell', 'price': 101.0},
        ]
        with caplog.at_level('WARNING'):
            GridEngine(
                symbol='BTCUSDT',
                tick_size=Decimal('0.1'),
                config=config,
                strat_id='btcusdt_test',
                restored_grid=good,
            )
        warnings = [r.message for r in caplog.records if r.levelname == 'WARNING']
        assert not any('Restored grid failed' in m for m in warnings)

    def test_drift_guard_rebuilds_when_price_outside_grid(self, caplog):
        """If last_close is outside [min_grid, max_grid] for the restored grid,
        the drift guard rebuilds at the current price on the first ticker."""
        config = GridConfig(grid_count=10, grid_step=0.2)
        restored = [
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Sell', 'price': 101.0},
        ]
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test',
            restored_grid=restored,
        )

        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(150.0), {'long': [], 'short': []})

        assert any('Restored grid out of range' in r.message for r in caplog.records)
        # Rebuild centered at the current price.
        assert engine.grid.anchor_price == 150.0

    def test_drift_guard_does_not_fire_when_price_inside_grid(self):
        config = GridConfig(grid_count=10, grid_step=0.2)
        restored = [
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Buy', 'price': 99.5},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Sell', 'price': 100.5},
            {'side': 'Sell', 'price': 101.0},
        ]
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test',
            restored_grid=restored,
        )
        # Price within grid_step of WAIT center → neither bounds-based drift
        # guard NOR feature-0022 recenter fires.
        engine.on_event(self._ticker(100.1), {'long': [], 'short': []})

        prices = [g['price'] for g in engine.grid.grid]
        assert prices == [99.0, 99.5, 100.0, 100.5, 101.0]

    def test_on_grid_change_callback_fires_on_build(self):
        """on_grid_change is invoked when the engine builds a fresh grid."""
        captured = []
        config = GridConfig(grid_count=10, grid_step=0.2)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.1'),
            config=config,
            strat_id='btcusdt_test',
            on_grid_change=lambda g: captured.append(len(g)),
        )
        engine.on_event(self._ticker(100.0), {'long': [], 'short': []})
        assert captured == [11]


class TestGridConfigValidation:
    """Tests for GridConfig validation in __post_init__."""

    def test_grid_count_must_be_positive(self):
        """grid_count <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="grid_count must be positive"):
            GridConfig(grid_count=0, grid_step=0.2)

        with pytest.raises(ValueError, match="grid_count must be positive"):
            GridConfig(grid_count=-1, grid_step=0.2)

    def test_grid_step_must_be_positive(self):
        """grid_step <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="grid_step must be positive"):
            GridConfig(grid_count=50, grid_step=0)

        with pytest.raises(ValueError, match="grid_step must be positive"):
            GridConfig(grid_count=50, grid_step=-0.1)

    def test_rebalance_threshold_must_be_between_0_and_1(self):
        """rebalance_threshold outside (0, 1) raises ValueError."""
        with pytest.raises(ValueError, match="rebalance_threshold must be between 0 and 1"):
            GridConfig(grid_count=50, grid_step=0.2, rebalance_threshold=0)

        with pytest.raises(ValueError, match="rebalance_threshold must be between 0 and 1"):
            GridConfig(grid_count=50, grid_step=0.2, rebalance_threshold=1)

        with pytest.raises(ValueError, match="rebalance_threshold must be between 0 and 1"):
            GridConfig(grid_count=50, grid_step=0.2, rebalance_threshold=1.5)

        with pytest.raises(ValueError, match="rebalance_threshold must be between 0 and 1"):
            GridConfig(grid_count=50, grid_step=0.2, rebalance_threshold=-0.1)

    def test_valid_config_succeeds(self):
        """Valid config values pass validation."""
        config = GridConfig(grid_count=50, grid_step=0.2, rebalance_threshold=0.3)
        assert config.grid_count == 50
        assert config.grid_step == 0.2
        assert config.rebalance_threshold == 0.3


class TestPublicTradeEventValidation:
    """Tests for PublicTradeEvent validation."""

    def test_public_trade_event_validates_event_type(self):
        """PublicTradeEvent enforces event_type=PUBLIC_TRADE."""
        # Valid event
        valid_event = PublicTradeEvent(
            event_type=EventType.PUBLIC_TRADE,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            trade_id='trade123',
            side='Buy',
            price=Decimal('100000.0'),
            size=Decimal('0.001')
        )
        assert valid_event.event_type == EventType.PUBLIC_TRADE

        # Invalid event type should raise ValueError
        with pytest.raises(ValueError, match="PublicTradeEvent must have event_type=PUBLIC_TRADE"):
            PublicTradeEvent(
                event_type=EventType.TICKER,  # Wrong type
                symbol='BTCUSDT',
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                trade_id='trade123',
                side='Buy',
                price=Decimal('100000.0'),
                size=Decimal('0.001')
            )


class TestEngineEdgeCasesAdvanced:
    """Advanced edge case tests for engine coverage."""

    def test_get_wait_indices_fallback_no_wait_items(self):
        """Test fallback when no WAIT indices exist (line 232)."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

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

        # Remove all WAIT items to trigger fallback
        engine.grid.grid = [g for g in engine.grid.grid if g['side'] != GridSideType.WAIT]

        # Call _get_wait_indices - should use fallback
        center_index = engine._get_wait_indices()
        expected_center = len(engine.grid.grid) // 2
        assert center_index == expected_center

    def test_get_wait_indices_empty_grid(self):
        """Test fallback when grid is empty (line 232)."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

        # Grid is empty initially
        center_index = engine._get_wait_indices()
        assert center_index == 0

    def test_create_place_intent_returns_none_for_wait(self):
        """_create_place_intent returns None for WAIT side (line 317)."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')
        engine.last_close = 100000.0

        wait_grid = {'side': 'Wait', 'price': 100000.0}
        result = engine._create_place_intent(wait_grid, 'long', 25)
        assert result is None

    def test_create_place_intent_returns_none_when_no_last_close(self):
        """_create_place_intent returns None when last_close is None (line 320)."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

        # last_close is None by default
        assert engine.last_close is None

        buy_grid = {'side': 'Buy', 'price': 99000.0}
        result = engine._create_place_intent(buy_grid, 'long', 10)
        assert result is None

    def test_create_place_intent_returns_none_when_too_close(self):
        """_create_place_intent returns None when price too close to market (line 332)."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')
        engine.last_close = 100000.0

        # Grid step is 0.2%, so grid_step/2 = 0.1%
        # Price at 99950 is 0.05% below 100000 - too close
        too_close_buy = {'side': 'Buy', 'price': 99950.0}
        result = engine._create_place_intent(too_close_buy, 'long', 10)
        assert result is None

    def test_execution_event_before_grid_built(self):
        """ExecutionEvent before grid is built doesn't crash."""
        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config, strat_id='btcusdt_test')

        # Send execution event without building grid first
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

        # last_close is None, so grid.update_grid won't be called
        intents = engine.on_event(exec_event)
        assert len(intents) == 0
        assert engine.last_filled_price == 99800.0


class TestReduceOnlyMap:
    """Tests for _REDUCE_ONLY_MAP: reduce_only flag on PlaceLimitIntent."""

    @pytest.fixture
    def engine(self):
        """GridEngine with a small grid for fast intent generation."""
        config = GridConfig(grid_count=10, grid_step=0.5)
        return GridEngine(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            config=config,
            strat_id="test_ro",
        )

    @pytest.fixture
    def ticker(self):
        """Ticker event at 100000."""
        now = datetime.now(UTC)
        return TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=now,
            local_ts=now,
            last_price=Decimal("100000.0"),
            mark_price=Decimal("100000.0"),
            bid1_price=Decimal("99999.0"),
            ask1_price=Decimal("100001.0"),
            funding_rate=Decimal("0.0001"),
        )

    def test_map_values(self):
        """_REDUCE_ONLY_MAP has correct open/close semantics."""
        m = GridEngine._REDUCE_ONLY_MAP
        # Open orders
        assert m[("long", "Buy")] is False
        assert m[("short", "Sell")] is False
        # Close orders
        assert m[("long", "Sell")] is True
        assert m[("short", "Buy")] is True

    def test_long_buy_is_open(self, engine, ticker):
        """Long direction Buy intents have reduce_only=False (open position)."""
        intents = engine.on_event(ticker, {"long": [], "short": []})
        long_buys = [
            i for i in intents
            if isinstance(i, PlaceLimitIntent)
            and i.direction == "long" and i.side == "Buy"
        ]
        assert len(long_buys) > 0
        for intent in long_buys:
            assert intent.reduce_only is False

    def test_long_sell_is_close(self, engine, ticker):
        """Long direction Sell intents have reduce_only=True (close position)."""
        intents = engine.on_event(ticker, {"long": [], "short": []})
        long_sells = [
            i for i in intents
            if isinstance(i, PlaceLimitIntent)
            and i.direction == "long" and i.side == "Sell"
        ]
        assert len(long_sells) > 0
        for intent in long_sells:
            assert intent.reduce_only is True

    def test_short_sell_is_open(self, engine, ticker):
        """Short direction Sell intents have reduce_only=False (open position)."""
        intents = engine.on_event(ticker, {"long": [], "short": []})
        short_sells = [
            i for i in intents
            if isinstance(i, PlaceLimitIntent)
            and i.direction == "short" and i.side == "Sell"
        ]
        assert len(short_sells) > 0
        for intent in short_sells:
            assert intent.reduce_only is False

    def test_short_buy_is_close(self, engine, ticker):
        """Short direction Buy intents have reduce_only=True (close position)."""
        intents = engine.on_event(ticker, {"long": [], "short": []})
        short_buys = [
            i for i in intents
            if isinstance(i, PlaceLimitIntent)
            and i.direction == "short" and i.side == "Buy"
        ]
        assert len(short_buys) > 0
        for intent in short_buys:
            assert intent.reduce_only is True

    def test_all_intents_have_correct_reduce_only(self, engine, ticker):
        """Every PlaceLimitIntent matches the _REDUCE_ONLY_MAP."""
        intents = engine.on_event(ticker, {"long": [], "short": []})
        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        assert len(place_intents) > 0
        for intent in place_intents:
            expected = GridEngine._REDUCE_ONLY_MAP[(intent.direction, intent.side)]
            assert intent.reduce_only is expected, (
                f"direction={intent.direction}, side={intent.side}: "
                f"expected reduce_only={expected}, got {intent.reduce_only}"
            )


class TestRecenterIntegration:
    """Engine-level tests for feature 0022 (per-tick grid drift detector)."""

    def _ticker(self, price: float) -> TickerEvent:
        return TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal(str(price)),
            mark_price=Decimal(str(price)),
            bid1_price=Decimal(str(price - 0.01)),
            ask1_price=Decimal(str(price + 0.01)),
            funding_rate=Decimal('0.0001'),
        )

    def _restored_grid_around_100(self) -> list[dict]:
        """11-level grid centered at 100 with 1.0% step:
        BUY 95..99, WAIT 100, SELL 101..105."""
        return [
            {'side': 'Buy', 'price': 95.0},
            {'side': 'Buy', 'price': 96.0},
            {'side': 'Buy', 'price': 97.0},
            {'side': 'Buy', 'price': 98.0},
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Sell', 'price': 101.0},
            {'side': 'Sell', 'price': 102.0},
            {'side': 'Sell', 'price': 103.0},
            {'side': 'Sell', 'price': 104.0},
            {'side': 'Sell', 'price': 105.0},
        ]

    def _make_engine(self, restored=True):
        config = GridConfig(grid_count=10, grid_step=1.0)
        kwargs = {
            'symbol': 'BTCUSDT',
            'tick_size': Decimal('0.01'),
            'config': config,
            'strat_id': 'btcusdt_test',
        }
        if restored:
            kwargs['restored_grid'] = self._restored_grid_around_100()
        return GridEngine(**kwargs)

    def test_cold_start_drift_walks_grid(self, caplog):
        """Restored grid centered at 100, ticker at 103.5 (within bounds, dev=3.5% > 1*step).
        Expected n_steps = 3."""
        engine = self._make_engine()
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(103.5), {'long': [], 'short': []})

        # Grid walked: bottom three BUYs dropped, three SELLs added on top.
        prices = [g['price'] for g in engine.grid.grid]
        assert 95.0 not in prices
        assert 96.0 not in prices
        assert 97.0 not in prices
        # Length preserved
        assert len(prices) == 11
        # New top is roughly 105 * (1.01)^3 ≈ 108.18 (after rounding)
        assert prices[-1] > 105.0

        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1
        assert 'N=3' in drift_logs[0]
        assert 'BTCUSDT' in drift_logs[0]

    def test_mid_run_fast_move_recenters_in_one_tick(self, caplog):
        """3 * grid_step jump triggers an N=3 walk in a single tick."""
        engine = self._make_engine()
        # Warm up with an in-band ticker first (no drift)
        engine.on_event(self._ticker(100.2), {'long': [], 'short': []})
        caplog.clear()
        # Now jump 3% above WAIT center
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(103.2), {'long': [], 'short': []})

        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1
        assert 'N=3' in drift_logs[0]

    def test_idempotent_no_second_walk(self, caplog):
        """Two ticker events with the same last_close → only one walk."""
        engine = self._make_engine()
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(103.5), {'long': [], 'short': []})
            engine.on_event(self._ticker(103.5), {'long': [], 'short': []})
        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1

    def test_below_threshold_does_not_walk(self, caplog):
        """Deviation = 0.5 * grid_step → no recenter, no log line."""
        engine = self._make_engine()
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(100.5), {'long': [], 'short': []})
        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert drift_logs == []
        # Grid prices unchanged
        prices = [g['price'] for g in engine.grid.grid]
        assert prices == [g['price'] for g in self._restored_grid_around_100()]

    def test_below_threshold_does_not_migrate_wait_band(self):
        """Sub-step drift must NOT migrate the WAIT band — that would let
        cumulative drift hide indefinitely. Below-threshold ticks are true
        no-ops: prices AND sides unchanged."""
        engine = self._make_engine()
        original_grid = self._restored_grid_around_100()
        # Drift 0.9% (< 1.0 grid_step) — should be no-op
        engine.on_event(self._ticker(100.9), {'long': [], 'short': []})

        for actual, expected in zip(engine.grid.grid, original_grid):
            assert actual['price'] == expected['price']
            # Side must match restored (no migration of WAIT mark)
            assert str(actual['side']) == expected['side'], (
                f"side at {actual['price']} migrated: "
                f"expected {expected['side']}, got {actual['side']}"
            )

    def test_cumulative_sub_step_drift_eventually_walks(self, caplog):
        """A sequence of below-threshold ticks must eventually trigger a walk
        once cumulative drift from the persisted anchor exceeds grid_step."""
        engine = self._make_engine()
        with caplog.at_level('INFO'):
            for price in [100.5, 100.9, 100.99]:
                engine.on_event(self._ticker(price), {'long': [], 'short': []})
            drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
            assert drift_logs == [], "no walk expected yet (all sub-step)"
            # Now cross the threshold against fixed anchor=100
            engine.on_event(self._ticker(101.5), {'long': [], 'short': []})

        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1, f"expected exactly one walk, got {drift_logs}"

    def test_fill_before_first_ticker_is_consumed_once(self):
        """ExecutionEvent that arrives before any TickerEvent (last_close=None)
        must be applied on the first ticker. Pre-0022 the per-tick update_grid
        in _check_and_place would always re-apply it; post-0022 we use a
        _fill_pending flag for one-shot consumption."""
        engine = self._make_engine()
        # Fill arrives before any ticker — last_close is still None.
        fill_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            price=Decimal('99.0'),
            qty=Decimal('0.01'),
            side='Buy',
        )
        engine.on_event(fill_event)
        assert engine._fill_pending is True

        # First ticker — must consume the pending fill exactly once.
        engine.on_event(self._ticker(100.0), {'long': [], 'short': []})
        assert engine._fill_pending is False
        # Filled level (99) is WAIT; level 100 is still the anchor (WAIT from build).
        filled_level = next(g for g in engine.grid.grid if g['price'] == 99.0)
        assert filled_level['side'] == GridSideType.WAIT

    def test_recenter_result_truthy_only_on_walk(self):
        """RecenterResult must be falsy on no-op so callers using
        `if grid.recenter_if_drifted(...):` see only real walks."""
        engine = self._make_engine()
        result_no_walk = engine.grid.recenter_if_drifted(100.5)
        assert bool(result_no_walk) is False
        result_walk = engine.grid.recenter_if_drifted(103.5)
        assert bool(result_walk) is True

    def test_drift_measured_against_current_wait_band_after_fill(self, caplog):
        """Plan Q1: drift reference is the CURRENT WAIT band, not the original
        anchor. After a fill expands the WAIT band, recenter must measure
        against the new wait_center so a tick that crosses one grid_step from
        the new center triggers a walk even when it would not have crossed
        from the original anchor."""
        config = GridConfig(grid_count=10, grid_step=1.0)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.01'),
            config=config,
            strat_id='btcusdt_test',
        )
        # Build at 100.0; original anchor and wait_center both = 100.0.
        engine.on_event(self._ticker(100.0), {'long': [], 'short': []})
        assert engine.grid.anchor_price == 100.0

        # Fill at 99.0 (last_close still 100.0 at fill time). update_grid
        # marks 99.0 as WAIT and the equality keeps 100.0 as WAIT, so the
        # WAIT band becomes [99.0, 100.0] and wait_center becomes 99.5.
        fill = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            price=Decimal('99.0'),
            qty=Decimal('0.01'),
            side='Buy',
        )
        engine.on_event(fill)

        # last_close = 100.6:
        #   - drift from anchor=100.0: 0.6% → would NOT walk if anchor was the reference
        #   - drift from wait_center=99.5: ≈1.106% > 1% → MUST walk per plan
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(100.6), {'long': [], 'short': []})

        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1, (
            f"expected one walk (drift > 1*step from current WAIT center 99.5), "
            f"got {drift_logs}"
        )

    def test_fill_before_first_ticker_with_empty_grid(self):
        """P2: pending fill must be consumed on the first ticker that builds
        the grid (empty-engine path). Pre-fix the consume gate was skipped on
        grid_just_built=True ticks, leaving the fill stranded for one cycle."""
        config = GridConfig(grid_count=10, grid_step=1.0)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.01'),
            config=config,
            strat_id='btcusdt_test',
        )
        # No restored_grid, no anchor — engine starts truly empty.
        assert len(engine.grid.grid) == 0

        fill = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            price=Decimal('99.0'),
            qty=Decimal('0.01'),
            side='Buy',
        )
        engine.on_event(fill)
        assert engine._fill_pending is True

        # First ticker builds the grid AND consumes the pending fill.
        engine.on_event(self._ticker(100.0), {'long': [], 'short': []})
        assert engine._fill_pending is False, "pending fill must be consumed on first build tick"
        filled_level = next(g for g in engine.grid.grid if g['price'] == 99.0)
        assert filled_level['side'] == GridSideType.WAIT

    def test_fresh_build_with_consumed_fill_runs_recenter(self, caplog):
        """If a pending fill consumed on the same tick as build_grid shifts
        the WAIT band far enough from last_close to exceed one grid_step,
        recenter must run before order placement instead of being skipped
        by the grid_just_built guard."""
        config = GridConfig(grid_count=10, grid_step=1.0)
        engine = GridEngine(
            symbol='BTCUSDT',
            tick_size=Decimal('0.01'),
            config=config,
            strat_id='btcusdt_test',
        )

        # Fill at 98.01 — this is a grid level 2 steps below the build anchor
        # (100 * 0.99^2 = 98.01). After build at 100 and post-fill update_grid,
        # WAIT band becomes [98.01, 100.0] → wait_center=99.005. Deviation
        # |100 - 99.005| / 99.005 ≈ 1.005% > 1*grid_step → walk should fire.
        fill = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            price=Decimal('98.01'),
            qty=Decimal('0.01'),
            side='Buy',
        )
        engine.on_event(fill)
        assert engine._fill_pending is True

        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(100.0), {'long': [], 'short': []})

        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1, (
            "fresh-build tick that consumed a pending fill must still run recenter "
            "when the post-fill WAIT center diverges from last_close beyond one step; "
            f"got {drift_logs}"
        )

    def test_stale_fill_does_not_cause_repeated_walks(self, caplog):
        """P1 regression: a stale last_filled_price + existing limits used to
        cause _check_and_place's update_grid to overwrite recenter's WAIT
        assignment, so two identical ticks each triggered a fresh walk.
        After fix, a stale fill must not re-trigger the walk on the second
        identical ticker."""
        engine = self._make_engine()
        # Stale fill from a prior session — last_filled_price set, no fresh fill.
        engine.last_filled_price = 99.0
        existing_limits = {
            'long': [{'orderId': '1', 'price': '99.0', 'side': 'Buy'}],
            'short': [],
        }
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(103.5), existing_limits)
            grid_after_first = [g['price'] for g in engine.grid.grid]
            engine.on_event(self._ticker(103.5), existing_limits)
            grid_after_second = [g['price'] for g in engine.grid.grid]

        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1, f"expected 1 drift log, got {len(drift_logs)}: {drift_logs}"
        assert grid_after_first == grid_after_second, "second identical ticker must not mutate grid"

    def test_out_of_bounds_rebuild_emits_drift_log(self, caplog):
        """P3: when the bounds-guard rebuilds at last_close because price is
        outside the restored grid, the planned 'Grid drift ... N=...' INFO line
        is emitted alongside the existing 'Restored grid out of range' line."""
        engine = self._make_engine()
        # 200 is far outside the restored grid [95..105].
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(200.0), {'long': [], 'short': []})

        msgs = [r.message for r in caplog.records]
        assert any('Grid drift' in m and 'N=' in m for m in msgs)
        assert any('Restored grid out of range' in m for m in msgs)
        assert engine.grid.anchor_price == 200.0

    def test_full_rebuild_fallback_via_engine(self, caplog):
        """deviation that yields n_steps >= grid_count // 2 → full rebuild via fallback.
        With grid_count=10, threshold = 5. A 6% deviation (last_close=106 inside bounds
        of [95..105]? No — 106 is outside max, would trigger engine's own out-of-bounds
        rebuild. Use a wider grid for this case."""
        # Use grid_count=10 still but pick last_close close to upper bound but inside.
        engine = self._make_engine()
        # last_close = 104.99 → deviation ≈ 4.99% → n_steps = 4 → walk path (NOT fallback).
        # Need n_steps >= 5 while staying in [95, 105]. With 1% step and centre=100,
        # max in-bounds last_close ≈ 105 → max deviation ≈ 5%. n_steps = int(5/1) = 5,
        # which equals grid_count // 2 = 5 → fallback triggers.
        # Use last_close = 105.0 exactly (still inside bounds inclusive).
        with caplog.at_level('INFO'):
            engine.on_event(self._ticker(105.0), {'long': [], 'short': []})

        drift_logs = [r.message for r in caplog.records if 'Grid drift' in r.message]
        assert len(drift_logs) == 1
        # Grid is fully rebuilt around 105.0 — anchor must equal 105.0.
        assert engine.grid.anchor_price == 105.0
        # Original prices like 95.0, 96.0 should not survive the rebuild.
        prices = [g['price'] for g in engine.grid.grid]
        assert 95.0 not in prices
