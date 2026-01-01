import time
from loggers import Loggers
from settings import Settings

from bybit_api_usdt import BybitApiUsdt

from strat import Strat50


class Controller:
    def __init__(self):
        Settings.read_settings()
        Loggers.init_loggers()
        Loggers.log_exception('Let MK begin Bbu')

        self.bms = self.__init_bms()
        self.strats = self.__init_strats()

    def __init_strats(self):
        strats = list()
        for pt in Settings.pair_timeframes:
            strat = Strat50(self, **pt)
            for bm in self.bms:
                if pt['id'] == bm.strat:
                    strat.bms.append(bm)
                    strat.init_symbol()
                    strat.init_positions()
            if len(strat.bms) > 0:
                strats.append(strat)
        return strats

    def __init_bms(self):
        bms = list()
        for bm_key in Settings.bm_keys:
            amount = None
            name = ''
            for am in Settings.amounts:
                if am['name'] == bm_key['name']:
                    name = am['name']
                    amount = am['amount']
                    strat = am['strat']
                    is_testnet = am['is_testnet']
                    break
            if amount is not None:
                if bm_key['exchange'] == 'bybit_usdt':
                    bm = BybitApiUsdt(bm_key['key'], bm_key['secret'], amount, strat, name, self, is_testnet)
                if self.__check_bm_key(bm):
                    bms.append(bm)
        return bms

    def __check_bm_key(self, bm):
        try:
            return True
        except Exception as e:
            Loggers.log_exception(f'{type(e)}: {e}')
            return False

    def check_job(self):
        while True:
            try:
                Loggers.check_new_day()
                self.__check_step()
                time.sleep(Settings.INTERVALS['CHECK'])
            except ConnectionError as e:
                Loggers.log_exception(f'{type(e)}: {e}')
                time.sleep(1)
            except Exception as e:
                Loggers.log_exception(f'{type(e)}: {e}')
                time.sleep(1)

    def __check_step(self):
        for strat in self.strats:
            try:
                strat.check_pair()

            except TypeError as e:
                Loggers.log_exception(f'{type(e)}: {e}')
                time.sleep(1)

    def get_last_close(self, symbol, bm=None):
        if bm is None:
            l_bm = self.bms[0]
        else:
            l_bm = bm
        return l_bm.get_last_close(symbol)

    def new_order(self, strat, side, symbol, price, direction, amount=None, ord_type='Limit'):
        order_price = 0
        for bm in self.bms:
            if strat.id == bm.strat:
                if ord_type == 'Limit':
                    order_price, order_id = bm.new_limit_order(side, symbol, price, bm.name, direction, amount)
                elif ord_type == 'Market':
                    pass
 
        return order_price, order_id

    def check_positions_ratio(self, strat, symbol):
        for bm in self.bms:
            if strat.id == bm.strat:
                bm.check_positions_ratio(symbol)

    def cancel_order(self, strat, symbol, order_id):
        for bm in self.bms:
            if strat.id == bm.strat:
                bm.cancel_order(symbol, order_id)

    def cancel_limits(self, strat, symbol):
        for bm in self.bms:
            if strat.id == bm.strat:
                bm.cancel_all_limits(symbol)

    def get_limit_orders(self, strat, symbol, direction='long'):
        for bm in self.bms:
            if strat.id == bm.strat:
                limits = bm.get_limit_orders(symbol, direction)
                return limits

    def get_last_filled_order(self, strat, symbol):
        for bm in self.bms:
            if strat.id == bm.strat:
                order = bm.get_last_filled_order(symbol)
                return order

    def get_same_orders_error(self, strat):
        for bm in self.bms:
            if strat.id == bm.strat:
                return bm.get_same_orders_error()

