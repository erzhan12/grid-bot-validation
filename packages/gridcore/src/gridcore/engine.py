"""
Grid trading strategy engine.

Extracted from bbu2-master/strat.py Strat50 class with the following transformations:
- Converted from network-calling to event-driven pattern
- Returns list[Intent] instead of calling controller methods
- Made on_event() a pure function with no side effects
- Removed all exchange and database dependencies
"""

from decimal import Decimal
from typing import Optional

from gridcore.config import GridConfig
from gridcore.events import Event, TickerEvent, ExecutionEvent, OrderUpdateEvent
from gridcore.grid import Grid
from gridcore.intents import PlaceLimitIntent, CancelIntent


class GridEngine:
    """
    Pure strategy engine with NO network calls and NO side effects.

    Processes events and returns intents representing desired actions.
    The execution layer (live trading or backtest) handles actual order placement.
    """

    def __init__(self, symbol: str, tick_size: Decimal, config: GridConfig):
        """
        Initialize grid trading engine.

        Args:
            symbol: Trading pair symbol (e.g., 'BTCUSDT')
            tick_size: Minimum price increment for the symbol
            config: Grid configuration parameters
        """
        self.symbol = symbol
        self.config = config
        self.tick_size = tick_size
        self.grid = Grid(tick_size, config.greed_count, config.greed_step, config.rebalance_threshold)
        self.last_close: Optional[float] = None
        self.last_filled_price: Optional[float] = None

        # Track pending orders to avoid duplicates
        # client_order_id → order_id mapping
        self.pending_orders: dict[str, str] = {}

    def on_event(self, event: Event, limit_orders: dict[str, list[dict]] | None = None) -> list[PlaceLimitIntent | CancelIntent]:
        """
        Process event and return list of intents.

        This method updates internal engine state (grid, last_close, etc.) but has no
        external side effects (no network calls, no database access). The execution
        layer handles actual order placement based on returned intents.

        Reference: Transformed from bbu2-master/strat.py:79-99 (_check_pair_step)

        Args:
            event: Event to process (TickerEvent, ExecutionEvent, OrderUpdateEvent)
            limit_orders: Current limit orders from execution layer
                         Format: {'long': [orders...], 'short': [orders...]}

        Returns:
            List of intents (PlaceLimitIntent or CancelIntent)
        """
        intents: list[PlaceLimitIntent | CancelIntent] = []

        # Process different event types
        if isinstance(event, TickerEvent):
            intents.extend(self._handle_ticker_event(event, limit_orders or {'long': [], 'short': []}))

        elif isinstance(event, ExecutionEvent):
            intents.extend(self._handle_execution_event(event))

        elif isinstance(event, OrderUpdateEvent):
            intents.extend(self._handle_order_update_event(event))

        return intents

    def _handle_ticker_event(self, event: TickerEvent, limit_orders: dict[str, list[dict]]) -> list[PlaceLimitIntent | CancelIntent]:
        """
        Handle ticker event - update price and check grid orders.

        Reference: bbu2-master/strat.py:79-99

        Args:
            event: Ticker event with current price
            limit_orders: Current limit orders

        Returns:
            List of intents for order placement/cancellation
        """
        intents: list[PlaceLimitIntent | CancelIntent] = []

        # Update last close price
        self.last_close = float(event.last_price)

        # Build grid if empty
        if len(self.grid.greed) <= 1:
            self.grid.build_greed(self.last_close)

        # Check and place orders for both directions
        intents.extend(self._check_and_place('long', limit_orders.get('long', [])))
        intents.extend(self._check_and_place('short', limit_orders.get('short', [])))

        return intents

    def _handle_execution_event(self, event: ExecutionEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """
        Handle execution event - update grid after fill.

        Reference: bbu2-master/strat.py:196-202 (get_last_filled_price)

        Args:
            event: Execution event

        Returns:
            List of intents (typically empty, grid update is internal)
        """
        # Update last filled price
        self.last_filled_price = float(event.price)

        # Update grid based on fill
        if self.last_close is not None:
            self.grid.update_greed(self.last_filled_price, self.last_close)

        return []

    def _handle_order_update_event(self, event: OrderUpdateEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """
        Handle order update event - track order status.

        Args:
            event: Order update event

        Returns:
            List of intents (typically empty, just state tracking)
        """
        # Track order lifecycle
        if event.status in ['New', 'PartiallyFilled']:
            self.pending_orders[event.order_link_id] = event.order_id
        elif event.status in ['Filled', 'Cancelled', 'Rejected']:
            self.pending_orders.pop(event.order_link_id, None)

        return []

    def _check_and_place(self, direction: str, limits: list[dict]) -> list[PlaceLimitIntent | CancelIntent]:
        """
        Check grid and generate intents for order placement/cancellation.

        Reference: bbu2-master/strat.py:101-107

        Args:
            direction: 'long' or 'short'
            limits: Current limit orders for this direction

        Returns:
            List of intents
        """
        intents: list[PlaceLimitIntent | CancelIntent] = []

        # Too many orders → rebuild needed (return cancel intents)
        if len(limits) > len(self.grid.greed) + 10:
            # Rebuild grid if we have a valid last_close (matches original behavior)
            if self.last_close is not None:
                self.grid.build_greed(self.last_close)
            for limit in limits:
                intents.append(CancelIntent(
                    symbol=self.symbol,
                    order_id=limit['orderId'],
                    reason='rebuild',
                    price=Decimal(str(limit['price'])),
                    side=limit['side']
                ))
            return intents

        # Update grid if we have some fills
        if 0 < len(limits) < self.grid.greed_count:
            if self.last_filled_price is not None and self.last_close is not None:
                self.grid.update_greed(self.last_filled_price, self.last_close)

        # Place grid orders
        intents.extend(self._place_greed_orders(limits, direction))

        return intents

    def _get_wait_indices(self) -> int:
        """
        Get center index based on WAIT region.

        Reference: bbu2-master/strat.py:114-122

        Returns:
            Index of center of grid
        """
        wait_indices = [i for i, greed in enumerate(self.grid.greed) if greed['side'] == self.grid.WAIT]
        if wait_indices:
            # Use the middle of the WAIT region as center
            center_index = (wait_indices[0] + wait_indices[-1]) // 2
        else:
            # Fallback: use the middle of the entire list
            center_index = len(self.grid.greed) // 2 if self.grid.greed else 0
        return center_index

    def _place_greed_orders(self, limits: list[dict], direction: str) -> list[PlaceLimitIntent | CancelIntent]:
        """
        Generate intents for placing/canceling grid orders.

        Reference: bbu2-master/strat.py:124-160

        Args:
            limits: Current limit orders
            direction: 'long' or 'short'

        Returns:
            List of intents
        """
        intents: list[PlaceLimitIntent | CancelIntent] = []

        # Sort limits by price
        sorted_limits = sorted(limits, key=lambda d: float(d['price']))

        # Create price→limit mapping for O(1) lookup
        limit_prices = {float(limit['price']): limit for limit in sorted_limits}

        # Find center and create sorted grid items
        center_index = self._get_wait_indices()
        indexed_greeds = [(i, greed) for i, greed in enumerate(self.grid.greed) if greed['side'] != self.grid.WAIT]
        # Sort by distance from center (primary) then by price (secondary)
        sorted_greeds = sorted(indexed_greeds, key=lambda x: (abs(x[0] - center_index), x[1]['price']))

        # Check each grid level
        for index, greed in sorted_greeds:
            # Check if limit exists for this greed price
            limit = limit_prices.get(greed['price'])

            if limit:
                # Cancel if side mismatch
                if limit['side'] != greed['side']:
                    intents.append(CancelIntent(
                        symbol=self.symbol,
                        order_id=limit['orderId'],
                        reason='side_mismatch',
                        price=Decimal(str(limit['price'])),
                        side=limit['side']
                    ))
                    # Then place correct order
                    place_intent = self._create_place_intent(greed, direction, index)
                    if place_intent:
                        intents.append(place_intent)
            else:
                # No limit exists, place order
                place_intent = self._create_place_intent(greed, direction, index)
                if place_intent:
                    intents.append(place_intent)

        # Cancel limits outside grid range
        greed_price_set = {round(greed['price'], 8) for greed in self.grid.greed}
        for limit in sorted_limits:
            limit_price = round(float(limit['price']), 8)
            if limit_price not in greed_price_set:
                intents.append(CancelIntent(
                    symbol=self.symbol,
                    order_id=limit['orderId'],
                    reason='outside_grid',
                    price=Decimal(str(limit['price'])),
                    side=limit['side']
                ))

        return intents

    def _create_place_intent(self, greed: dict, direction: str, grid_level: int) -> Optional[PlaceLimitIntent]:
        """
        Create a PlaceLimitIntent for a grid level.

        Reference: bbu2-master/strat.py:162-182

        Args:
            greed: Grid level dict with 'side' and 'price'
            direction: 'long' or 'short'
            grid_level: Grid level index

        Returns:
            PlaceLimitIntent or None if order shouldn't be placed
        """
        if greed['side'] == self.grid.WAIT:
            return None

        if self.last_close is None:
            return None

        # Check if price is eligible
        diff_p = (self.last_close - greed['price']) / self.last_close * 100

        # Buy orders must be below market, sell orders above market
        if (greed['side'] == self.grid.BUY and diff_p <= 0) or \
           (greed['side'] == self.grid.SELL and diff_p >= 0):
            return None

        # Must be far enough from current price
        if abs(diff_p) <= self.grid.greed_step / 2:
            return None

        # Create the intent
        # Note: qty calculation would come from execution layer based on risk management
        return PlaceLimitIntent.create(
            symbol=self.symbol,
            side=greed['side'],
            price=Decimal(str(greed['price'])),
            qty=Decimal('0'),  # Qty determined by execution layer
            grid_level=grid_level,
            direction=direction,
            reduce_only=False
        )
