from loggers import Loggers
import time
import pybit
from settings import Settings
from bybit_api_usdt import BybitApiUsdt
from greed import Greed


class Strat:
    def __init__(self, controller):
        self.controller = controller

        self.bms = list()

    def check_pair(self):
        pass


class Strat1(Strat):
    def __init__(self, controller, id, strat, symbol, greed_step, greed_count, direction, exchange, max_margin,
                 min_liq_ratio, max_liq_ratio, min_total_margin, long_koef, increase_same_position_on_low_margin):
        super().__init__(controller)
        self._symbol = symbol
        self.strat_name = strat
        self._exchange = exchange
        self.direction = direction
        self.id = id
        self.greed_step = greed_step
        self.greed_count = greed_count
        self.greed = Greed(self, symbol, greed_count, greed_step)
        self.last_filled_price = None
        self.last_close = None
        self.liq_ratio = {'min': min_liq_ratio, 'max': max_liq_ratio}
        self.max_margin = max_margin
        self.min_total_margin = min_total_margin
        self.long_koef = long_koef
        self.increase_same_position_on_low_margin = increase_same_position_on_low_margin

    def init_positions(self):
        for bm in self.bms:
            bm.init_positions(self)

    def init_symbol(self, symbol=None):
        l_symbol = symbol if symbol is not None else self._symbol
        for bm in self.bms:
            bm.symbol = l_symbol
            bm.long_koef = self.long_koef
            bm.read_ticksize(l_symbol)
            bm.connect_ws_public()
            bm.connect_ws_private()

    def _get_ticksize(self, symbol):
        if self._exchange == 'bybit_usdt':
            ticksize = BybitApiUsdt.ticksizes[symbol]
            multiplier = 1
        return ticksize, multiplier

    def _check_pair_step(self, symbol):
        # Loggers.log_order('Check pair step {} {} candle_len {}'.format(symbol, self._timeframe, len(candles)))
        next_step = True
        return next_step


    def check_pair(self):
        # Check pair one time for the same timestamp (i.e. 4h)
        try:
            self._check_pair_step(self._symbol)
        except pybit.exceptions.InvalidRequestError:
            Loggers.log_exception(f'{self._symbol} Invalid request error')
            time.sleep(1)

    def _cancel_limits(self, symbol):
        if Settings.DEBUG:
            return 0
        self.controller.cancel_limits(self, symbol)


class Strat50(Strat1):
    def _check_pair_step(self, symbol):
        # Loggers.log_order('Check pair step {} {} candle_len {}'.format(symbol, self._timeframe, len(candles)))
        if self.controller.get_same_orders_error(self):
            return

        # Build greed if empty
        self.greed.read_from_db()
        while len(self.greed.greed) <= 1:
            self.greed.build_greed(self.get_last_close())
        
        # Periodically rebuild greed
        # if self.is_do_rebuild_greed():
        #     self._cancel_limits(symbol)
        #     self.greed.rebuild_greed(self.get_last_close())

        self.check_positions_ratio()

        self._check_and_place('long')
        self._check_and_place('short')

        return True

    def _check_and_place(self, direction):
        limits = self.controller.get_limit_orders(self, self._symbol, direction)
        if len(limits) > len(self.greed.greed) + 10:
            self._rebuild_greed(self._symbol)
        if len(limits) > 0 and len(limits) < self.greed.greed_count:
            self.greed.update_greed(self.get_last_filled_price(), self.get_last_close())
        self.__place_greed_orders(limits, direction)

    def _rebuild_greed(self, symbol):
        self._cancel_limits(symbol)
        self.greed.rebuild_greed(self.get_last_close())
        Loggers.log_exception('Rebuild greed')
    
    def _get_wait_indices(self):
        wait_indices = [i for i, greed in enumerate(self.greed.greed) if greed['side'] == self.greed.WAIT]
        if wait_indices:
            # Use the middle of the WAIT region as center
            center_index = (wait_indices[0] + wait_indices[-1]) // 2
        else:
            # Fallback: use the middle of the entire list
            center_index = len(self.greed.greed) // 2 if self.greed.greed else 0
        return center_index

    def __place_greed_orders(self, i_limits, direction):
        limits = sorted(i_limits, key=lambda d: float(d['price']))
        limits_len = len(limits)
        # Use a dictionary for O(1) lookups instead of nested loops
        # Convert limit prices to float to match greed prices (which are floats)
        limit_prices = {float(limit['price']): limit for limit in limits}

        # Find center of the WAIT region
        center_index = self._get_wait_indices()
        # Create list of (index, greed) pairs, excluding WAIT items
        indexed_greeds = [(i, greed) for i, greed in enumerate(self.greed.greed) if greed['side'] != self.greed.WAIT]
        # Sort by distance from center (primary) then by price (secondary)
        sorted_greeds = sorted(indexed_greeds, key=lambda x: (abs(x[0] - center_index), x[1]['price']))

        for index, greed in sorted_greeds:
            # Place order if no limits
            if limits_len == 0:
                self.__place_order(greed, direction)
                continue

            # Check if limit exists for this greed price
            limit = limit_prices.get(greed['price'])
            if limit:
                if limit['side'] != greed['side']:
                    self.cancel_order(limit['orderId'])
                    self.__place_order(greed, direction)
            # Place order if no limit exists
            else:
                self.__place_order(greed, direction)

        # cancel limits if price outside greed
        # Use rounded comparison to handle floating-point precision issues
        greed_price_set = {round(greed['price'], 8) for greed in self.greed.greed}
        for limit in limits:
            limit_price = round(float(limit['price']), 8)
            if limit_price not in greed_price_set:
                self.cancel_order(limit['orderId'])

    def __place_order(self, greed, direction):
        if Settings.DEBUG:
            return 0
        last_close = self.get_last_close()
        if greed['side'] == self.greed.WAIT:
            return
        
        # Check if price is eligible (lower than last close if buy)
        diff_p = (last_close - greed['price']) / last_close * 100
        if (greed['side'] == self.greed.BUY and diff_p <= 0) or \
           (greed['side'] == self.greed.SELL and diff_p >= 0):
            return

        if abs(diff_p) <= self.greed.greed_step / 2:
            return

        price, order_id = self.controller.new_order(self, greed['side'], self._symbol, greed['price'], direction)
        if price == Settings.ERROR_PRICE:
            Loggers.log_exception('Error price triggered')
            time.sleep(0.3)
        return price

    def check_positions_ratio(self):
        self.controller.check_positions_ratio(self, self._symbol)

    def cancel_order(self, order_id):
        self.controller.cancel_order(self, self._symbol, order_id)

    def get_last_close(self):
        last_close = self.controller.get_last_close(self._symbol, self.bms[0])
        if last_close is not None:
            self.last_close = last_close
        return self.last_close

    def get_last_filled_price(self):
        order = self.controller.get_last_filled_order(self, self._symbol)
        try:
            self.last_filled_price = float(order['execPrice'])
        except Exception:
            pass
        return self.last_filled_price