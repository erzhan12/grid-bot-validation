import math
from datetime import datetime, timedelta
from typing import Any, Dict

try:
    from pybit.unified_trading import HTTP
except ImportError:
    # For testing without pybit dependency
    HTTP = None

from src.backtest_order_manager import BacktestOrderManager
from src.constants import COMMISSION_RATE
from src.enums import ChannelType, Direction, MarginMode, PositionSide
from src.position import Position
from src.position_tracker import PositionTracker


class BybitApiUsdt:
    ticksizes = {}

    def __init__(self, APIKey, secret, amount, strat, name, controller):
        self.channel_type = ChannelType.LINEAR
        self.APIKey = APIKey
        self.secret = secret
        self.symbol = None
        self.amount = amount
        self.name = name
        self.controller = controller
        self.strat = strat
        # self.session = HTTP(testnet=False, api_key=APIKey, api_secret=secret) if (APIKey and HTTP) else None
        self.session = HTTP(testnet=False)
        self.ticker_data = None
        self.min_amount = 0.001

        self.POSITIONS_RATIO_CHECK_INTERVAL = 1 * 63
        self.last_checked_positions_ratio = None

        self.position = {Direction.LONG: None, Direction.SHORT: None}
        self.position_ratio = None
        self.log_counter = 999
        self.MAX_LOG_COUNTER = 4
        
        # Backtest components
        self.backtest_session = None
        self.backtest_order_manager = None
        self.current_timestamp = None

    def get_last_close(self):
        try:
            return float(self.ticker_data["lastPrice"])
        except (KeyError, TypeError):
            return None

    def init_symbol(self):
        pass
    
    def init_backtest_mode(self, backtest_session):
        """Initialize backtesting mode"""
        self.backtest_session = backtest_session
        self.backtest_order_manager = BacktestOrderManager(backtest_session)
        
        print(f"Backtest mode initialized for {self.name}")
    
    def check_and_fill_orders(self, symbol, current_price, timestamp):
        """Check for order fills and update positions"""
        self.current_timestamp = timestamp

        # Only process in backtest mode
        if self.backtest_order_manager is None:
            return

        filled_orders = self.backtest_order_manager.check_fills(
            symbol, current_price, timestamp
        )

        for order in filled_orders:
            self._process_filled_order(order, order.fill_price, timestamp)
    
    def _process_filled_order(self, order, current_price, timestamp):
        """Process a filled order and update positions"""
        direction = Direction.LONG if order.direction == 'long' else Direction.SHORT
        position = self.position[direction]
        
        # Update position based on fill
        if order.side == PositionSide.BUY:
            if direction == Direction.LONG:
                # Increase long position
                self._increase_position(position, order.size, order.fill_price)
            else:
                # Reduce short position
                self._reduce_position(position, order.size, order.fill_price)
        else:  # SELL
            if direction == Direction.SHORT:
                # Increase short position
                self._increase_position(position, order.size, order.fill_price)
            else:
                # Reduce long position
                self._reduce_position(position, order.size, order.fill_price)
        
        # Update position with current market data
        self._update_position_current_price(position, current_price, timestamp)
    
    def _log_position_change(self, position, action, size, price, previous_state, new_state):
        """
        Log detailed position change information

        Args:
            position: Position object
            action: Action performed ('increase', 'reduce', 'close')
            size: Size of the change
            price: Price of the transaction
            previous_state: Position state before change
            new_state: Position state after change
        """
        direction = getattr(position, '_Position__direction', 'unknown')
        symbol = getattr(self, 'symbol', 'unknown')

        print(f"   📊 Position {action.upper()}: {direction.value if hasattr(direction, 'value') else direction}")
        print(f"      Symbol: {symbol}")
        print(f"      Transaction: {action} {size:.6f} @ ${price:.2f}")

        if previous_state and new_state:
            size_change = new_state.get('size', 0) - previous_state.get('size', 0)
            price_change = new_state.get('avg_price', 0) - previous_state.get('avg_price', 0)

            print(f"      Before: Size={previous_state.get('size', 0):.6f}, Avg=${previous_state.get('avg_price', 0):.2f}")
            print(f"      After:  Size={new_state.get('size', 0):.6f}, Avg=${new_state.get('avg_price', 0):.2f}")
            print(f"      Change: Size={size_change:+.6f}, Avg=${price_change:+.2f}")

            # Show PnL if available
            if 'unrealized_pnl' in new_state:
                print(f"      Unrealized PnL: ${new_state['unrealized_pnl']:+.5f}")
            if 'realized_pnl' in new_state:
                print(f"      Realized PnL: ${new_state['realized_pnl']:+.5f}")
            if 'commission' in new_state:
                print(f"      Commission: ${new_state['commission']:.5f}")

            # Show total realized PnL for this direction when closing a trade
            if action == 'close' and 'realized_pnl' in new_state:
                dir_name = direction.value if hasattr(direction, 'value') else direction
                total_pnl = new_state['realized_pnl']
                print(f"      💰 Total Realized PnL ({dir_name}): ${total_pnl:+.5f}")

    def _increase_position(self, position, size, price):
        """
        Increase position size with proper average price calculation

        Args:
            position: Position object to modify
            size: Size to add to position
            price: Price of the new position entry
        """
        # Capture state before change
        previous_state = {
            'size': getattr(position, 'size', 0),
            'avg_price': getattr(position, 'entry_price', 0),
            'unrealized_pnl': 0,
            'realized_pnl': 0,
            'commission': 0
        }

        # Initialize position tracker if it doesn't exist
        if not hasattr(position, 'tracker'):
            # Convert PositionSide to Direction for PositionTracker
            position_side = position._Position__direction
            tracker_direction = Direction.LONG if position_side == PositionSide.BUY else Direction.SHORT

            position.tracker = PositionTracker(
                direction=tracker_direction,
                commission_rate=COMMISSION_RATE,
                symbol=getattr(self, 'symbol', 'BTCUSDT'),
                leverage=10,  # Default leverage - should be configurable
                margin_mode=MarginMode.CROSS
            )
        else:
            # Update previous state with tracker data
            previous_state.update({
                'size': position.tracker.state.total_size,
                'avg_price': position.tracker.state.average_entry_price,
                'realized_pnl': position.tracker.state.realized_pnl,
                'commission': position.tracker.state.commission_paid
            })

        # Add to position
        realized_pnl = position.tracker.add_position(
            size=size,
            price=price,
            timestamp=self.current_timestamp,
            order_id=f"FILL_{len(position.tracker.state.entries)}"
        )

        # Update position object attributes
        position.size = position.tracker.state.total_size
        position.entry_price = position.tracker.state.average_entry_price

        # Capture state after change
        new_state = {
            'size': position.tracker.state.total_size,
            'avg_price': position.tracker.state.average_entry_price,
            'realized_pnl': position.tracker.state.realized_pnl,
            'commission': position.tracker.state.commission_paid
        }

        # Update the corresponding trade's realized PnL (commission cost)
        self._update_trade_realized_pnl(size, price, realized_pnl)

        # Log the detailed position change
        self._log_position_change(position, 'increase', size, price, previous_state, new_state)

        return realized_pnl
    
    def _reduce_position(self, position, size, price):
        """
        Reduce position size with proper PnL realization
        
        Args:
            position: Position object to modify
            size: Size to reduce from position
            price: Price of the position exit
        """
        if not hasattr(position, 'tracker') or position.tracker.is_empty():
            print(f"   ⚠️  Cannot reduce {position._Position__direction.value} position: No position to reduce")
            return 0.0

        if size > position.tracker.state.total_size:
            print(f"   ⚠️  Cannot reduce {size}, position size is only {position.tracker.state.total_size}")
            return 0.0

        # Capture state before change
        previous_state = {
            'size': position.tracker.state.total_size,
            'avg_price': position.tracker.state.average_entry_price,
            'realized_pnl': position.tracker.state.realized_pnl,
            'commission': position.tracker.state.commission_paid
        }

        # Calculate realized PnL before reducing
        realized_pnl = position.tracker.reduce_position(
            size=size,
            price=price,
            timestamp=self.current_timestamp,
            order_id=f"CLOSE_{len(position.tracker.state.entries)}"
        )

        # Update position object attributes
        position.size = position.tracker.state.total_size
        if position.size > 0:
            position.entry_price = position.tracker.state.average_entry_price
        else:
            position.entry_price = 0.0

        # Capture state after change
        new_state = {
            'size': position.tracker.state.total_size,
            'avg_price': position.tracker.state.average_entry_price,
            'realized_pnl': position.tracker.state.realized_pnl,
            'commission': position.tracker.state.commission_paid
        }

        # Update the corresponding trade's realized PnL
        self._update_trade_realized_pnl(size, price, realized_pnl)

        # Determine action type
        action = 'close' if position.size == 0 else 'reduce'

        # Log the detailed position change
        self._log_position_change(position, action, size, price, previous_state, new_state)
        
        print(f"   📉 Reduced {position._Position__direction.value} position: "
              f"Size={position.size:.6f}, Realized PnL=${realized_pnl:.5f}")
        
        return realized_pnl
    
    def _update_position_current_price(self, position, current_price, timestamp):
        """
        Update position with current market price and calculate all metrics using BybitCalculator

        Args:
            position: Position object to update
            current_price: Current market price
            timestamp: Current timestamp
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
        liquidation_price = self._calculate_liquidation_price(position, current_price)

        # Calculate bankruptcy price
        bankruptcy_price = position.tracker.calculate_bankruptcy_price()

        # Update position tracker state
        position.tracker.state.unrealized_pnl = unrealized_pnl
        position.tracker.state.margin_used = initial_margin
        position.tracker.state.liquidation_price = liquidation_price
        position.tracker.state.bankruptcy_price = bankruptcy_price
        position.tracker.state.maintenance_margin = maintenance_margin

        # Calculate margin ratio if wallet balance is available
        if hasattr(self, 'backtest_session') and self.backtest_session:
            wallet_balance = self.backtest_session.current_balance
            margin_ratio = position.tracker.calculate_margin_ratio(current_price, wallet_balance)
            position.tracker.state.margin_ratio = margin_ratio

            # Check if position is at risk
            at_risk = position.tracker.is_position_at_risk(current_price, wallet_balance)
            if at_risk:
                print(f"⚠️  WARNING: {position.tracker.direction.value} position approaching liquidation!")
                print(f"   Margin Ratio: {margin_ratio:.4f}")
                print(f"   Liquidation Price: ${liquidation_price:,.2f}")

        # Update legacy position object if it has these methods
        if hasattr(position, 'update_unrealized_pnl'):
            position.update_unrealized_pnl(unrealized_pnl)

        # Record enhanced position snapshot for backtesting analysis
        if self.backtest_session:
            self._record_detailed_position_snapshot(position, current_price, timestamp)
    
    def _update_trade_realized_pnl(self, size, price, realized_pnl):
        """Update the most recent trade's realized PnL"""
        if not self.backtest_session:
            return
            
        # Find the corresponding trade and update its realized PnL
        for trade in reversed(self.backtest_session.trades):
            if (abs(trade.size - size) < 0.000001 and 
                abs(trade.price - price) < 0.01):
                trade.realized_pnl = realized_pnl
                break
    
    def _calculate_liquidation_price(self, position, current_price):
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
        if hasattr(self, 'backtest_session') and self.backtest_session:
            available_balance = self.backtest_session.current_balance

        return position.tracker.calculate_liquidation_price(
            available_balance=available_balance if position.tracker.margin_mode == MarginMode.CROSS else 0
        )
    
    def _record_detailed_position_snapshot(self, position, current_price, timestamp):
        """Record detailed position snapshot for analysis"""
        from src.backtest_session import BacktestPositionSnapshot
        
        if not hasattr(position, 'tracker'):
            return
            
        liquidation_price = self._calculate_liquidation_price(position, current_price)
        unrealized_pnl = position.tracker.calculate_unrealized_pnl(current_price)
        
        snapshot = BacktestPositionSnapshot(
            timestamp=timestamp,
            symbol=self.symbol,
            direction=position._Position__direction.value,
            size=position.size,
            entry_price=position.entry_price,
            current_price=current_price,
            unrealized_pnl=unrealized_pnl,
            margin=position.tracker.state.margin_used,
            liquidation_price=liquidation_price
        )
        
        self.backtest_session.record_position_snapshot(snapshot)

    def apply_funding_payments(self, current_price: float, funding_rate: float, timestamp: datetime):
        """
        Apply funding payments to all open positions

        Args:
            current_price: Current market price
            funding_rate: Current funding rate (e.g., 0.0001 = 0.01%)
            timestamp: Timestamp of funding application
        """
        total_funding_paid = 0.0

        for direction in [Direction.LONG, Direction.SHORT]:
            position = self.position[direction]
            if hasattr(position, 'tracker') and not position.tracker.is_empty():
                funding_payment = position.tracker.apply_funding_payment(
                    funding_rate=funding_rate,
                    current_price=current_price,
                    timestamp=timestamp
                )

                total_funding_paid += funding_payment

                if abs(funding_payment) > 0.01:  # Only log significant funding payments
                    print("💰 Funding Payment Applied:")
                    print(f"   Direction: {direction.value}")
                    print(f"   Rate: {funding_rate:.6f} ({funding_rate * 100:.4f}%)")
                    print(f"   Payment: ${funding_payment:+.4f}")

        # Update backtest session with funding costs
        if self.backtest_session and abs(total_funding_paid) > 0.01:
            self.backtest_session.total_funding_paid = getattr(
                self.backtest_session, 'total_funding_paid', 0.0
            ) + total_funding_paid

        return total_funding_paid

    def get_position_risk_summary(self, current_price: float) -> Dict[str, Any]:
        """
        Get comprehensive risk summary for all positions

        Args:
            current_price: Current market price

        Returns:
            Dictionary with risk metrics for all positions
        """
        if not hasattr(self, 'backtest_session') or not self.backtest_session:
            return {}

        wallet_balance = self.backtest_session.current_balance
        risk_summary = {
            'timestamp': datetime.now().isoformat(),
            'current_price': current_price,
            'wallet_balance': wallet_balance,
            'positions': {},
            'total_risk_level': 'low'
        }

        total_position_value = 0.0
        any_at_risk = False

        for direction in [Direction.LONG, Direction.SHORT]:
            position = self.position[direction]
            if hasattr(position, 'tracker') and not position.tracker.is_empty():
                # Get comprehensive position summary
                summary = position.tracker.get_comprehensive_summary(
                    current_price=current_price,
                    wallet_balance=wallet_balance
                )

                position_value = summary.get('position_value', 0)
                total_position_value += position_value

                at_risk = summary.get('at_risk', False)
                if at_risk:
                    any_at_risk = True

                risk_summary['positions'][direction.value] = {
                    'size': summary.get('size', 0),
                    'position_value': position_value,
                    'unrealized_pnl': summary.get('unrealized_pnl', 0),
                    'liquidation_price': summary.get('liquidation_price', 0),
                    'margin_ratio': summary.get('margin_ratio', 0),
                    'at_risk': at_risk,
                    'roe_percentage': summary.get('roe_percentage', 0)
                }

        # Calculate overall risk level
        if any_at_risk:
            risk_summary['total_risk_level'] = 'high'
        elif total_position_value > wallet_balance * 0.8:  # High utilization
            risk_summary['total_risk_level'] = 'medium'

        risk_summary['total_position_value'] = total_position_value
        risk_summary['position_utilization'] = (total_position_value / wallet_balance) if wallet_balance > 0 else 0

        return risk_summary

    def init_positions(self, strat):
        self.position = {
            Direction.LONG: Position(PositionSide.BUY, strat), 
            Direction.SHORT: Position(PositionSide.SELL, strat)
        }
        self.position[Direction.LONG].set_opposite(self.position[Direction.SHORT])
        self.position[Direction.SHORT].set_opposite(self.position[Direction.LONG])
        # Set API context for positions to access min_amount and base_amount calculations
        self.position[Direction.LONG].set_api_context(self)
        self.position[Direction.SHORT].set_api_context(self)

    def read_ticksize(self, symbol):
        if self.session is None:
            # Fallback for backtest mode
            BybitApiUsdt.ticksizes[symbol] = 0.01  # Default ticksize
            self.min_amount = 0.001  # Default min amount
            return
            
        r = self.session.get_instruments_info(category=self.channel_type, symbol=symbol)
        symbols = r['result']['list']
        symbol_info = next((x for x in symbols if x['symbol'] == symbol), None)
        if symbol_info is None:
            raise ValueError(f"Symbol '{symbol}' not found")
        BybitApiUsdt.ticksizes[symbol] = float(symbol_info['priceFilter']['tickSize'])
        self.min_amount = float(symbol_info['lotSizeFilter']['qtyStep'])

    @staticmethod
    def read_ticksize_static(symbol):
        if HTTP is None:
            # Fallback for testing/backtest mode without API
            BybitApiUsdt.ticksizes[symbol] = 0.01  # Default ticksize
            return
            
        r = HTTP(testnet=False, api_key=None, api_secret=None).get_instruments_info(
            category=ChannelType.LINEAR, symbol=symbol)
        symbols = r['result']['list']
        symbol_info = next((x for x in symbols if x['symbol'] == symbol), None)
        if symbol_info is None:
            raise ValueError(f"Symbol '{symbol}' not found")
        BybitApiUsdt.ticksizes[symbol] = float(symbol_info['priceFilter']['tickSize'])

    @staticmethod
    def round_price(symbol, price, ticksize=None):
        if ticksize is None:
            if symbol not in BybitApiUsdt.ticksizes:
                BybitApiUsdt.read_ticksize_static(symbol)
            ticksize = BybitApiUsdt.ticksizes[symbol]
        l_price = round(price / ticksize) * ticksize
        l_price = float('{:.10f}'.format(l_price))  # to avoid 4.76999999e-6 != 4.77
        return l_price

    def check_positions_ratio(self, symbol, datetime_now, last_close):
        if (self.last_checked_positions_ratio is None or
                datetime_now - self.last_checked_positions_ratio > timedelta(
                    seconds=self.POSITIONS_RATIO_CHECK_INTERVAL)):
            self.last_checked_positions_ratio = datetime_now

            try:
                self.reset_amount_multiplier()
                # self.get_position_status(symbol, Direction.LONG)
                # self.get_position_status(symbol, Direction.SHORT)
                # Calculate position ratio safely
                short_size = self.position[Direction.SHORT].size
                if short_size != 0:
                    self.position_ratio = self.position[Direction.LONG].size / short_size
                else:
                    self.position_ratio = None
                self.update_position_ratio()
                # last_close = self.get_last_close()

                if self.log_counter > self.MAX_LOG_COUNTER:
                    ratio_str = f"{self.position_ratio:.2f}" if self.position_ratio is not None else "N/A"
                    print(f'Position ratio: {symbol} {ratio_str}')
                    # self.position[Direction.LONG].log_position(symbol, last_close)
                    # self.position[Direction.SHORT].log_position(symbol, last_close)
                    self.log_counter = 0
                else:
                    self.log_counter += 1

            except TypeError as e:
                # pass
                print(e)
            except ZeroDivisionError:
                pass

    def reset_amount_multiplier(self):
        self.position[Direction.LONG].reset_amount_multiplier()
        self.position[Direction.SHORT].reset_amount_multiplier()

    def get_position_status(self, symbol, direction=Direction.LONG):
        pass

    def update_position_ratio(self):
        self.position[Direction.LONG].position_ratio = self.position_ratio
        self.position[Direction.SHORT].position_ratio = self.position_ratio

    def new_limit_order(self, side, symbol, price, bm_name, direction, amount=None, reduce_only=None):
        """Create a new limit order (supports both live and backtest modes)"""
        order_id = ''
        l_price = BybitApiUsdt.round_price(symbol, price)
        
        if amount is None:
            l_amount = self.__get_amount(symbol, l_price, side=side, bm_name=bm_name)
        else:
            l_amount = amount
            
        l_amount_multiplier = self.__get_amount_multiplier(symbol, side, l_price, direction)
        try:
            if 1.1 < self.position_ratio < 10 and \
                    self.position[Direction.LONG].liq_price == 0.0 and self.position[Direction.SHORT].liq_price == 0.0:
                l_amount_multiplier *= self.strat.long_koef
        except (TypeError, AttributeError):
            pass

        l_amount = self.round_amount(l_amount * l_amount_multiplier)

        # Determine if this should be reduce-only if not explicitly specified
        if reduce_only is None:
            reduce_only = self._get_reduce_only(direction, side)

        # Create order through BacktestOrderManager
        order_side = PositionSide.BUY if side == 'Buy' else PositionSide.SELL
        
        if self._is_good_to_place(symbol, l_price, l_amount, side, direction, reduce_only):
            order = self.backtest_order_manager.create_order(
                symbol=symbol,
                side=order_side,
                limit_price=l_price,
                size=l_amount,
                direction=direction,
                strategy_id=self.strat,
                bm_name=bm_name,
                timestamp=self.current_timestamp,
                reduce_only=reduce_only
            )
            return l_price, order.order_id

        return l_price, order_id
    
    def round_amount(self, amount):
        """Round amount to appropriate precision (placeholder)"""
        l_amount = math.ceil(amount / self.min_amount) * self.min_amount
        return float('{:.10f}'.format(l_amount))
    
    def __get_amount(self, symbol, price, side, bm_name):
        """Get order amount (placeholder)"""
        # This should return the calculated order amount
        min_amount_usdt = 5
        amount = 0.0
        try:
            amount = float(self.amount)
        except ValueError:
            if self.amount[0] == 'x':
                wallet_amount = self.get_wallet_amount()

                try:
                    mult = float(self.amount[1:])
                    amount = self.round_amount(wallet_amount / price * mult)
                except ValueError:
                    pass
        min_amount = min_amount_usdt / price
        if amount < min_amount:
            amount = self.round_amount(min_amount)
        return max(amount, self.min_amount)
    
    def get_wallet_amount(self):
        """Get current wallet balance"""
        if self.backtest_session:
            return self.backtest_session.current_balance
        else:
            # Fallback value if session not initialized
            return 10000.0
    
    def __get_amount_multiplier(self, symbol, side, price, direction):
        """Get amount multiplier (placeholder)"""
        # This should return the position-based multiplier
        try:
            direction_obj = Direction.LONG if direction == 'long' else Direction.SHORT
            if self.position[direction_obj]:
                multiplier = self.position[direction_obj].get_amount_multiplier()
                side_key = PositionSide.BUY if side == 'Buy' else PositionSide.SELL
                return multiplier.get(side_key, 1.0)
        except (KeyError, AttributeError):
            pass
        return 1.0
    
    def _get_reduce_only(self, direction, side):
        """
        Determine if an order should be reduce-only based on direction and side.

        Args:
            direction: Position direction ('long' or 'short')
            side: Order side ('Buy' or 'Sell')

        Returns:
            True if this should be a reduce-only order, False otherwise
        """
        mapping = {
            'long': {
                'Buy': False,
                'Sell': True
            },
            'short': {
                'Buy': True,
                'Sell': False,
            }
        }
        return mapping[direction][side]
    
    def _get_position_idx(self, direction):
        """Get position index (placeholder for live mode)"""
        return 0
    
    def _is_good_to_place(self, symbol, price, amount, side, direction, reduce_only):
        """
        Check if order is good to place by validating against existing orders and position sizes.

        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT')
            price: Order price
            amount: Order amount
            side: Order side ('Buy' or 'Sell')
            direction: Position direction ('long' or 'short')
            reduce_only: Whether this is a reduce-only order

        Returns:
            True if order is good to place, False otherwise
        """
        # Get existing limit orders for this direction and symbol
        limits = self.controller.get_limit_orders(self.strat, symbol, direction)

        # Check for duplicate orders (same price, amount, side, reduce_only)
        for limit in limits:
            if (abs(limit.limit_price - price) < 0.01 and  # Allow for small price differences
                abs(limit.size - amount) < 0.000001 and  # Allow for floating point precision
                limit.side.value == side and
                limit.reduce_only == reduce_only):
                return False

        # Direction mapping for order types
        direction_mapping = {
            'long': {'open': 'Buy', 'close': 'Sell'},
            'short': {'open': 'Sell', 'close': 'Buy'}
        }

        # If this is an opening order (position increase), always allow
        if side == direction_mapping[direction]['open']:
            return True

        # For closing orders (reduce-only), check if we have enough position to reduce
        if side == direction_mapping[direction]['close']:
            direction_enum = Direction.LONG if direction == 'long' else Direction.SHORT
            position_size = self.position[direction_enum].size

            # Calculate total pending close orders
            limits_qty = amount
            for limit in limits:
                if (limit.reduce_only and
                    limit.side.value == direction_mapping[direction]['close']):
                    limits_qty += limit.size

            # Ensure we don't try to close more than we have
            return position_size >= limits_qty

        # Default to allowing the order
        return True

    def _place_active_order(self, symbol, side, amount, price, reduce_only, position_idx):
        pass
