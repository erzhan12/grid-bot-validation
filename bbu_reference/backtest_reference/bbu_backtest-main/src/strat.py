import traceback

from src.backtest_session import BacktestPositionSnapshot
from src.bybit_api_usdt import BybitApiUsdt
from src.data_provider import DataProvider
from src.enums import Direction, MarginMode
from src.greed import Greed


class Strat:
    def __init__(self, controller):
        self.controller = controller
        self.bms = []


class Strat1(Strat):
    def __init__(self, controller, id, strat, symbol, greed_step, greed_count, exchange, max_margin,
                 min_liq_ratio, max_liq_ratio, min_total_margin, long_koef):
        super().__init__(controller)
        self.__last_checked_bms_hour = 25
        self._position_amount = 0.0
        self.open_price = 0.0
        self._symbol = symbol
        self.strat_name = strat
        self._exchange = exchange
        self.id = id
        self._greed_step = greed_step
        self._greed_count = greed_count
        self.max_margin = max_margin
        self.greed = Greed(self, symbol, greed_count, greed_step, exchange) 
        self.to_init = True
        self.last_filled_price = None
        self.last_close = None
        self.liq_ratio = {'min': min_liq_ratio, 'max': max_liq_ratio}
        self.long_koef = long_koef
        self.min_total_margin = min_total_margin

    def init_positions(self):
        for bm in self.bms:
            bm.init_positions(self)

    def init_symbol(self, symbol=None):
        l_symbol = symbol or self._symbol
        for bm in self.bms:
            bm.symbol = l_symbol
            bm.read_ticksize(l_symbol)

    def _get_ticksize(self, symbol):
        ticksize = BybitApiUsdt.ticksizes[symbol]
        multiplier = 1
        return ticksize, multiplier

    def check_pair(self):
        try:
            self._check_pair_step(self._symbol)
        except Exception as e:
            print(e)
            print(traceback.format_exc())

    def _check_pair_step(self, _symbol):
        pass


