"""
Tests for Direction-Based Order Separation

This module tests the enhanced order management system with direction-specific
collections, analytics, and lifecycle tracking.
"""

from datetime import datetime

import pytest

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestSession
from src.enums import Direction, OrderEventType, OrderStatus, PositionSide
from src.limit_order import LimitOrder, OrderManager
from src.order_analytics import CrossDirectionStats, DirectionOrderAnalytics
from src.order_lifecycle import OrderLifecycleTracker


class TestDirectionSeparation:
    """Test direction-based order separation functionality"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.order_manager = OrderManager()
        self.backtest_session = BacktestSession("test_session")
        self.backtest_order_manager = BacktestOrderManager(self.backtest_session)
        self.symbol = "BTCUSDT"
        self.timestamp = datetime.now()
    
    def test_create_long_order(self):
        """Test creating a long direction order"""
        order = self.order_manager.create_long_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            strategy_id=1,
            bm_name="test_bm"
        )
        
        assert order.is_long_direction()
        assert order.order_direction == Direction.LONG
        assert order.strategy_id == 1
        assert order.bm_name == "test_bm"
        assert order.order_id in self.order_manager.active_long_orders
        assert order.order_id in self.order_manager.active_orders
    
    def test_create_short_order(self):
        """Test creating a short direction order"""
        order = self.order_manager.create_short_order(
            symbol=self.symbol,
            side=PositionSide.SELL,
            limit_price=51000.0,
            size=0.1,
            strategy_id=1,
            bm_name="test_bm"
        )
        
        assert order.is_short_direction()
        assert order.order_direction == Direction.SHORT
        assert order.strategy_id == 1
        assert order.bm_name == "test_bm"
        assert order.order_id in self.order_manager.active_short_orders
        assert order.order_id in self.order_manager.active_orders
    
    def test_direction_specific_collections(self):
        """Test that orders are stored in correct direction-specific collections"""
        # Create orders of different directions
        long_order = self.order_manager.create_long_order(
            symbol=self.symbol, side=PositionSide.BUY, limit_price=50000.0, size=0.1
        )
        short_order = self.order_manager.create_short_order(
            symbol=self.symbol, side=PositionSide.SELL, limit_price=51000.0, size=0.1
        )
        
        # Check long orders collection
        assert len(self.order_manager.active_long_orders) == 1
        assert long_order.order_id in self.order_manager.active_long_orders
        assert short_order.order_id not in self.order_manager.active_long_orders
        
        # Check short orders collection
        assert len(self.order_manager.active_short_orders) == 1
        assert short_order.order_id in self.order_manager.active_short_orders
        assert long_order.order_id not in self.order_manager.active_short_orders
        
        # Check unified collection
        assert len(self.order_manager.active_orders) == 2
        assert long_order.order_id in self.order_manager.active_orders
        assert short_order.order_id in self.order_manager.active_orders
    
    def test_get_orders_by_direction(self):
        """Test retrieving orders by direction"""
        # Create mixed orders
        long_order1 = self.order_manager.create_long_order(
            symbol=self.symbol, side=PositionSide.BUY, limit_price=50000.0, size=0.1
        )
        long_order2 = self.order_manager.create_long_order(
            symbol=self.symbol, side=PositionSide.BUY, limit_price=49500.0, size=0.1
        )
        short_order = self.order_manager.create_short_order(
            symbol=self.symbol, side=PositionSide.SELL, limit_price=51000.0, size=0.1
        )
        
        # Test getting long orders
        long_orders = self.order_manager.get_orders_by_direction(Direction.LONG.value)
        assert len(long_orders) == 2
        assert long_order1 in long_orders
        assert long_order2 in long_orders
        assert short_order not in long_orders
        
        # Test getting short orders
        short_orders = self.order_manager.get_orders_by_direction(Direction.SHORT.value)
        assert len(short_orders) == 1
        assert short_order in short_orders
        assert long_order1 not in short_orders
        assert long_order2 not in short_orders
    
    def test_cancel_direction_orders(self):
        """Test cancelling orders by direction"""
        # Create orders
        long_order1 = self.order_manager.create_long_order(
            symbol=self.symbol, side=PositionSide.BUY, limit_price=50000.0, size=0.1
        )
        long_order2 = self.order_manager.create_long_order(
            symbol=self.symbol, side=PositionSide.BUY, limit_price=49500.0, size=0.1
        )
        short_order = self.order_manager.create_short_order(
            symbol=self.symbol, side=PositionSide.SELL, limit_price=51000.0, size=0.1
        )
        
        # Cancel all long orders
        cancelled_count = self.order_manager.cancel_long_orders(self.symbol)
        assert cancelled_count == 2
        
        # Check that long orders are cancelled and moved to history
        assert len(self.order_manager.active_long_orders) == 0
        assert len(self.order_manager.long_order_history) == 2
        assert long_order1.status == OrderStatus.CANCELLED
        assert long_order2.status == OrderStatus.CANCELLED
        
        # Check that short order is still active
        assert len(self.order_manager.active_short_orders) == 1
        assert short_order.status == OrderStatus.PENDING
    
    def test_direction_stats(self):
        """Test direction-specific statistics"""
        # Create orders
        long_order1 = self.order_manager.create_long_order(
            symbol=self.symbol, side=PositionSide.BUY, limit_price=50000.0, size=0.1
        )
        self.order_manager.create_long_order(
            symbol=self.symbol, side=PositionSide.BUY, limit_price=49500.0, size=0.1
        )
        self.order_manager.create_short_order(
            symbol=self.symbol, side=PositionSide.SELL, limit_price=51000.0, size=0.1
        )
        
        # Cancel one long order
        self.order_manager.cancel_order(long_order1.order_id)
        
        # Get long direction stats
        long_stats = self.order_manager.get_direction_stats(Direction.LONG.value)
        assert long_stats['direction'] == Direction.LONG.value
        assert long_stats['total_orders'] == 2
        assert long_stats['active_orders'] == 1
        assert long_stats['cancelled_orders'] == 1
        assert long_stats['fill_rate'] == 0.0  # No filled orders
        
        # Get short direction stats
        short_stats = self.order_manager.get_direction_stats(Direction.SHORT.value)
        assert short_stats['direction'] == Direction.SHORT.value
        assert short_stats['total_orders'] == 1
        assert short_stats['active_orders'] == 1
        assert short_stats['cancelled_orders'] == 0


class TestBacktestOrderManagerDirectionSeparation:
    """Test direction separation in BacktestOrderManager"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.backtest_session = BacktestSession("test_session")
        self.order_manager = BacktestOrderManager(self.backtest_session)
        self.symbol = "BTCUSDT"
        self.timestamp = datetime.now()
    
    def test_create_order_with_lifecycle_tracking(self):
        """Test order creation with lifecycle event tracking"""
        order = self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            direction=Direction.LONG.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        # Check order properties
        assert order.is_long_direction()
        assert order.strategy_id == 1
        assert order.bm_name == "test_bm"
        
        # Check lifecycle events
        events = self.order_manager.get_order_lifecycle_events(order.order_id)
        assert len(events) == 1
        assert events[0].event_type == OrderEventType.CREATED
        assert events[0].order_id == order.order_id
        assert events[0].direction == Direction.LONG.value
    
    def test_order_fill_with_lifecycle_tracking(self):
        """Test order filling with lifecycle event tracking"""
        # Create order
        order = self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            direction=Direction.LONG.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        # Fill the order
        filled_orders = self.order_manager.check_fills(self.symbol, 49500.0, self.timestamp)
        
        assert len(filled_orders) == 1
        assert filled_orders[0].order_id == order.order_id
        assert filled_orders[0].is_filled()
        
        # Check lifecycle events
        events = self.order_manager.get_order_lifecycle_events(order.order_id)
        assert len(events) == 2  # CREATED + FILLED
        assert events[1].event_type == OrderEventType.FILLED
        assert events[1].price == 50000.0  # Fill price should be limit price
    
    def test_order_cancellation_with_lifecycle_tracking(self):
        """Test order cancellation with lifecycle event tracking"""
        # Create order
        order = self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            direction=Direction.LONG.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        # Cancel the order
        success = self.order_manager.cancel_order(order.order_id, self.timestamp)
        
        assert success
        assert order.status == OrderStatus.CANCELLED
        
        # Check lifecycle events
        events = self.order_manager.get_order_lifecycle_events(order.order_id)
        assert len(events) == 2  # CREATED + CANCELLED
        assert events[1].event_type == OrderEventType.CANCELLED
        assert events[1].reason == "Manual cancellation"
    
    def test_direction_analytics(self):
        """Test direction-specific analytics"""
        # Create orders
        self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            direction=Direction.LONG.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.SELL,
            limit_price=51000.0,
            size=0.1,
            direction=Direction.SHORT.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        # Fill long order
        self.order_manager.check_fills(self.symbol, 49500.0, self.timestamp)
        
        # Get analytics
        long_analytics = self.order_manager.get_direction_analytics(Direction.LONG.value)
        short_analytics = self.order_manager.get_direction_analytics(Direction.SHORT.value)
        
        assert long_analytics.direction == Direction.LONG.value
        assert long_analytics.total_orders == 1
        assert long_analytics.filled_orders == 1
        assert long_analytics.fill_rate == 1.0
        
        assert short_analytics.direction == Direction.SHORT.value
        assert short_analytics.total_orders == 1
        assert short_analytics.filled_orders == 0
        assert short_analytics.fill_rate == 0.0
    
    def test_cross_direction_stats(self):
        """Test cross-direction comparison statistics"""
        # Create orders
        self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            direction=Direction.LONG.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=49500.0,
            size=0.1,
            direction=Direction.LONG.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.SELL,
            limit_price=51000.0,
            size=0.1,
            direction=Direction.SHORT.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        # Get cross-direction stats
        cross_stats = self.order_manager.get_cross_direction_stats()
        
        assert cross_stats.order_imbalance == 2.0  # 2 long / 1 short
        assert cross_stats.volume_imbalance == 2.0  # 0.2 long / 0.1 short
    
    def test_enhanced_statistics(self):
        """Test enhanced statistics including direction-specific data"""
        # Create and fill orders
        self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            direction=Direction.LONG.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        self.order_manager.create_order(
            symbol=self.symbol,
            side=PositionSide.SELL,
            limit_price=51000.0,
            size=0.1,
            direction=Direction.SHORT.value,
            strategy_id=1,
            bm_name="test_bm",
            timestamp=self.timestamp
        )
        
        # Fill long order
        self.order_manager.check_fills(self.symbol, 49500.0, self.timestamp)
        
        # Get enhanced statistics
        stats = self.order_manager.get_enhanced_statistics()
        
        # Check base stats
        assert 'total_orders_created' in stats
        assert 'active_orders' in stats
        assert 'filled_orders' in stats
        
        # Check direction-specific stats
        assert 'long_orders' in stats
        assert 'short_orders' in stats
        assert stats['long_orders']['direction'] == Direction.LONG.value
        assert stats['short_orders']['direction'] == Direction.SHORT.value
        
        # Check cross-direction stats
        assert 'cross_direction' in stats
        assert 'order_imbalance' in stats['cross_direction']
        assert 'volume_imbalance' in stats['cross_direction']
        
        # Check lifecycle events
        assert 'lifecycle_events' in stats
        assert 'total_events' in stats['lifecycle_events']
        assert 'long_events' in stats['lifecycle_events']
        assert 'short_events' in stats['lifecycle_events']


