from db_files import DbFiles
from loggers import Loggers
from bybit_api_usdt import BybitApiUsdt


class Greed:
    def __init__(self, strat, symbol, n=50, step=0.2):
        self.greed = []
        self.symbol = symbol
        self.greed_count = n
        self.greed_step = step
        self.BUY = 'Buy'
        self.SELL = 'Sell'
        self.WAIT = 'wait'
      
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
            price = BybitApiUsdt.round_price(self.symbol, price*(1+step))
            self.greed.append({'side': self.SELL, 'price': price})
            i += 1

        i = 0
        price = last_close
        while i < half_greed:
            price = BybitApiUsdt.round_price(self.symbol, price*(1-step))
            self.greed.insert(0, {'side': self.BUY, 'price': price})
            i += 1

        self.write_to_db()

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
            Loggers.log_exception('Rebuild greed bbu: Out of bounds')
        for greed in self.greed:
            if self.__is_too_close(greed['price'], last_filled_price):
                greed['side'] = self.WAIT
            elif last_close < greed['price']:
                greed['side'] = self.SELL
            elif last_close > greed['price']:
                greed['side'] = self.BUY

        self.__center_greed()

        self.write_to_db()

    def __center_greed(self):
        buy_count = 0
        sell_count = 0
        highest_sell_price = 0
        lowest_buy_price = self.greed[0]['price'] if self.greed else 0
        step = self.greed_step / 100
        
        # Single pass to count and find prices
        for greed in self.greed:
            if greed['side'] == self.BUY:
                buy_count += 1
            elif greed['side'] == self.SELL:
                sell_count += 1
                highest_sell_price = greed['price']
        
        total_count = buy_count + sell_count
        if total_count == 0:
            return
            
        if (buy_count - sell_count) / total_count > 0.3:
            self.greed.pop(0)  # Delete the bottom line
            price = BybitApiUsdt.round_price(self.symbol, highest_sell_price*(1+step))
            self.greed.append({'side': self.SELL, 'price': price})
        elif (sell_count - buy_count) / total_count > 0.3:
            self.greed.pop()  # Delete the top line
            price = BybitApiUsdt.round_price(self.symbol, lowest_buy_price*(1-step))
            self.greed.insert(0, {'side': self.BUY, 'price': price})

    def __is_too_close(self, price1, price2):
        return abs(price1 - price2) / price1 * 100 < self.greed_step / 4

    def read_from_db(self):
        self.greed = DbFiles.read_greed(self.strat_id)

    def write_to_db(self):
        DbFiles.write_greed(self.greed, self.strat_id)

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



