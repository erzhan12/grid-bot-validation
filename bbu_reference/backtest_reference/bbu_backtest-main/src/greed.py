from datetime import UTC, datetime, timedelta

from src.bybit_api_usdt import BybitApiUsdt


class Greed:
    REBALANCE_THRESHOLD = 0.3  # Imbalance ratio threshold for grid rebalancing

    def __init__(self, strat, symbol, n=50, step=0.2, exchange='bybit_usdt'):
        self.greed = []
        self.symbol = symbol
        self.greed_count = n
        self.greed_step = step
        self.BUY = 'Buy'
        self.SELL = 'Sell'
        self.WAIT = 'wait'
        # self.round_price = 'BybitApiUsdt.round_price'
        self.last_move_border_time = datetime.now(UTC) - timedelta(days=1)
        self.strat_id = strat.id

    def build_greed(self, last_close):
        if not last_close:
            return
        half_greed = self.greed_count // 2
        i = 0
        step = self.greed_step / 100
        self.greed.append({
            'side': self.WAIT,
            'price': BybitApiUsdt.round_price(self.symbol, last_close)
        })  # Middle line = actual price
        price = last_close
        while i < half_greed:
            price = BybitApiUsdt.round_price(self.symbol, price * (1 + step))
            self.greed.append({'side': self.SELL, 'price': price})
            i += 1

        i = 0
        price = last_close
        while i < half_greed:
            price = BybitApiUsdt.round_price(self.symbol, price * (1 - step))
            self.greed.insert(0, {'side': self.BUY, 'price': price})
            i += 1

    def rebuild_greed(self, last_close):
        self.greed = []
        self.build_greed(last_close)

    # Return list of orders to be placed
    def update_greed(self, last_filled_price, last_close):
        if last_filled_price is None:
            return
        if last_close is None:
            return
        if not (self.__min_greed < last_close < self.__max_greed):
            self.rebuild_greed(last_close)
        for greed in self.greed:
            if self.is_too_close(greed['price'], last_filled_price):
                greed['side'] = self.WAIT
            elif last_close < greed['price']:
                greed['side'] = self.SELL
            elif last_close > greed['price']:
                greed['side'] = self.BUY

        self.center_grid()

        # self.write_to_db()

    def is_price_sorted(self):
        last_price = float('-inf')

        for greed in self.greed:
            if greed['price'] < last_price:
                # Prices should be in non-decreasing order
                return False
            last_price = greed["price"]

        return True

    def is_greed_correct(self):
        # Define the expected states
        expected_sequence = [self.BUY, self.WAIT, self.SELL]
        current_state = 0

        if not self.is_price_sorted():
            return False

        for greed in self.greed:
            side = greed['side']

            # Check if side matches the expected state
            if side == expected_sequence[current_state]:
                continue
            # If it encounters a 'wait', move to the 'Sell' state
            elif side == self.WAIT and current_state == 0:
                current_state = 2
            # If a 'Sell' is found after 'Buy', it should have found a 'wait' first
            elif side == self.SELL and current_state == 1:
                current_state = 2
            else:
                # If the sequence is not in order, return False
                return False

        # Check if 'Sell' was the last state to confirm the sequence completed correctly
        return current_state == 2

    def center_grid(self):
        # Use existing properties instead of re-counting
        buy_count = self.__greed_count_buy
        sell_count = self.__greed_count_sell

        # Early return for edge case - no orders to rebalance
        total_count = buy_count + sell_count
        if total_count == 0:
            return

        # Calculate imbalance ratio once
        imbalance_ratio = (buy_count - sell_count) / total_count

        step = self.greed_step / 100

        # Too many BUY orders - shift grid upward
        if imbalance_ratio > self.REBALANCE_THRESHOLD:
            # Find highest sell price by iterating from the end
            highest_sell_price = None
            for greed in reversed(self.greed):
                if greed['side'] == self.SELL:
                    highest_sell_price = greed['price']
                    break

            if highest_sell_price is not None and len(self.greed) > 0:
                self.greed.pop(0)  # Delete the bottom line
                price = BybitApiUsdt.round_price(self.symbol, highest_sell_price * (1 + step))
                self.greed.append({'side': self.SELL, 'price': price})

        # Too many SELL orders - shift grid downward
        elif imbalance_ratio < -self.REBALANCE_THRESHOLD:
            # Find lowest buy price by iterating from the start
            lowest_buy_price = None
            for greed in self.greed:
                if greed['side'] == self.BUY:
                    lowest_buy_price = greed['price']
                    break

            if lowest_buy_price is not None and len(self.greed) > 0:
                self.greed.pop()  # Delete the top line
                price = BybitApiUsdt.round_price(self.symbol, lowest_buy_price * (1 - step))
                self.greed.insert(0, {'side': self.BUY, 'price': price})

    def is_too_close(self, price1, price2):
        return abs(price1 - price2) / price1 * 100 < self.greed_step / 4

    @property
    def __greed_count_sell(self):
        n = 0
        for step in self.greed:
            if step['side'] == self.SELL:
                n += 1
        return n

    @property
    def __greed_count_buy(self):
        n = 0
        for step in self.greed:
            if step['side'] == self.BUY:
                n += 1
        return n

    @property
    def __min_greed(self):
        prices = [step['price'] for step in self.greed]
        return min(prices)

    @property
    def __max_greed(self):
        prices = [step['price'] for step in self.greed]
        return max(prices)