class TestOrderAnalytics:
    """Test order analytics functionality"""
    
    def test_direction_order_analytics(self):
        """Test DirectionOrderAnalytics class"""
        analytics = DirectionOrderAnalytics(Direction.LONG.value)
        
        # Create mock order
        order = LimitOrder(
            order_id="TEST_001",
            symbol="BTCUSDT",
            side=PositionSide.BUY,
            limit_price=50000.0,
            size=0.1,
            direction=Direction.LONG.value,
            order_direction=Direction.LONG,
            created_at=datetime.now()
        )
        
        # Update analytics
        analytics.update_with_order(order)
        
        assert analytics.direction == Direction.LONG.value
        assert analytics.total_orders == 1
        assert analytics.total_volume == 0.1
        assert analytics.avg_order_size == 0.1
    
    def test_cross_direction_stats(self):
        """Test CrossDirectionStats class"""
        long_analytics = DirectionOrderAnalytics(Direction.LONG.value)
        short_analytics = DirectionOrderAnalytics(Direction.SHORT.value)
        
        # Add some orders to long analytics
        long_analytics.total_orders = 3
        long_analytics.total_volume = 0.3
        
        # Add some orders to short analytics
        short_analytics.total_orders = 2
        short_analytics.total_volume = 0.2
        
        # Create cross-direction stats
        cross_stats = CrossDirectionStats(long_analytics, short_analytics)
        
        assert cross_stats.order_imbalance == 1.5  # 3 / 2
        assert abs(cross_stats.volume_imbalance - 1.5) < 0.0001  # 0.3 / 0.2 (with floating point tolerance)


