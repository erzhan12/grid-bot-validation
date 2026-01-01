"""
Backtest Order Manager

Extends the existing OrderManager to support backtesting with realistic
slippage simulation and trade recording to BacktestSession.
"""

from datetime import datetime
from typing import Any, Callable, Dict, Optional

from src.backtest_session import BacktestSession, BacktestTrade
from src.constants import COMMISSION_RATE
from src.enums import Direction, OrderEventType, OrderStatus, PositionSide
from src.limit_order import LimitOrder, OrderManager
from src.order_analytics import CrossDirectionStats, DirectionOrderAnalytics
from src.order_lifecycle import OrderLifecycleEvent, OrderLifecycleTracker


class BacktestOrderManager(OrderManager):
    """
    Enhanced OrderManager for backtesting with slippage simulation
    and automatic trade recording to BacktestSession.
    """
    
    def __init__(self, backtest_session: BacktestSession):
        super().__init__()
        self.backtest_session = backtest_session
        self.slippage_bps = 0  # 0.0% default slippage
        self.commission_rate = COMMISSION_RATE

        # Track last filled order per symbol for efficient O(1) lookup
        self.last_filled_order_by_symbol: Dict[str, LimitOrder] = {}

        # Enhanced analytics and tracking
        self.long_analytics = DirectionOrderAnalytics(Direction.LONG.value)
        self.short_analytics = DirectionOrderAnalytics(Direction.SHORT.value)
        self.lifecycle_tracker = OrderLifecycleTracker()
        
    def create_order(self,
                    symbol: str,
                    side: PositionSide,
                    limit_price: float,
                    size: float,
                    direction: str,
                    strategy_id: int,
                    bm_name: str,
                    timestamp: datetime,
                    callback: Optional[Callable] = None,
                    metadata: Optional[Dict[str, Any]] = None,
                    reduce_only: bool = False) -> LimitOrder:
        """
        Create a new limit order with backtest-specific metadata.
        
        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT')
            side: Buy or sell order
            limit_price: Price at which to execute the order
            size: Size of the order
            direction: 'long' or 'short' position direction
            strategy_id: ID of the strategy placing the order
            bm_name: Name of the market maker (balance manager)
            timestamp: When the order was created
            callback: Optional callback function when order is filled
            metadata: Additional metadata
            reduce_only: Whether this is a reduce-only order

        Returns:
            Created LimitOrder object with backtest metadata
        """
        # Create order using parent class with enhanced parameters
        order = super().create_order(
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            size=size,
            direction=direction,
            callback=callback,
            order_direction=Direction(direction.lower()) if direction else None,
            strategy_id=strategy_id,
            bm_name=bm_name,
            metadata=metadata or {},
            reduce_only=reduce_only
        )
        
        # Set timestamp
        order.created_at = timestamp
        
        # Log order creation event
        self.lifecycle_tracker.log_event(
            event_type=OrderEventType.CREATED,
            order_id=order.order_id,
            direction=direction,
            symbol=symbol,
            timestamp=timestamp,
            price=limit_price,
            size=size,
            metadata={'strategy_id': strategy_id, 'bm_name': bm_name}
        )
        
        # Update analytics for order creation
        if order.is_long_direction():
            self.long_analytics.update_with_order(order)
        elif order.is_short_direction():
            self.short_analytics.update_with_order(order)
        
        print(f"Order created: {order.order_id} {side.value} {size} {symbol} @ {limit_price} ({direction})")
        
        return order
    
    def check_fills(self, symbol: str, current_price: float, timestamp: datetime) -> list[LimitOrder]:
        """
        Check for order fills with realistic slippage simulation.
        
        Args:
            symbol: Trading symbol to check
            current_price: Current market price
            timestamp: Current timestamp
            
        Returns:
            List of orders that were filled
        """
        filled_orders = []
        orders_to_remove = []
        
        for order_id, order in list(self.active_orders.items()):
            if order.symbol != symbol:
                continue
                
            if self._should_fill_with_slippage(order, current_price):
                fill_price = self._calculate_fill_price(order, current_price)
                
                if order.fill(fill_price, timestamp):
                    filled_orders.append(order)
                    orders_to_remove.append(order_id)

                    # Cache this as the last filled order for the symbol (O(1) update)
                    self.last_filled_order_by_symbol[symbol] = order

                    # Log fill event
                    self.lifecycle_tracker.log_event(
                        event_type=OrderEventType.FILLED,
                        order_id=order.order_id,
                        direction=order.get_direction(),
                        symbol=symbol,
                        timestamp=timestamp,
                        price=fill_price,
                        size=order.size,
                        metadata={'slippage': order.get_slippage(current_price)}
                    )
                    
                    # Record trade in backtest session
                    self._record_trade(order, fill_price, timestamp)
                    
                    # Move to history
                    self.order_history.append(order)
                    
                    # Update analytics with fill time (only when order is actually filled)
                    if order.is_long_direction():
                        self.long_analytics.update_with_order(order, timestamp, is_new_order=False)
                    elif order.is_short_direction():
                        self.short_analytics.update_with_order(order, timestamp, is_new_order=False)
                    
            elif order.status != OrderStatus.PENDING:
                # Order expired or was cancelled
                orders_to_remove.append(order_id)
                self.order_history.append(order)
        
        # Remove filled/expired orders from active orders
        for order_id in orders_to_remove:
            del self.active_orders[order_id]
            
        return filled_orders
    
    def cancel_order(self, order_id: str, timestamp: datetime) -> bool:
        """
        Cancel an active order with timestamp tracking.
        
        Args:
            order_id: ID of the order to cancel
            timestamp: When the order was cancelled
            
        Returns:
            True if order was cancelled, False otherwise
        """
        if order_id not in self.active_orders:
            return False
        
        order = self.active_orders[order_id]
        if order.cancel():
            # Log cancellation event
            self.lifecycle_tracker.log_event(
                event_type=OrderEventType.CANCELLED,
                order_id=order.order_id,
                direction=order.get_direction(),
                symbol=order.symbol,
                timestamp=timestamp,
                price=order.limit_price,
                size=order.size,
                reason="Manual cancellation"
            )
            
            print(f"Order cancelled: {order_id} {order.side.value} {order.size} @ {order.limit_price} ({order.direction})")
            self.order_history.append(order)
            del self.active_orders[order_id]
            return True
        
        return False
    
    def _should_fill_with_slippage(self, order: LimitOrder, current_price: float) -> bool:
        """
        Check if order should fill considering realistic market conditions.
        
        Args:
            order: The order to check
            current_price: Current market price
            
        Returns:
            True if order should be filled
        """
        if order.side == PositionSide.BUY:
            # Buy limit: fill when market price is at or below limit price
            return current_price <= order.limit_price
        else:  # SELL
            # Sell limit: fill when market price is at or above limit price
            return current_price >= order.limit_price
    
    def _calculate_fill_price(self, order: LimitOrder, current_price: float) -> float:
        """
        Calculate fill price - always use the limit price.
        
        Args:
            order: The order being filled
            current_price: Current market price (not used)
            
        Returns:
            The limit price of the order
        """
        return order.limit_price
    
    def _record_trade(self, order: LimitOrder, fill_price: float, timestamp: datetime):
        """
        Record the executed trade in the backtest session.
        
        Args:
            order: The filled order
            fill_price: Price at which order was filled
            timestamp: When the order was filled
        """
        # Calculate commission
        trade_value = order.size * fill_price
        commission = trade_value * self.commission_rate
        
        # For now, set realized_pnl to 0 - this will be calculated properly
        # when positions are closed in the position management system
        realized_pnl = -commission  # Start with just commission cost
        
        trade = BacktestTrade(
            trade_id=f"TRADE_{len(self.backtest_session.trades) + 1:06d}",
            symbol=order.symbol,
            side=order.side.value,
            size=order.size,
            price=fill_price,
            direction=order.direction,
            executed_at=timestamp,
            order_id=order.order_id,
            strategy_id=order.strategy_id,
            bm_name=order.bm_name,
            realized_pnl=realized_pnl
        )
        
        self.backtest_session.record_trade(trade)
    
    def get_active_orders_count(self, symbol: Optional[str] = None) -> int:
        """Get count of active orders, optionally filtered by symbol."""
        if symbol:
            return len([o for o in self.active_orders.values() if o.symbol == symbol])
        return len(self.active_orders)
    
    def get_orders_by_direction(self, symbol: str, direction: str) -> list[LimitOrder]:
        """Get active orders for a specific symbol and direction."""
        return [order for order in self.active_orders.values() 
                if order.symbol == symbol and hasattr(order, 'direction') and order.direction == direction]
    
    def set_slippage(self, slippage_bps: float):
        """Set slippage in basis points (e.g., 5 = 0.05%)."""
        self.slippage_bps = slippage_bps
    
    def set_commission_rate(self, commission_rate: float):
        """Set commission rate as decimal (e.g., 0.0006 = 0.06%)."""
        self.commission_rate = commission_rate
    
    def get_statistics(self) -> dict:
        """Get order management statistics."""
        total_orders = len(self.order_history) + len(self.active_orders)
        filled_orders = len([o for o in self.order_history if o.is_filled()])
        cancelled_orders = len([o for o in self.order_history if o.status == OrderStatus.CANCELLED])
        
        return {
            'total_orders_created': total_orders,
            'active_orders': len(self.active_orders),
            'filled_orders': filled_orders,
            'cancelled_orders': cancelled_orders,
            'fill_rate': filled_orders / total_orders if total_orders > 0 else 0.0,
            'slippage_bps': self.slippage_bps,
            'commission_rate': self.commission_rate
        }
    
    def get_direction_analytics(self, direction: str) -> DirectionOrderAnalytics:
        """Get analytics for a specific direction"""
        if direction.lower() == Direction.LONG.value:
            return self.long_analytics
        elif direction.lower() == Direction.SHORT.value:
            return self.short_analytics
        else:
            raise ValueError(f"Invalid direction: {direction}")
    
    def get_cross_direction_stats(self) -> CrossDirectionStats:
        """Get cross-direction comparison statistics"""
        return CrossDirectionStats(
            long_analytics=self.long_analytics,
            short_analytics=self.short_analytics
        )
    
    def get_order_lifecycle_events(self, order_id: str) -> list[OrderLifecycleEvent]:
        """Get all lifecycle events for a specific order"""
        return self.lifecycle_tracker.get_events_for_order(order_id)
    
    def get_direction_lifecycle_summary(self, direction: str) -> dict:
        """Get lifecycle event summary for a direction"""
        return self.lifecycle_tracker.get_direction_summary(direction)
    
    def get_enhanced_statistics(self) -> dict:
        """Get enhanced statistics including direction-specific data"""
        base_stats = self.get_statistics()
        
        # Add direction-specific stats
        long_stats = self.get_direction_stats(Direction.LONG.value)
        short_stats = self.get_direction_stats(Direction.SHORT.value)
        
        # Add cross-direction comparison
        cross_stats = self.get_cross_direction_stats()
        
        return {
            **base_stats,
            'long_orders': long_stats,
            'short_orders': short_stats,
            'cross_direction': {
                'order_imbalance': cross_stats.order_imbalance,
                'volume_imbalance': cross_stats.volume_imbalance,
                'performance_difference': cross_stats.performance_difference
            },
            'lifecycle_events': {
                'total_events': self.lifecycle_tracker.get_total_events_count(),
                'long_events': self.get_direction_lifecycle_summary(Direction.LONG.value),
                'short_events': self.get_direction_lifecycle_summary(Direction.SHORT.value)
            }
        }

    def get_last_filled_order(self, symbol: str) -> Optional[LimitOrder]:
        """
        Get the last filled order for a symbol (O(1) lookup).

        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT')

        Returns:
            Last filled LimitOrder for the symbol, or None if no orders filled
        """
        return self.last_filled_order_by_symbol.get(symbol)
