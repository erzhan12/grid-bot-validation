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

        # Verify anchor_price still returns original center (100k), not filled price (99800)
        anchor_after_fill = engine.get_anchor_price()
        assert anchor_after_fill == 100000.0, \
            f"Expected anchor_price to remain 100000.0 after fill, got {anchor_after_fill}"

        # Only the filled level should be WAIT (price moved away from original center)
        wait_items = [g for g in engine.grid.grid if g['side'] == GridSideType.WAIT]
        assert len(wait_items) == 1, \
            f"Should have only 1 WAIT item (filled level), got {len(wait_items)}: {[g['price'] for g in wait_items]}"
        assert wait_items[0]['price'] == 99800.0, \
            f"WAIT should be at filled price 99800, got {wait_items[0]['price']}"

        # Original center should now be SELL (since price 99850 < 100000)
        center_after_fill = next(g for g in engine.grid.grid if g['price'] == 100000.0)
        assert center_after_fill['side'] == GridSideType.SELL, \
            f"Original center should be SELL after price moved below it, got {center_after_fill['side']}"


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