class TestOrderLifecycleTracker:
    """Test order lifecycle tracking functionality"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.tracker = OrderLifecycleTracker()
        self.timestamp = datetime.now()
    
    def test_log_event(self):
        """Test logging lifecycle events"""
        event_id = self.tracker.log_event(
            event_type=OrderEventType.CREATED,
            order_id="TEST_001",
            direction=Direction.LONG.value,
            symbol="BTCUSDT",
            timestamp=self.timestamp,
            price=50000.0,
            size=0.1
        )
        
        assert event_id.startswith("EVENT_")
        assert len(self.tracker.events) == 1
        
        event = self.tracker.events[0]
        assert event.event_type == OrderEventType.CREATED
        assert event.order_id == "TEST_001"
        assert event.direction == Direction.LONG.value
    
    def test_get_events_for_order(self):
        """Test retrieving events for a specific order"""
        # Log multiple events for the same order
        self.tracker.log_event(
            OrderEventType.CREATED, "TEST_001", Direction.LONG.value, "BTCUSDT", self.timestamp
        )
        self.tracker.log_event(
            OrderEventType.FILLED, "TEST_001", Direction.LONG.value, "BTCUSDT", self.timestamp
        )
        self.tracker.log_event(
            OrderEventType.CREATED, "TEST_002", Direction.SHORT.value, "BTCUSDT", self.timestamp
        )
        
        # Get events for TEST_001
        events = self.tracker.get_events_for_order("TEST_001")
        assert len(events) == 2
        assert all(event.order_id == "TEST_001" for event in events)
    
    def test_filter_events(self):
        """Test filtering events by multiple criteria"""
        # Log events
        self.tracker.log_event(
            OrderEventType.CREATED, "TEST_001", Direction.LONG.value, "BTCUSDT", self.timestamp
        )
        self.tracker.log_event(
            OrderEventType.FILLED, "TEST_001", Direction.LONG.value, "BTCUSDT", self.timestamp
        )
        self.tracker.log_event(
            OrderEventType.CREATED, "TEST_002", Direction.SHORT.value, "ETHUSDT", self.timestamp
        )
        
        # Filter by direction
        long_events = self.tracker.filter_events(direction=Direction.LONG.value)
        assert len(long_events) == 2
        
        # Filter by symbol
        btc_events = self.tracker.filter_events(symbol="BTCUSDT")
        assert len(btc_events) == 2
        
        # Filter by event type
        created_events = self.tracker.filter_events(event_type=OrderEventType.CREATED)
        assert len(created_events) == 2
        
        # Filter by multiple criteria
        long_created_events = self.tracker.filter_events(
            direction=Direction.LONG.value,
            event_type=OrderEventType.CREATED
        )
        assert len(long_created_events) == 1


if __name__ == "__main__":
    pytest.main([__file__])