class Strat50(Strat1):
    def __init__(self, *args, **kwargs):
        start_datetime = kwargs.pop('start_datetime', None)
        super().__init__(*args, **kwargs)
        self.data_provider = DataProvider(start_datetime=start_datetime)  # Get one record at a time
        self.current_start_id = 1  # Start from ID 1
    
    @classmethod
    def create_for_testing(cls, symbol="BTCUSDT", controller=None):
        """Helper method to create a Strat50 instance for testing with default parameters"""
        if controller is None:
            from unittest.mock import Mock
            controller = Mock()
            controller.get_same_orders_error.return_value = False
            controller.get_limit_orders.return_value = []
            controller.check_positions_ratio.return_value = None
        
        return cls(
            controller=controller,
            id=1,
            strat="test_strategy",
            symbol=symbol,
            greed_step=0.2,
            greed_count=50,
            exchange="bybit_usdt",
            max_margin=5,
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            min_total_margin=0,
            long_koef=1.0
        )

    def _check_pair_step(self, symbol):
        """Modified to loop over database table and get one last_close at each step"""

        # Loop over database table and process one last_close at a time
        for ticker_data in self.data_provider.iterate_all(symbol, self.current_start_id):
            # Update the current start_id for next iteration
            self.current_start_id = ticker_data['id'] + 1
            # Print progress on single line (overwrites previous line)
            print(f"\r{ticker_data['id']:>10} {ticker_data['symbol']:<10} {ticker_data['timestamp']} {ticker_data['last_price']:>12.2f}",
                  end='', flush=True)
            # Extract last_close from the ticker data
            last_close = float(ticker_data['last_price'])

            # Process this single last_close value
            self._process_single_last_close(last_close, ticker_data['timestamp'])

        # Print newline after loop to preserve the final line
        print()

    def has_more_data(self):
        """Check if there's more data available for processing"""
        return self.data_provider.has_more_data(self._symbol)
            
    def reset_database_iteration(self, start_id=1):
        """Reset the database iteration to start from a specific ID"""
        self.current_start_id = start_id
        self.data_provider.last_id = None  # Reset the provider's internal state
    
    def get_current_database_position(self):
        """Get the current position in the database iteration"""
        return {
            'current_start_id': self.current_start_id,
            'provider_last_id': self.data_provider.last_id
        }

    def _process_single_last_close(self, last_close, timestamp):
        """Enhanced processing with full order and position management"""
        # Store the current last_close for other methods to use
        self.last_close = last_close
        
        # Set current timestamp for all market makers
        for bm in self.bms:
            if self.id == bm.strat:
                bm.current_timestamp = timestamp
        
        # Check for order fills first (CRITICAL: before position updates)
        self._check_order_fills(last_close, timestamp)
        
        # Update positions with current price
        self._update_positions(last_close, timestamp)
        
        # Build greed if needed
        while len(self.greed.greed) <= 1:
            self.greed.build_greed(last_close)

        # Check positions ratio
        self.check_positions_ratio(timestamp)

        # Check and place orders
        self._check_and_place(Direction.LONG)
        self._check_and_place(Direction.SHORT)
        
        # Record position snapshots for metrics (if in backtest mode)
        self._record_position_snapshots(timestamp)
    
    def _check_and_place(self, direction):
        limits = self.controller.get_limit_orders(self.id, self._symbol, direction)
        if len(limits) > len(self.greed.greed) + 10:
            self._rebuild_greed(self._symbol)
        if len(limits) > 0 and len(limits) < self.greed.greed_count:
            self.greed.update_greed(self.get_last_filled_price(), self.get_last_close())
        self.place_greed_orders(limits, direction)
    
    def _rebuild_greed(self, symbol):
        self._cancel_limits(symbol)
        self.greed.rebuild_greed(self.get_last_close())

    def place_greed_orders(self, i_limits, direction):
        # i_limits is now a list of LimitOrder objects, not dictionaries
        limits = sorted(i_limits, key=lambda order: order.limit_price)
        # Use a dictionary for O(1) lookups instead of nested loops
        limit_prices = {limit.limit_price: limit for limit in limits}

        # Find center of WAIT region for distance-based sorting
        wait_indices = [i for i, g in enumerate(self.greed.greed) if g['side'] == self.greed.WAIT]
        if wait_indices:
            # Use the middle of the WAIT region as center
            center_index = (wait_indices[0] + wait_indices[-1]) // 2
        else:
            # Fallback: use the middle of the entire list
            center_index = len(self.greed.greed) // 2 if self.greed.greed else 0

        # Create list of (index, greed) pairs, excluding WAIT items
        indexed_greeds = [
            (i, greed)
            for i, greed in enumerate(self.greed.greed)
            if greed['side'] != self.greed.WAIT
        ]

        # Sort by distance from center (primary), then by price (secondary)
        sorted_greeds = sorted(
            indexed_greeds,
            key=lambda x: (abs(x[0] - center_index), x[1]['price'])
        )

        # Iterate over greeds sorted by distance from center
        for index, greed in sorted_greeds:
            # If there are no limits, place the order directly
            if not limits:
                self.place_order(greed, direction)
                continue

            limit = limit_prices.get(greed['price'])
            if limit:
                # If the limit exists and the sides are different, cancel the limit and place the order
                if limit.side.value != greed['side']:
                    self.cancel_order(limit.order_id)
                    self.place_order(greed, direction)
            else:
                # Didn't find limit for this greed price, place the order
                self.place_order(greed, direction)

        # Cancel limits if price is outside greed - use set for O(1) lookups
        greed_prices = {greed['price'] for greed in self.greed.greed}
        for limit in limits:
            if limit.limit_price not in greed_prices:
                self.cancel_order(limit.order_id)

    def place_order(self, greed, direction):
        last_close = self.get_last_close()
        if greed['side'] == self.greed.WAIT:
            return
        # Check if price is eligible (lower than last close if buy)
        diff_p = (last_close - greed['price']) / last_close * 100
        if (greed['side'] == self.greed.BUY and diff_p <= 0) or \
           (greed['side'] == self.greed.SELL and diff_p >= 0):
            return

        if abs(diff_p) <= self.greed.greed_step / 2:  # if last_close is too close to greed price
            return

        price, order_id = self.controller.new_order(self.id, greed['side'], self._symbol, greed['price'], direction)
        if price:
            greed['order_id'] = order_id
        return price

    def check_positions_ratio(self, timestamp):
        self.controller.check_positions_ratio(self.id, self._symbol, timestamp, self.last_close)

    def cancel_order(self, order_id):
        self.controller.cancel_order(self.id, self._symbol, order_id)

    def get_last_close(self):
        """Override to use the current last_close from database iteration"""
        # Return the last_close that was set during database iteration
        return self.last_close

    def get_last_filled_price(self):
        order = self.controller.get_last_filled_order(self.id, self._symbol)
        try:
            self.last_filled_price = float(order['execPrice'])
        except Exception:
            pass
        return self.last_filled_price
    
    def _check_order_fills(self, current_price, timestamp):
        """Check and process order fills"""
        for bm in self.bms:
            if self.id == bm.strat:
                bm.check_and_fill_orders(self._symbol, current_price, timestamp)

    def _update_positions(self, current_price, timestamp):
        """Update position values and PnL"""
        for bm in self.bms:
            if self.id == bm.strat:
                # Update current price for both positions
                long_pos = bm.position[Direction.LONG]
                short_pos = bm.position[Direction.SHORT]
                
                # Update unrealized PnL (simplified for now)
                if not long_pos.is_empty():
                    self._update_position_unrealized_pnl(long_pos, current_price)
                if not short_pos.is_empty():
                    self._update_position_unrealized_pnl(short_pos, current_price)

    def _update_position_unrealized_pnl(self, position, current_price):
        """
        Update position with current market price and calculate all metrics using PositionTracker
        
        Args:
            position: Position object to update
            current_price: Current market price
        """
        if not hasattr(position, 'tracker') or position.tracker.is_empty():
            return
        
        # Calculate unrealized PnL using tracker
        unrealized_pnl = position.tracker.calculate_unrealized_pnl(current_price)
        
        # Calculate position value using BybitCalculator
        position_value = position.tracker.calculator.calculate_position_value(position.size, current_price)
        
        # Calculate initial margin using BybitCalculator
        initial_margin = position.tracker.calculator.calculate_initial_margin(
            position_value, position.tracker.leverage
        )
        
        # Calculate maintenance margin using tiered system
        maintenance_margin = position.tracker.calculate_maintenance_margin(current_price)
        
        # Calculate accurate liquidation price
        liquidation_price = self._calculate_position_liquidation_price(position, current_price)
        
        # Calculate bankruptcy price
        bankruptcy_price = position.tracker.calculate_bankruptcy_price()
        
        # Update position tracker state
        position.tracker.state.unrealized_pnl = unrealized_pnl
        position.tracker.state.margin_used = initial_margin
        position.tracker.state.liquidation_price = liquidation_price
        position.tracker.state.bankruptcy_price = bankruptcy_price
        position.tracker.state.maintenance_margin = maintenance_margin
        
        # Calculate margin ratio if wallet balance is available
        for bm in self.bms:
            if self.id == bm.strat and hasattr(bm, 'backtest_session') and bm.backtest_session:
                wallet_balance = bm.backtest_session.current_balance
                margin_ratio = position.tracker.calculate_margin_ratio(current_price, wallet_balance)
                position.tracker.state.margin_ratio = margin_ratio
                
                # Check if position is at risk
                at_risk = position.tracker.is_position_at_risk(current_price, wallet_balance)
                if at_risk:
                    print(f"⚠️  WARNING: {position.tracker.direction.value} position approaching liquidation!")
                    print(f"   Margin Ratio: {margin_ratio:.4f}")
                    print(f"   Liquidation Price: ${liquidation_price:,.2f}")
                break
    
    def _calculate_position_liquidation_price(self, position, current_price):
        """
        Calculate liquidation price using accurate Bybit formula via BybitCalculator
        
        Args:
            position: Position object
            current_price: Current market price
            
        Returns:
            Liquidation price
        """
        if not hasattr(position, 'tracker') or position.tracker.is_empty():
            return 0.0
        
        # Use enhanced PositionTracker with accurate calculations
        available_balance = 0  # For isolated margin mode
        for bm in self.bms:
            if self.id == bm.strat and hasattr(bm, 'backtest_session') and bm.backtest_session:
                available_balance = bm.backtest_session.current_balance
                break
        
        return position.tracker.calculate_liquidation_price(
            available_balance=available_balance if position.tracker.margin_mode == MarginMode.CROSS else 0
        )

    def _record_position_snapshots(self, timestamp):
        """Record current position state for analysis"""
        for bm in self.bms:
            if self.id == bm.strat and hasattr(bm, 'backtest_session') and bm.backtest_session:
                # Record long position snapshot
                long_pos = bm.position[Direction.LONG]
                if not long_pos.is_empty():
                    snapshot = BacktestPositionSnapshot(
                        timestamp=timestamp,
                        symbol=self._symbol,
                        direction='long',
                        size=long_pos.size,
                        entry_price=long_pos.entry_price,
                        current_price=self.last_close,
                        unrealized_pnl=self._calculate_unrealized_pnl(long_pos, self.last_close),
                        margin=long_pos.get_margin(),
                        liquidation_price=long_pos.liq_price
                    )
                    bm.backtest_session.record_position_snapshot(snapshot)
                
                # Record short position snapshot
                short_pos = bm.position[Direction.SHORT]
                if not short_pos.is_empty():
                    snapshot = BacktestPositionSnapshot(
                        timestamp=timestamp,
                        symbol=self._symbol,
                        direction='short',
                        size=short_pos.size,
                        entry_price=short_pos.entry_price,
                        current_price=self.last_close,
                        unrealized_pnl=self._calculate_unrealized_pnl(short_pos, self.last_close),
                        margin=short_pos.get_margin(),
                        liquidation_price=short_pos.liq_price
                    )
                    bm.backtest_session.record_position_snapshot(snapshot)
    
    def _calculate_unrealized_pnl(self, position, current_price):
        """
        Calculate unrealized PnL for a position using PositionTracker
        
        Args:
            position: Position object
            current_price: Current market price
            
        Returns:
            Unrealized PnL
        """
        # Use PositionTracker if available for accurate calculations
        if hasattr(position, 'tracker') and not position.tracker.is_empty():
            return position.tracker.calculate_unrealized_pnl(current_price)
        
        # Fallback to basic calculation if tracker not initialized
        if position.is_empty() or position.size == 0:
            return 0.0
        
        try:
            # Basic PnL calculation
            if hasattr(position, '_Position__direction'):
                direction = position._Position__direction
                if direction == Direction.LONG:
                    return (current_price - position.entry_price) * position.size
                else:  # SHORT
                    return (position.entry_price - current_price) * position.size
        except (AttributeError, TypeError):
            pass
        
        return 0.0
    
    def _cancel_limits(self, symbol):
        """Cancel all limit orders for the symbol (placeholder)"""
        # This method needs to be implemented based on your existing logic
        # For now, just a placeholder
        pass