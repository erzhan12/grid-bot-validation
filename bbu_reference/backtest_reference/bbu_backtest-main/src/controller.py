import importlib
import traceback

from config.settings import settings
from src.bybit_api_usdt import BybitApiUsdt
from src.strat import Strat50


class Controller:
    def __init__(self, symbol, start_datetime=None):
        self.symbol = symbol
        self.start_datetime = start_datetime
        self.pair_timeframe = next((pt for pt in settings.pair_timeframes if pt.symbol == symbol), None)
        if self.pair_timeframe is None:
            raise ValueError(f"Pair timeframe for symbol {symbol} not found in settings")
        self.bms = self.__init_bms()
        self.strats = self.__init_strats()

    def __init_bms(self):
        bms = []
        for am in settings.amounts:
            name = am.name
            amount = am.amount
            strat = am.strat
            if amount is not None:
                bm = BybitApiUsdt(
                    APIKey=None,
                    secret=None,
                    amount=amount,
                    strat=strat,
                    name=name,
                    controller=self)
                bms.append(bm)
        return bms

    def __init_strats(self):
        strats = []
        strat_module = importlib.import_module('src.strat')
        pt = self.pair_timeframe
        try:
            strat_class = getattr(strat_module, pt.strat)
        except AttributeError:
            raise ValueError(f"Strat class {pt.strat} not found in src.strat")

        # Pass start_datetime to strategy if it's Strat50
        strat_kwargs = dict(pt)
        if pt.strat == 'Strat50' and self.start_datetime:
            strat_kwargs['start_datetime'] = self.start_datetime

        strat = strat_class(self, **strat_kwargs)

        for bm in self.bms:
            if pt.id == bm.strat:
                strat.bms.append(bm)
                strat.init_symbol()
                strat.init_positions()

        if strat.bms:
            strats.append(strat)
        return strats
    
    def check_job(self):  # noqa: C901
        """Main backtest loop with data exhaustion detection"""
        iterations = 0
        print(f"🔄 Starting backtest for {self.symbol}")

        # Print data range info for each strategy
        for strat in self.strats:
            if hasattr(strat, 'data_provider'):
                data_info = strat.data_provider.get_data_range_info(self.symbol)
                if data_info:
                    print(f"📊 Data available: {data_info['total_records']} records from "
                          f"{data_info['start_time']} to {data_info['end_time']}")

        while True:
            try:
                # Check if any strategy has more data to process
                strategies_with_data = []
                for strat in self.strats:
                    if hasattr(strat, 'has_more_data') and strat.has_more_data():
                        strategies_with_data.append(strat.strat_name)

                # If no strategies have data, terminate backtest
                if not strategies_with_data:
                    print("\n✅ Backtest completed: No more data to process")
                    print(f"📈 Total iterations processed: {iterations:,}")
                    self._print_completion_summary()
                    break

                # Process next step for all strategies
                self.__check_step()
                iterations += 1

                # Progress reporting every 1000 iterations
                if iterations % 1000 == 0:
                    print(f"📊 Processed {iterations:,} iterations, strategies with data: {strategies_with_data}")

            except KeyboardInterrupt:
                print(f"\n⚠️  Backtest interrupted by user after {iterations:,} iterations")
                break
            except Exception as e:
                print(f"❌ Error in iteration {iterations:,}: {e}")
                print(traceback.format_exc())
                break

    def _print_completion_summary(self):
        """Print completion summary for each strategy"""
        for strat in self.strats:
            if hasattr(strat, 'data_provider'):
                print(f"📊 {strat.strat_name}: Data exhausted for {self.symbol}")
                if hasattr(strat, 'current_start_id'):
                    print(f"   Final database position: ID {strat.current_start_id - 1}")

    def __check_step(self):
        for strat in self.strats:
            try:
                strat.check_pair()
            except Exception as e:
                print(e)
                print(traceback.format_exc())

    def get_last_close(self, bm):
        if bm is None:
            l_bm = self.bms[0]
        else:
            l_bm = bm
        return l_bm.get_last_close()

    def get_same_orders_error(self, strat):
        return False

    def get_limit_orders(self, strat_id, symbol, direction):
        """
        Get active limit orders for a specific strategy, symbol, and direction.
        
        Args:
            strat: Strategy object
            symbol: Trading symbol (e.g., 'BTCUSDT')
            direction: Direction ('long' or 'short')
            
        Returns:
            List of active LimitOrder objects
        """
        # Find the market maker (bm) for this strategy
        for bm in self.bms:
            if strat_id == bm.strat:
                # Get orders filtered by symbol and direction
                return bm.backtest_order_manager.get_orders_by_direction(symbol, direction)
        
        # No matching market maker found
        return []

    def new_order(self, strat_id, side, symbol, price, direction, amount=None):
        order_price = 0
        for bm in self.bms:
            if strat_id == bm.strat:
                order_price, order_id = bm.new_limit_order(side, symbol, price, bm.name, direction, amount)
                break
        return order_price, order_id

    def cancel_order(self, strat_id, symbol, order_id):
        """
        Cancel an active order for a specific strategy.

        Args:
            strat_id: Strategy ID
            symbol: Trading symbol (e.g., 'BTCUSDT')
            order_id: ID of the order to cancel

        Returns:
            True if order was cancelled successfully, False otherwise
        """
        # Find the market maker (bm) for this strategy
        for bm in self.bms:
            if strat_id == bm.strat:
                # Check if backtest mode is initialized
                if bm.backtest_order_manager is None:
                    return False

                # Get current timestamp from market maker
                timestamp = bm.current_timestamp

                # Cancel order through BacktestOrderManager
                return bm.backtest_order_manager.cancel_order(order_id, timestamp)

        # No matching market maker found
        return False

    def get_last_filled_order(self, strat_id, symbol):
        """
        Get the last filled order for this strategy and symbol (any direction).

        Args:
            strat_id: Strategy ID
            symbol: Trading symbol (e.g., 'BTCUSDT')

        Returns:
            Dictionary with order details including 'execPrice' key, or None if no filled orders
        """
        for bm in self.bms:
            if strat_id == bm.strat:
                # Check if backtest mode is initialized
                if bm.backtest_order_manager is None:
                    return None

                # Get cached last filled order for symbol (O(1) lookup)
                last_order = bm.backtest_order_manager.get_last_filled_order(symbol)

                if last_order is None:
                    return None

                # Convert to dictionary with execPrice key for backward compatibility
                return {
                    'execPrice': last_order.fill_price,
                    'fill_price': last_order.fill_price,
                    'order_id': last_order.order_id,
                    'symbol': last_order.symbol,
                    'side': last_order.side.value,
                    'size': last_order.size,
                    'direction': last_order.direction,
                    'filled_at': last_order.filled_at.isoformat() if last_order.filled_at else None,
                    'status': last_order.status.value
                }

        # No matching strategy found
        return None

    def check_positions_ratio(self, strat_id, symbol, timestamp, last_close):
        for bm in self.bms:
            if strat_id == bm.strat:
                bm.check_positions_ratio(symbol, timestamp, last_close)

    def reset_same_orders_error(self, strat):
        pass
