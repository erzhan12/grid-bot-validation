from loggers import Loggers


class Position:
    SIDE_BUY = 'Buy'
    SIDE_SELL = 'Sell'

    def __init__(self, direction, strat):
        self.__direction = direction
        self.__position_response = None
        self.__wallet_balance = None
        self.__margin = None
        self.__opposite = None
        self.__amount_multiplier = {Position.SIDE_BUY: 1.0, Position.SIDE_SELL: 1.0}
        self.__min_liq_ratio = strat.liq_ratio['min']
        self.__max_liq_ratio = strat.liq_ratio['max']
        self.__max_margin = strat.max_margin
        self.__min_total_margin = strat.min_total_margin
        self.__strat_id = strat.id
        self.__upnl = None
        self.position_ratio = 1
        self.__increase_same_position_on_low_margin = strat.increase_same_position_on_low_margin

    def log_position(self, symbol, last_close):
        log = f'{symbol}-{self.__strat_id} {self.__direction} margin:{self.__margin:.2f}\n' \
              f'liq_price:{self.liq_price:.2f} ratio:{self.get_liquidation_ratio(last_close):.2f}\n' \
              f'unrealised PnL:{self.__upnl:.2f}%\n' \
              f'multiplier:{self.__amount_multiplier}\n' \
              f'position_ratio:{self.position_ratio:.2f}\n' \
              f'total margin: {self.get_total_margin():.2f}'
        Loggers.log_exception(log)

    def reset_amount_multiplier(self):
        self.set_amount_multiplier(Position.SIDE_BUY, 1.0)
        self.set_amount_multiplier(Position.SIDE_SELL, 1.0)

    def _adjust_position_for_low_margin(self):
        """Adjust position multipliers when total margin is below minimum and positions are equal."""
        if self.__increase_same_position_on_low_margin:
            # Increase same position by doubling order size
            if self.__direction == 'long':
                self.set_amount_multiplier(Position.SIDE_BUY, 2.0)
            else:  # short
                self.set_amount_multiplier(Position.SIDE_SELL, 2.0)
        else:
            # Increase position by reducing opposite side order size
            if self.__direction == 'long':
                self.set_amount_multiplier(Position.SIDE_SELL, 0.5)
            else:  # short
                self.set_amount_multiplier(Position.SIDE_BUY, 0.5)

    def __calc_amount_multiplier(self, pos, last_close):
        # long
        try:
            entry_price = pos['entryPrice']
        except KeyError:
            entry_price = pos['avgPrice']
        if self.__direction == 'long':
            self.__upnl = (1 / float(entry_price) - 1 / last_close) * float(entry_price) * 100 * float(pos['leverage'])
            if self.get_liquidation_ratio(last_close) > 1.05 * self.__min_liq_ratio:
                self.set_amount_multiplier(Position.SIDE_SELL, 1.5)  # decrease long position

            elif self.get_liquidation_ratio(last_close) > self.__min_liq_ratio:
                # if self.__opposite.get_margin() > self.__max_margin:
                #     self.set_amount_multiplier(Position.SIDE_SELL, 2.0)  # decrease long position
                # else:
                #     self.__opposite.set_amount_multiplier(Position.SIDE_SELL, 2.0)  # increase short position
                self.__opposite.set_amount_multiplier(Position.SIDE_BUY, 0.5)  # increase short position
            elif self.is_position_equal() and self.get_total_margin() < self.__min_total_margin:
                self._adjust_position_for_low_margin()
            elif self.position_ratio < 0.5 and self.__upnl < 0:
                self.set_amount_multiplier(Position.SIDE_BUY, 2)  # increase long position
            elif self.position_ratio < 0.20:
                self.set_amount_multiplier(Position.SIDE_BUY, 2)  # increase long position
        # short
        if self.__direction == 'short':
            self.__upnl = (1 / last_close - 1 / float(entry_price)) * float(entry_price) * 100 * float(pos['leverage'])
            if 0.0 < self.get_liquidation_ratio(last_close) < 0.95 * self.__max_liq_ratio:
                self.set_amount_multiplier(Position.SIDE_BUY, 1.5)  # decrease short position

            elif 0.0 < self.get_liquidation_ratio(last_close) < self.__max_liq_ratio:
                # if self.__opposite.get_margin() > self.__max_margin:
                #     self.set_amount_multiplier(Position.SIDE_BUY, 2.0)  # decrease short position
                # else:
                #     self.__opposite.set_amount_multiplier(Position.SIDE_BUY, 2.0)  # increase long position
                self.__opposite.set_amount_multiplier(Position.SIDE_SELL, 0.5)  # increase long position
            elif self.is_position_equal() and self.get_total_margin() < self.__min_total_margin:
                self._adjust_position_for_low_margin()
            elif self.position_ratio > 2.0 and self.__upnl < 0:
                self.set_amount_multiplier(Position.SIDE_SELL, 2)  # increase short position
            elif self.position_ratio > 5.0:
                self.set_amount_multiplier(Position.SIDE_SELL, 2)  # increase short position


    def set_amount_multiplier(self, side, mult):
        self.__amount_multiplier[side] = mult

    def get_amount_multiplier(self):
        return self.__amount_multiplier

    def update_position(self, position_response, wallet_balance, last_close):
        try:
            self.__position_response = position_response
            self.__wallet_balance = wallet_balance
            self.__margin = float(self.__position_response['positionValue']) / wallet_balance
            self.__calc_amount_multiplier(position_response, last_close)
        except TypeError:
            self.__margin = 0

    def set_opposite(self, opposite):
        self.__opposite = opposite

    def is_empty(self):
        if self.__position_response is None:
            return True
        return False

    def get_margin(self):
        return self.__margin

    def get_liquidation_ratio(self, last_close):
        return self.liq_price / last_close

    def is_position_equal(self):
        try:
            return 0.94 < self.get_margin_ratio() < 1.05
        except ZeroDivisionError:
            return False

    def get_margin_ratio(self):
        ratio = self.get_margin() / self.__opposite.get_margin()
        return ratio

    def get_total_margin(self):
        return self.__margin + self.__opposite.get_margin()

    @property
    def size(self):
        try:
            return float(self.__position_response['size'])
        except TypeError:
            return 0.0

    @property
    def liq_price(self):
        try:
            liq_price = float(self.__position_response['liqPrice'])
        except ValueError:
            liq_price = 0.0
        return liq_price

    @property
    def entry_price(self):
        return float(self.__position_response['entryPrice'])

    @property
    def position_value(self):
        return float(self.__position_response['positionValue'])
