"""Simulated order book for backtest.

Manages limit orders in simulation, checks for fills, and generates execution events.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from gridcore import ExecutionEvent, EventType

from backtest.fill_simulator import TradeThroughFillSimulator


@dataclass
class SimulatedOrder:
    """Order in the simulated order book."""

    order_id: str
    client_order_id: str
    symbol: str
    side: str  # 'Buy' or 'Sell'
    price: Decimal
    qty: Decimal
    direction: str  # 'long' or 'short'
    grid_level: int
    status: str = "pending"  # 'pending', 'filled', 'cancelled'
    created_ts: datetime = field(default_factory=datetime.now)
    filled_ts: Optional[datetime] = None
    reduce_only: bool = False


class BacktestOrderManager:
    """Manages simulated orders during backtest.

    Tracks active and filled orders, checks for fills on each tick,
    and generates ExecutionEvent when orders fill.
    """

    def __init__(
        self,
        fill_simulator: TradeThroughFillSimulator,
        commission_rate: Decimal = Decimal("0.0002"),
    ):
        """Initialize order manager.

        Args:
            fill_simulator: Fill logic implementation
            commission_rate: Commission rate for fee calculation
        """
        self.fill_simulator = fill_simulator
        self.commission_rate = commission_rate

        # Order tracking
        self.active_orders: dict[str, SimulatedOrder] = {}  # order_id -> order
        self.filled_orders: list[SimulatedOrder] = []
        self.cancelled_orders: list[SimulatedOrder] = []

        # Client order ID tracking for deduplication
        self._client_order_ids: set[str] = set()

        # Order ID counter for generating unique IDs
        self._order_counter = 0

    def place_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        direction: str,
        grid_level: int,
        timestamp: datetime,
        reduce_only: bool = False,
    ) -> Optional[SimulatedOrder]:
        """Place order in simulated order book.

        Args:
            client_order_id: Client-provided order ID for deduplication
            symbol: Trading symbol
            side: 'Buy' or 'Sell'
            price: Limit price
            qty: Order quantity
            direction: 'long' or 'short'
            grid_level: Grid level index
            timestamp: Order creation time
            reduce_only: Whether this is a reduce-only order

        Returns:
            Created order, or None if duplicate client_order_id
        """
        # Check for duplicate (deduplication by client_order_id)
        if client_order_id in self._client_order_ids:
            return None

        self._order_counter += 1
        order_id = f"sim_{self._order_counter:08d}"

        order = SimulatedOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            direction=direction,
            grid_level=grid_level,
            status="pending",
            created_ts=timestamp,
            reduce_only=reduce_only,
        )

        self.active_orders[order_id] = order
        self._client_order_ids.add(client_order_id)
        return order

    def cancel_order(self, order_id: str, timestamp: datetime) -> bool:
        """Cancel order from simulated order book.

        Args:
            order_id: Order ID to cancel
            timestamp: Cancellation time

        Returns:
            True if order was cancelled, False if not found
        """
        if order_id not in self.active_orders:
            return False

        order = self.active_orders.pop(order_id)
        order.status = "cancelled"
        self.cancelled_orders.append(order)
        # Allow client_order_id to be reused
        self._client_order_ids.discard(order.client_order_id)
        return True

    def cancel_by_client_order_id(self, client_order_id: str, timestamp: datetime) -> bool:
        """Cancel order by client order ID.

        Args:
            client_order_id: Client order ID to cancel
            timestamp: Cancellation time

        Returns:
            True if order was cancelled, False if not found
        """
        for order_id, order in list(self.active_orders.items()):
            if order.client_order_id == client_order_id:
                return self.cancel_order(order_id, timestamp)
        return False

    def check_fills(
        self,
        current_price: Decimal,
        timestamp: datetime,
        symbol: Optional[str] = None,
    ) -> list[ExecutionEvent]:
        """Check all active orders for fills.

        Args:
            current_price: Current market price
            timestamp: Current timestamp
            symbol: Optional symbol filter

        Returns:
            List of ExecutionEvent for filled orders
        """
        fills: list[ExecutionEvent] = []

        for order_id, order in list(self.active_orders.items()):
            # Skip if symbol filter specified and doesn't match
            if symbol is not None and order.symbol != symbol:
                continue

            fill_result = self.fill_simulator.check_fill(order, current_price)

            if fill_result.should_fill:
                # Move from active to filled
                self.active_orders.pop(order_id)
                order.status = "filled"
                order.filled_ts = timestamp
                self.filled_orders.append(order)
                # Allow client_order_id to be reused
                self._client_order_ids.discard(order.client_order_id)

                # Calculate commission
                fee = order.qty * fill_result.fill_price * self.commission_rate

                # Create ExecutionEvent
                exec_event = ExecutionEvent(
                    event_type=EventType.EXECUTION,
                    symbol=order.symbol,
                    exchange_ts=timestamp,
                    local_ts=timestamp,
                    exec_id=f"exec_{uuid.uuid4().hex[:8]}",
                    order_id=order.order_id,
                    order_link_id=order.client_order_id,
                    side=order.side,
                    price=fill_result.fill_price,
                    qty=order.qty,
                    fee=fee,
                    closed_pnl=Decimal("0"),  # Will be calculated by position tracker
                    closed_size=Decimal("0"),  # Not used in backtest (live bot uses for same-order detection)
                    leaves_qty=Decimal("0"),  # Fully filled
                )
                fills.append(exec_event)

        return fills

    def get_limit_orders(self) -> dict[str, list[dict]]:
        """Get orders in format expected by GridEngine.

        Returns dict with 'long' and 'short' keys, each containing
        list of order dicts with price, qty, side, orderId, orderLinkId.

        Note: Uses camelCase keys to match GridEngine expectations
        (matching Bybit API response format).
        """
        result: dict[str, list[dict]] = {"long": [], "short": []}

        for order in self.active_orders.values():
            order_dict = {
                "price": str(order.price),  # String like Bybit API
                "qty": str(order.qty),
                "side": order.side,
                "orderId": order.order_id,  # camelCase for GridEngine
                "orderLinkId": order.client_order_id,
            }
            result[order.direction].append(order_dict)

        return result

    def get_order_by_id(self, order_id: str) -> Optional[SimulatedOrder]:
        """Get active order by ID."""
        return self.active_orders.get(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> Optional[SimulatedOrder]:
        """Get order by client order ID (searches active and filled orders)."""
        for order in self.active_orders.values():
            if order.client_order_id == client_order_id:
                return order
        # Also check filled orders (for recently filled)
        for order in self.filled_orders:
            if order.client_order_id == client_order_id:
                return order
        return None

    @property
    def total_active_orders(self) -> int:
        """Get count of active orders."""
        return len(self.active_orders)

    @property
    def total_filled_orders(self) -> int:
        """Get count of filled orders."""
        return len(self.filled_orders)
