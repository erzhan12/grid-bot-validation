"""
Limit Order Management System

This module provides classes for managing limit orders in the trading system.
Orders can be created, monitored, and automatically filled when market conditions are met.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from src.enums import Direction, OrderStatus, PositionSide


@dataclass
class LimitOrder:
    """
    Represents a limit order in the trading system.
    
    A limit order is an order to buy or sell at a specific price or better.
    - Buy limit: Execute when market price <= limit_price
    - Sell limit: Execute when market price >= limit_price
    """

    # Order identification
    order_id: str
    symbol: str
    side: PositionSide
    # Order parameters
    limit_price: float
    size: float
    direction: str

    # Order lifecycle
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = None
    filled_at: Optional[datetime] = None
    fill_price: Optional[float] = None

    # Enhanced direction tracking
    order_direction: Optional[Direction] = None
    strategy_id: Optional[int] = None
    bm_name: Optional[str] = None
    reduce_only: bool = False

    # Order metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Optional features
    callback: Optional[Callable] = None  # Function to call when order is filled

    def should_fill(self, current_price: float) -> bool:
        """
        Check if order should be filled based on current market price.
        
        Args:
            current_price: Current market price
            
        Returns:
            True if order should be filled, False otherwise
        """
        if self.status != OrderStatus.PENDING:
            return False

        # Check fill conditions
        if self.side == PositionSide.BUY:
            # Buy limit: fill when market price is at or below limit price
            return current_price <= self.limit_price
        elif self.side == PositionSide.SELL:
            # Sell limit: fill when market price is at or above limit price
            return current_price >= self.limit_price

        return False

    def fill(self, fill_price: float, datetime: datetime) -> bool:
        """
        Fill the order at the specified price.
        
        Args:
            fill_price: Price at which order was filled
            
        Returns:
            True if order was successfully filled, False otherwise
        """
        if self.status != OrderStatus.PENDING:
            return False

        self.status = OrderStatus.FILLED
        self.filled_at = datetime
        self.fill_price = fill_price

        # Execute callback if provided
        if self.callback:
            try:
                self.callback(self)
            except Exception as e:
                print(f"Error executing order callback: {e}")

        return True

    def cancel(self) -> bool:
        """
        Cancel the order.
        
        Returns:
            True if order was successfully cancelled, False otherwise
        """
        if self.status != OrderStatus.PENDING:
            return False

        self.status = OrderStatus.CANCELLED
        return True

    def is_active(self) -> bool:
        """Check if order is still active (pending)"""
        return self.status == OrderStatus.PENDING

    def is_filled(self) -> bool:
        """Check if order has been filled"""
        return self.status == OrderStatus.FILLED

    def get_direction(self) -> str:
        """Get the order direction as string"""
        if self.order_direction:
            return self.order_direction.value
        return self.direction
    
    def is_long_direction(self) -> bool:
        """Check if this is a long direction order"""
        return self.get_direction().lower() == Direction.LONG.value
    
    def is_short_direction(self) -> bool:
        """Check if this is a short direction order"""
        return self.get_direction().lower() == Direction.SHORT.value
    
    def set_metadata(self, key: str, value: Any) -> None:
        """Set metadata for the order"""
        self.metadata[key] = value
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Get metadata value for the order"""
        return self.metadata.get(key, default)
    
    def get_fill_duration(self) -> Optional[float]:
        """Get fill duration in seconds if order was filled"""
        if self.filled_at and self.created_at:
            return (self.filled_at - self.created_at).total_seconds()
        return None
    
    def get_slippage(self, market_price: float) -> float:
        """Calculate slippage from market price"""
        if not self.fill_price:
            return 0.0
        
        if self.side == PositionSide.BUY:
            return self.fill_price - market_price
        else:  # SELL
            return market_price - self.fill_price
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert order to dictionary for serialization"""
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'limit_price': self.limit_price,
            'size': self.size,
            'direction': self.direction,
            'order_direction': self.order_direction.value if self.order_direction else None,
            'status': self.status.value,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'filled_at': self.filled_at.isoformat() if self.filled_at else None,
            'fill_price': self.fill_price,
            'strategy_id': self.strategy_id,
            'bm_name': self.bm_name,
            'metadata': self.metadata
        }

    def __str__(self) -> str:  # noqa: C901
        """String representation of the order"""  # noqa: C901 
        direction_str = f"({self.get_direction()})" if self.get_direction() else ""
        return (f"LimitOrder({self.order_id}: {self.side.value.upper()} "
                f"{self.size} {self.symbol} @ {self.limit_price} {direction_str} - {self.status.upper()})")
    
    def __repr__(self) -> str:
        """String representation of the order"""  # noqa: C901
        return self.__str__()


class OrderManager:
    """
    Manages a collection of limit orders.
    
    Provides functionality to:
    - Add new orders
    - Check orders for fills
    - Cancel orders
    - Track order history
    - Direction-specific order management
    """

    def __init__(self):
        # Unified order storage (backward compatibility)
        self.active_orders: dict[str, LimitOrder] = {}
        self.order_history: list[LimitOrder] = []
        
        # Direction-specific collections
        self.active_long_orders: dict[str, LimitOrder] = {}
        self.active_short_orders: dict[str, LimitOrder] = {}
        self.long_order_history: list[LimitOrder] = []
        self.short_order_history: list[LimitOrder] = []
        
        self._next_order_id = 1

    def create_order(self,
                     symbol: str,
                     side: PositionSide,
                     limit_price: float,
                     size: float,
                     direction: str,
                     callback: Optional[Callable] = None,
                     order_direction: Optional[Direction] = None,
                     strategy_id: Optional[int] = None,
                     bm_name: Optional[str] = None,
                     metadata: Optional[Dict[str, Any]] = None,
                     reduce_only: bool = False) -> LimitOrder:
        """
        Create a new limit order.
        
        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT')
            side: Buy or sell order
            limit_price: Price at which to execute the order
            size: Size of the order
            direction: Order direction (long/short)
            callback: Optional callback function when order is filled
            order_direction: Enhanced direction enum
            strategy_id: Strategy ID
            bm_name: Balance manager name
            metadata: Additional metadata
            reduce_only: Whether this is a reduce-only order

        Returns:
            Created LimitOrder object
        """
        order_id = f"ORDER_{self._next_order_id:06d}"
        self._next_order_id += 1

        # Determine order direction
        if order_direction is None:
            order_direction = Direction(direction.lower()) if direction else None

        order = LimitOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            size=size,
            direction=direction,
            order_direction=order_direction,
            strategy_id=strategy_id,
            bm_name=bm_name,
            metadata=metadata or {},
            callback=callback,
            reduce_only=reduce_only
        )

        # Store in unified collection (backward compatibility)
        self.active_orders[order_id] = order
        
        # Store in direction-specific collections
        if order.is_long_direction():
            self.active_long_orders[order_id] = order
        elif order.is_short_direction():
            self.active_short_orders[order_id] = order
        
        return order

    def check_fills(self, symbol: str, current_price: float, datetime: datetime) -> list[LimitOrder]:  # noqa: C901
        """
        Check all active orders for the given symbol and fill if conditions are met.
        
        Args:
            symbol: Trading symbol to check
            current_price: Current market price
            
        Returns:
            List of orders that were filled
        """
        filled_orders = []
        orders_to_remove = []

        for order_id, order in self.active_orders.items():
            if order.symbol != symbol:
                continue

            if order.should_fill(current_price):
                if order.fill(current_price, datetime):
                    filled_orders.append(order)
                    orders_to_remove.append(order_id)
                    self.order_history.append(order)
                    
                    # Add to direction-specific history
                    if order.is_long_direction():
                        self.long_order_history.append(order)
                    elif order.is_short_direction():
                        self.short_order_history.append(order)
            elif order.status != OrderStatus.PENDING:
                # Order expired or was cancelled
                orders_to_remove.append(order_id)
                self.order_history.append(order)
                
                # Add to direction-specific history
                if order.is_long_direction():
                    self.long_order_history.append(order)
                elif order.is_short_direction():
                    self.short_order_history.append(order)

        # Remove filled/expired orders from active orders
        for order_id in orders_to_remove:
            del self.active_orders[order_id]
            
            # Remove from direction-specific collections
            if order_id in self.active_long_orders:
                del self.active_long_orders[order_id]
            if order_id in self.active_short_orders:
                del self.active_short_orders[order_id]

        return filled_orders

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an active order.
        
        Args:
            order_id: ID of the order to cancel
            
        Returns:
            True if order was cancelled, False otherwise
        """
        if order_id not in self.active_orders:
            return False

        order = self.active_orders[order_id]
        if order.cancel():
            self.order_history.append(order)
            
            # Add to direction-specific history
            if order.is_long_direction():
                self.long_order_history.append(order)
            elif order.is_short_direction():
                self.short_order_history.append(order)
            
            del self.active_orders[order_id]
            
            # Remove from direction-specific collections
            if order_id in self.active_long_orders:
                del self.active_long_orders[order_id]
            if order_id in self.active_short_orders:
                del self.active_short_orders[order_id]
            
            return True

        return False

    def get_active_orders(self, symbol: Optional[str] = None) -> list[LimitOrder]:
        """
        Get all active orders, optionally filtered by symbol.
        
        Args:
            symbol: Optional symbol filter
            
        Returns:
            List of active orders
        """
        if symbol:
            return [order for order in self.active_orders.values() if order.symbol == symbol]
        return list(self.active_orders.values())

    def get_order_history(self, symbol: Optional[str] = None) -> list[LimitOrder]:
        """
        Get order history, optionally filtered by symbol.
        
        Args:
            symbol: Optional symbol filter
            
        Returns:
            List of historical orders
        """
        if symbol:
            return [order for order in self.order_history if order.symbol == symbol]
        return self.order_history.copy()

    def clear_history(self):
        """Clear order history"""
        self.order_history.clear()

    def get_stats(self) -> dict:
        """Get order statistics"""
        total_orders = len(self.order_history) + len(self.active_orders)
        filled_orders = len([o for o in self.order_history if o.is_filled()])
        cancelled_orders = len([o for o in self.order_history if o.status == OrderStatus.CANCELLED])

        return {
            'total_orders': total_orders,
            'active_orders': len(self.active_orders),
            'filled_orders': filled_orders,
            'cancelled_orders': cancelled_orders,
            'fill_rate': filled_orders / total_orders if total_orders > 0 else 0.0
        }
    
    # Direction-specific methods
    
    def create_long_order(self, 
                         symbol: str,
                         side: PositionSide,
                         limit_price: float,
                         size: float,
                         callback: Optional[Callable] = None,
                         strategy_id: Optional[int] = None,
                         bm_name: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None) -> LimitOrder:
        """Create a long direction order"""
        return self.create_order(
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            size=size,
            direction=Direction.LONG.value,
            callback=callback,
            order_direction=Direction.LONG,
            strategy_id=strategy_id,
            bm_name=bm_name,
            metadata=metadata
        )
    
    def create_short_order(self, 
                          symbol: str,
                          side: PositionSide,
                          limit_price: float,
                          size: float,
                          callback: Optional[Callable] = None,
                          strategy_id: Optional[int] = None,
                          bm_name: Optional[str] = None,
                          metadata: Optional[Dict[str, Any]] = None) -> LimitOrder:
        """Create a short direction order"""
        return self.create_order(
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            size=size,
            direction=Direction.SHORT.value,
            callback=callback,
            order_direction=Direction.SHORT,
            strategy_id=strategy_id,
            bm_name=bm_name,
            metadata=metadata
        )
    
    def get_long_orders(self, symbol: Optional[str] = None) -> list[LimitOrder]:
        """Get all active long orders, optionally filtered by symbol"""
        if symbol:
            return [order for order in self.active_long_orders.values() if order.symbol == symbol]
        return list(self.active_long_orders.values())
    
    def get_short_orders(self, symbol: Optional[str] = None) -> list[LimitOrder]:
        """Get all active short orders, optionally filtered by symbol"""
        if symbol:
            return [order for order in self.active_short_orders.values() if order.symbol == symbol]
        return list(self.active_short_orders.values())
    
    def get_orders_by_direction(self, direction: str, symbol: Optional[str] = None) -> list[LimitOrder]:
        """Get orders by direction"""
        if direction.lower() == Direction.LONG.value:
            return self.get_long_orders(symbol)
        elif direction.lower() == Direction.SHORT.value:
            return self.get_short_orders(symbol)
        else:
            return []
    
    def get_long_order_history(self, symbol: Optional[str] = None) -> list[LimitOrder]:
        """Get long order history, optionally filtered by symbol"""
        if symbol:
            return [order for order in self.long_order_history if order.symbol == symbol]
        return self.long_order_history.copy()
    
    def get_short_order_history(self, symbol: Optional[str] = None) -> list[LimitOrder]:
        """Get short order history, optionally filtered by symbol"""
        if symbol:
            return [order for order in self.short_order_history if order.symbol == symbol]
        return self.short_order_history.copy()
    
    def cancel_long_orders(self, symbol: str) -> int:
        """Cancel all active long orders for a symbol"""
        cancelled_count = 0
        orders_to_cancel = [order_id for order_id, order in self.active_long_orders.items() 
                           if order.symbol == symbol]
        
        for order_id in orders_to_cancel:
            if self.cancel_order(order_id):
                cancelled_count += 1
        
        return cancelled_count
    
    def cancel_short_orders(self, symbol: str) -> int:
        """Cancel all active short orders for a symbol"""
        cancelled_count = 0
        orders_to_cancel = [order_id for order_id, order in self.active_short_orders.items() 
                           if order.symbol == symbol]
        
        for order_id in orders_to_cancel:
            if self.cancel_order(order_id):
                cancelled_count += 1
        
        return cancelled_count
    
    def cancel_direction_orders(self, symbol: str, direction: str) -> int:
        """Cancel all orders for a specific direction and symbol"""
        if direction.lower() == Direction.LONG.value:
            return self.cancel_long_orders(symbol)
        elif direction.lower() == Direction.SHORT.value:
            return self.cancel_short_orders(symbol)
        else:
            return 0
    
    def get_direction_stats(self, direction: str) -> dict:
        """Get statistics for a specific direction"""
        if direction.lower() == Direction.LONG.value:
            active_orders = self.active_long_orders
            history = self.long_order_history
        elif direction.lower() == Direction.SHORT.value:
            active_orders = self.active_short_orders
            history = self.short_order_history
        else:
            return {}
        
        total_orders = len(history) + len(active_orders)
        filled_orders = len([o for o in history if o.is_filled()])
        cancelled_orders = len([o for o in history if o.status == OrderStatus.CANCELLED])
        
        return {
            'direction': direction,
            'total_orders': total_orders,
            'active_orders': len(active_orders),
            'filled_orders': filled_orders,
            'cancelled_orders': cancelled_orders,
            'fill_rate': filled_orders / total_orders if total_orders > 0 else 0.0
        }
