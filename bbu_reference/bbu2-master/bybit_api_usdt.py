# -*- coding: utf-8 -*-
import time
from datetime import datetime, timedelta, UTC
from loggers import Loggers
import math
from pybit.unified_trading import HTTP, WebSocket
from pybit import exceptions as pybit_exceptions
from settings import Settings
from position import Position


class BybitApiUsdt:
    ticksizes = dict()

    def __init__(self, APIKey, secret, amount, strat, name, controller, is_testnet=False):
        self.CHANNEL_TYPE_LINEAR = 'linear'
        self.CHANNEL_TYPE_PRIVATE = 'private'

        self.APIKey = APIKey
        self.Secret = secret
        self.symbol = None
        self.amount = amount
        self.name = name
        self.strat = strat
        self.min_amount = None
        self.controller = controller
        self.exchange = 'bybit_usdt'
        self.is_testnet = is_testnet
        self.session = HTTP(testnet=self.is_testnet, api_key=APIKey, api_secret=secret)
        self.ws_public = None
        self.ws_private = None

        self.last_checked_position = {'long': datetime.now(UTC) - timedelta(days=1),
                                      'short': datetime.now(UTC) - timedelta(days=1)}
        self.POSITION_CHECK_INTERVAL = 5 * 60
        self.last_filled_order = None
        self._last_limits_read = {'long': datetime.now(UTC) - timedelta(days=1),
                                 'short': datetime.now(UTC) - timedelta(days=1)}
        self.LIMITS_READ_INTERVAL = 61
        self.limits = {'long': [], 'short': []}
        self.order_status_active = ['Active', 'New', 'PartiallyFilled']

        self.GET_WALLET_INTERVAL = 10 * 62
        self._last_wallet_time = datetime.now(UTC) - timedelta(days=1)
        self.wallet = None

        self.POSITIONS_RATIO_CHECK_INTERVAL = 1 * 63
        self.last_checked_positions_ratio = datetime.now(UTC) - timedelta(days=1)

        self.position = {'long': None, 'short': None}
        self.position_ratio = None
        self.log_counter = 999
        self.MAX_LOG_COUNTER = 40

        self.ENSURE_SOCKET_INTERVAL = 10
        self._last_checked_socket_public = datetime.now(UTC) - timedelta(days=1)
        self._last_checked_socket_private = datetime.now(UTC) - timedelta(days=1)

        self.ticker_data = None
        self.position_data = {'Buy': None, 'Sell': None}
        self.execution_data = []
        self.order_data = {'long': [], 'short': []}

        self._same_orders_error = False
        self.SAME_ORDER_MAX_WAIT = 6000  # 600 seconds
        self._same_order_counter = 0

        self.long_koef = 1.0

    def init_positions(self, strat):
        self.position = {'long': Position('long', strat), 'short': Position('short', strat)}
        self.position['long'].set_opposite(self.position['short'])
        self.position['short'].set_opposite(self.position['long'])

    def connect_ws_public(self):
        self.ws_public = WebSocket(
            testnet=self.is_testnet,
            channel_type=self.CHANNEL_TYPE_LINEAR,
        )
        self.ws_public.ticker_stream(self.symbol, self.handle_ticker)

    def connect_ws_private(self):
        self.ws_private = WebSocket(
            testnet=self.is_testnet,
            channel_type=self.CHANNEL_TYPE_PRIVATE,
            api_key=self.APIKey,
            api_secret=self.Secret,
            trace_logging=False
        )
        self.ws_private.position_stream(self.handle_position)
        self.ws_private.order_stream(self.handle_order)
        self.ws_private.execution_stream(self.handle_execution)

    def handle_ticker(self, message):
        try:
            self.ticker_data = message['data']
            # print(f'handle ticker Last Price: {self.ticker_data['lastPrice']} symbol: {self.symbol}')
        except (KeyError, ValueError):
            pass

    def handle_position(self, message):
        try:
            l_position_data = list(filter(lambda x: x['category'] == self.CHANNEL_TYPE_LINEAR and
                                                    x['symbol'] == self.symbol, message['data']))
            for pos in l_position_data:
                self.position_data[pos['side']] = pos
            # print(f'handle position {self.position_data}')
        except (KeyError, ValueError):
            pass
        except IndexError:
            pass

    def handle_order(self, message):
        try:
            limits_ws = list(filter(lambda x: x['category'] == self.CHANNEL_TYPE_LINEAR and
                                              x['symbol'] == self.symbol and
                                              x['orderType'] == 'Limit', message['data']))

            limits_ws_long = self.__filter_limits(limits_ws, 'long')
            self.order_data['long'] += limits_ws_long
            limits_ws_short = self.__filter_limits(limits_ws, 'short')
            self.order_data['short'] += limits_ws_short
            # print(f'handle order {self.order_data}')
        except (KeyError, ValueError):
            pass

    def handle_execution(self, message):
        try:
            new_execution_data = message['data']
            filled_orders = list(filter(lambda x: x['symbol'] == self.symbol and
                                                  x['leavesQty'] == '0' and
                                                  x['execType'] == 'Trade', new_execution_data))
            if not filled_orders:
                return
            self.execution_data = filled_orders + self.execution_data
            self.execution_data = sorted(self.execution_data, key=lambda x: x['execTime'], reverse=True)
            self._check_same_orders(self.execution_data)
            self.execution_data = self.execution_data[:4]  # sublist with the only first elements
            # print(self.execution_data)
        except Exception:
            pass

    def _check_same_orders(self, execution_data):
        execution_long = list(filter(lambda x: (x['side'] == 'Buy' and x['closedSize'] != '0' or
                                                x['side'] == 'Sell' and x['closedSize'] == '0'), execution_data))
        execution_long = sorted(execution_long, key=lambda x: x['execTime'], reverse=True)[:2]
        execution_short = list(filter(lambda x: (x['side'] == 'Buy' and x['closedSize'] == '0' or
                                                x['side'] == 'Sell' and x['closedSize'] != '0'), execution_data))
        execution_short = sorted(execution_short, key=lambda x: x['execTime'], reverse=True)[:2]
        self._check_same_orders_side(execution_long)
        if self._same_orders_error:
            return True
        self._check_same_orders_side(execution_short)
        if self._same_orders_error:
            return True

    def _check_same_orders_side(self, execution_data):
        # compare exePrice, leavesQty=0 new and old if equal then error restart socket and sleep 60 min
        self._same_orders_error = False
        for current_dict, next_dict in zip(execution_data, execution_data[1:]):
            # print(f'current dict {current_dict['execPrice']} {current_dict['side']}')
            # print(f'next dict {next_dict['execPrice']} {next_dict['side']}')
            if current_dict['execPrice'] == next_dict['execPrice'] and current_dict['side'] == next_dict['side']:
                if current_dict['orderId'] == next_dict['orderId']:
                    # print('same  order  id, it is ok')
                    return
                Loggers.log_exception(f'{self.symbol} Same order error detected. {current_dict['execPrice']}')
                self._same_orders_error = True
                return

    def get_same_orders_error(self):
        if self._same_orders_error:
            self.__same_order_counter()
            if self._same_order_counter == 1:  # for the first only
                Loggers.log_exception(f'{self.symbol} Check same order error.')

        return self._same_orders_error

    def _reset_ws_public(self):
        self.ws_public.exit()
        self.connect_ws_public()

    def _reset_ws_private(self):
        self.ws_private.exit()
        self.connect_ws_private()

    def _ensure_private_connection(self):
        if datetime.now(UTC) - self._last_checked_socket_private < timedelta(seconds=self.ENSURE_SOCKET_INTERVAL):
            return True

        self._last_checked_socket_private = datetime.now(UTC)
        while True:
            # print('Checking socket ')
            if self.ws_private.is_connected():
                return True
            else:
                Loggers.log_exception('Ensure private socket connection problem. Reconnecting..')
                try:
                    self._reset_ws_private()
                except Exception:
                    pass
                time.sleep(1)

    def _ensure_public_connection(self, symbol):
        if datetime.now(UTC) - self._last_checked_socket_public < timedelta(seconds=self.ENSURE_SOCKET_INTERVAL):
            return True

        self._last_checked_socket_public = datetime.now(UTC)
        while True:
            # print('Checking socket ')
            if self.ws_public.is_connected():
                return True
            else:
                Loggers.log_exception('Ensure public socket connection problem. Reconnecting..')
                try:
                    self._reset_ws_public()
                except Exception:
                    pass
                time.sleep(1)

    def get_last_close(self, symbol):
        self._ensure_public_connection(symbol)
        try:
            return float(self.ticker_data["lastPrice"])
        except Exception:
            return None

    def get_last_filled_order(self, symbol='BTCUSD'):
        try:
            self._ensure_private_connection()
            self.last_filled_order = self.execution_data[0]
        except Exception:
            if self.last_filled_order is None:
                self.last_filled_order = self.__get_last_filled_order_rest(symbol)
        return self.last_filled_order

    def __get_last_filled_order_rest(self, symbol='BTCUSD'):
        try:
            r = self.session.get_executions(category=self.CHANNEL_TYPE_LINEAR, symbol=symbol, execType='Trade')
            data = r['result']['list']
            filled_orders = list(filter(lambda x: x['leavesQty'] == '0', data))
            return filled_orders[0]
        except Exception:
            return None

    def new_limit_order(self, side, symbol, price, bm_name, direction, amount=None):
        order_id = ''
        l_price = BybitApiUsdt.round_price(symbol, price)
        reduce_only = self._get_reduce_only(direction, side)
        position_idx = self._get_position_idx(direction)
        if amount is None:
            l_amount = self.__get_amount(symbol, l_price, side=side, bm_name=bm_name)
        else:
            l_amount = amount
        l_amount_multiplier = self.__get_amount_multiplier(symbol, side, l_price, direction)
        try:
            if 1.1 < self.position_ratio < 10 and \
                    self.position['long'].liq_price == 0.0 and self.position['short'].liq_price == 0.0:
                l_amount_multiplier *= self.long_koef
        except TypeError:
            pass

        l_amount = self.__round_amount(l_amount * l_amount_multiplier)
        if self._is_good_to_place(symbol, l_price, l_amount, side, direction, reduce_only):
            l_price, order_id = self._place_active_order(symbol, side, l_amount, l_price, reduce_only, position_idx)
        # else:
        # l_price = Settings.ERROR_PRICE

        return l_price, order_id

    def __round_amount(self, amount):
        l_amount = math.ceil(amount / self.min_amount) * self.min_amount
        return float('{:.10f}'.format(l_amount))

    def _get_position_idx(self, direction):
        if direction == 'long':
            return 1
        elif direction == 'short':
            return 2
        return 0

    def _get_reduce_only(self, direction, side):
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

    def _is_good_to_place(self, symbol, price, amount, side, direction, reduce_only):
        limits = self.get_limit_orders(symbol, direction)
        for limit in limits:
            if limit['price'] == price and float(limit['qty']) == amount and limit['side'] == side and \
                    limit['reduceOnly'] == reduce_only:
                return False

        direction_mapping = {
            'long': {'open': 'Buy', 'close': 'Sell'},
            'short': {'open': 'Sell', 'close': 'Buy'}
        }
        if side == direction_mapping[direction]['open']:
            return True
        position_size = self.position[direction].size
        limits_qty = amount
        for limit in limits:
            if limit['reduceOnly'] and limit['side'] == direction_mapping[direction]['close']:
                limits_qty += float(limit['qty'])
        return position_size > limits_qty

    def _place_active_order(self, symbol, side, amount, price, reduce_only, position_idx):
        try:
            # Loggers.logger_check.info('Placing order. pair: {}, amount: {}'.format(symbol, amount))
            r = self.session.place_order(category=self.CHANNEL_TYPE_LINEAR,
                                         symbol=symbol, side=side, orderType="Limit",
                                         qty=str(amount), price=str(price),
                                         reduceOnly=reduce_only, positionIdx=position_idx)

            order_id = r['result']['orderId']
            # Loggers.logger_check.info('order_id: {}'.format(order_id))
        except Exception as e:
            Loggers.log_exception('{}: {}'.format(type(e), e))
            return Settings.ERROR_PRICE, ''
        # self.controller.write_min_amount_db(bm_name, l_amount)
        return price, order_id

    def cancel_all_limits(self, symbol):
        limits_long = self.get_limit_orders(symbol, 'long')
        limits_short = self.get_limit_orders(symbol, 'short')
        while len(limits_long) > 0 or len(limits_short) > 0:
            self._cancel_all_active_orders(symbol)
            time.sleep(2)
            Loggers.log_exception('Sleeping 10 sec')
            limits_long = self.get_limit_orders(symbol, 'long')
            limits_short = self.get_limit_orders(symbol, 'short')

    def get_limit_orders(self, symbol, direction='long'):
        limits = self.limits[direction]
        if datetime.now(UTC) - self._last_limits_read[direction] > timedelta(seconds=self.LIMITS_READ_INTERVAL):
            limits = self.__get_limit_orders_rest(symbol)
            self._last_limits_read[direction] = datetime.now(UTC)
        else:
            # read from web socket and append to self.limits
            try:
                new_limits_ws = self.order_data[direction]
                for limit_ws in new_limits_ws:
                    found = False
                    for limit_existing in limits:
                        if limit_existing['orderId'] == limit_ws['orderId']:
                            found = True
                            limit_existing['orderStatus'] = limit_ws['orderStatus']
                            break
                    if not found:
                        limits.append(limit_ws)
                limits = list(filter(lambda x: x['orderStatus'] in self.order_status_active, limits))
            except Exception:
                pass

        limits = self.__filter_limits(limits, direction)
        self.order_data[direction] = list()  # clear
        self.limits[direction] = limits
        return limits

    def __filter_limits(self, limits, direction):
        direction_mapping = {
            'long': {'open_side': 'Buy', 'close_side': 'Sell'},
            'short': {'open_side': 'Sell', 'close_side': 'Buy'}
        }
        new_limits = list(filter(lambda x:
                             x['side'] == direction_mapping[direction]['open_side'] and not x['reduceOnly'] or
                             x['side'] == direction_mapping[direction]['close_side'] and x['reduceOnly'],
                             limits
                             ))
        return new_limits

    def __get_limit_orders_rest(self, symbol, ord_type='Limit'):
        limits = list()
        r = self.session.get_open_orders(category=self.CHANNEL_TYPE_LINEAR, symbol=symbol, limit=50)['result']
        orders = r['list']
        if not orders:
            return []
        next_cursor = r['nextPageCursor']
        try:
            limits = list(filter(lambda x: x['orderType'] == ord_type and
                                           x['orderStatus'] in self.order_status_active, orders))
        except Exception:
            pass
        while True:
            r = self.session.get_open_orders(category=self.CHANNEL_TYPE_LINEAR, symbol=symbol, limit=50,
                                             cursor=next_cursor)['result']
            orders = r['list']
            if not orders:
                break
            next_cursor = r['nextPageCursor']
            try:
                limits += list(filter(lambda x: x['orderType'] == ord_type and
                                                x['orderStatus'] in self.order_status_active, orders))
            except Exception:
                break
        return limits

    def __reset_amount_multiplier(self):
        self.position['long'].reset_amount_multiplier()
        self.position['short'].reset_amount_multiplier()

    def __update_position_ratio(self):
        self.position['long'].position_ratio = self.position_ratio
        self.position['short'].position_ratio = self.position_ratio

    def __get_position_status(self, symbol, direction='long'):
        direction_mapping = {
            'long': 'Buy',
            'short': 'Sell'
        }
        side = direction_mapping[direction]
        self._ensure_private_connection()
        l_pos = self.position_data[side]
        if l_pos is None:
            l_pos = self.__get_position_status_rest(symbol, side)
        l_wallet_balance = self.__get_wallet_amount()
        self.position[direction].update_position(l_pos, l_wallet_balance, self.get_last_close(symbol))

    def __get_position_status_rest(self, symbol, side='Buy'):
        try:
            r = self.session.get_positions(category=self.CHANNEL_TYPE_LINEAR, symbol=symbol)
            try:
                pos = list(filter(lambda x: x['side'] == side, r['result']['list']))[0]
            except KeyError:
                return None
            return pos if pos['side'] in ['Buy', 'Sell'] else None
        except IndexError:
            pass
        except Exception as e:
            Loggers.log_exception(f'{type(e)}: {e}')
            return None
        return None

    def cancel_order(self, symbol, order_id):
        Loggers.logger_check.info(f'Canceling order: {order_id}')
        try:
            self.session.cancel_order(category=self.CHANNEL_TYPE_LINEAR, symbol=symbol, order_id=order_id)
        except pybit_exceptions.InvalidRequestError:
            return False
        return True

    def _cancel_all_active_orders(self, symbol):
        Loggers.logger_check.info('Canceling all active orders')
        self.session.cancel_all_orders(category=self.CHANNEL_TYPE_LINEAR, symbol=symbol)

    def __get_wallet(self, coin=''):
        if coin != '':
            r = self.session.get_wallet_balance(accountType="UNIFIED", coin=coin)
        else:
            r = self.session.get_wallet_balance(accountType="UNIFIED")
        wallet = r['result']['list'][0]['coin']
        return wallet

    def __get_wallet_amount(self, coin='USDT'):
        total_balance = 0.0
        if datetime.now(UTC) - self._last_wallet_time > timedelta(seconds=self.GET_WALLET_INTERVAL):
            self.wallet = self.__get_wallet()
            self._last_wallet_time = datetime.now(UTC)
        for coin in self.wallet:
            if coin['coin'] != 'USDT':
                total_balance += float(coin['usdValue'])
            else:
                total_balance += float(coin['walletBalance'])

        return total_balance

    def read_ticksize(self, symbol):
        r = self.session.get_instruments_info(category=self.CHANNEL_TYPE_LINEAR, symbol=symbol)
        symbols = r['result']['list']
        symbol_info = list(filter(lambda x: x['symbol'] == symbol, symbols))[0]
        BybitApiUsdt.ticksizes[symbol] = float(symbol_info['priceFilter']['tickSize'])
        self.min_amount = float(symbol_info['lotSizeFilter']['qtyStep'])

    @staticmethod
    def round_price(symbol, price, ticksize=None):
        if ticksize is None:
            ticksize = BybitApiUsdt.ticksizes[symbol]
        l_price = round(price / ticksize) * ticksize
        l_price = float('{:.10f}'.format(l_price))  # to avoid 4.76999999e-6 != 4.77
        return l_price

    def __get_amount(self, symbol, price, side='', bm_name='', open_price=0, status='', position_amount=0):
        min_amount_usdt = 5
        amount = 0.0
        try:
            amount = float(self.amount)
        except ValueError:
            if self.amount[0] == 'x':
                if 'USDT' in symbol:
                    currency = 'USDT'
                else:
                    currency = symbol[:3]
                wallet_amount = self.__get_wallet_amount(currency)

                try:
                    mult = float(self.amount[1:])
                    amount = self.__round_amount(wallet_amount / price * mult)
                    # Loggers.logger_check.info('amount = {} * {} * {} '.format(wallet_amount, price, mult))
                except ValueError:
                    pass
            elif self.amount[0] == 'b':
                try:
                    btc_amount = float(self.amount[1:])
                    if symbol == 'BTCUSD':
                        amount = btc_amount * price
                    else:
                        amount = math.ceil(btc_amount / price)
                    # Loggers.logger_check.info('amount = {} * {}'.format(btc_amount, price))
                except ValueError:
                    pass

        min_amount = min_amount_usdt / price
        if amount < min_amount:
            amount = self.__round_amount(min_amount)
        # db_min_amount = self.controller.read_min_amount_db(bm_name)
        db_min_amount = 0
        return max(amount, self.min_amount, db_min_amount)

    def __get_amount_multiplier(self, symbol, side, price, direction):
        amount_multiplier = 1
        l_position = self.position[direction]

        if l_position.is_empty():
            self.__get_position_status(symbol, direction)
        try:
            amount_multiplier = self.position[direction].get_amount_multiplier()[side]

        except Exception:
            pass
        return amount_multiplier

    def check_positions_ratio(self, symbol):
        if datetime.now(UTC) - self.last_checked_positions_ratio > timedelta(seconds=self.POSITIONS_RATIO_CHECK_INTERVAL):
            self.last_checked_positions_ratio = datetime.now(UTC)

            try:
                self.__reset_amount_multiplier()
                self.__get_position_status(symbol, 'long')
                self.__get_position_status(symbol, 'short')
                self.position_ratio = self.position['long'].size / self.position['short'].size
                self.__update_position_ratio()
                last_close = self.get_last_close(symbol)

                if self.log_counter > self.MAX_LOG_COUNTER:
                    Loggers.log_exception(f'Position ratio: {symbol} {self.position_ratio:.2f}')
                    self.position['long'].log_position(symbol, last_close)
                    self.position['short'].log_position(symbol, last_close)
                    self.log_counter = 0
                else:
                    self.log_counter += 1

            except TypeError:
                pass
            except ZeroDivisionError:
                pass

    def __same_order_counter(self):
        self._same_order_counter += 1
        if self._same_order_counter >= self.SAME_ORDER_MAX_WAIT:
            self._same_order_counter = 0

