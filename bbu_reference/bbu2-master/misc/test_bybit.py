"""
To see which WebSocket topics are available, check the Bybit API documentation:
https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
"""

from time import sleep

# Import WebSocket from the unified_trading module.
from pybit.unified_trading import WebSocket, HTTP

# Set up logging (optional)
import logging
logging.basicConfig(filename="pybit.log", level=logging.DEBUG,
                    format="%(asctime)s %(levelname)s %(message)s")

api_key = 'socket_key'
api_secret = 'socket_secret'
# Connect with authentication!
# Here, we are connecting to the "linear" WebSocket, which will deliver public
# market data for the linear (USDT) perpetuals.
# The available channel types are, for public market data:
#    inverse – Inverse Contracts;
#    linear  – USDT Perpetual, USDC Contracts;
#    spot    – Spot Trading;
#    option  – USDC Options;
# and for private data:
#    private – Private account data for all markets.
class Bybit:
    def __init__(self, name):
        self.ws = None
        self.ws_private = None
        self.kline = None
        self.ticker = None
        self.position = None
        self.orders = None
        self.execution = None
        self.name = name
        self.session = HTTP(
            testnet=False,
            api_key=api_key,
            api_secret=api_secret,
        )

    def connect(self):
        print(f'connecting name: {self.name}')
        self.ws = WebSocket(
            testnet=False,
            channel_type="linear",
        )

        self.ws_private = WebSocket(
            testnet=False,
            channel_type="private",
            api_key=api_key,
            api_secret=api_secret,
            trace_logging=False,
        )

# Let's fetch the orderbook for BTCUSDT. First, we'll define a function.
    def handle_ticker(self, message):
        # I will be called every time there is new orderbook data!
        # print(message['data']['lastPrice'])
        self.ticker = message["data"]

    def handle_kline(self,message):
        self.kline = message['data']

    def handle_position(self, message):
        self.position = message['data']
        print('---')
        print(self.position)


    def handle_orders(self, message):
        self.orders = message['data']
        print(f'{self.name}  orders: {self.orders}')

    def handle_execution(self, message):
        self.execution = message['data']
        print(self.execution)

# Now, we can subscribe to the orderbook stream and pass our arguments:
# our depth, symbol, and callback function.
    def start_stream(self):
        # self.ws.ticker_stream("LINAUSDT", self.handle_ticker)
        # self.ws.kline_stream(1, 'ETHUSD', self.handle_kline)
        # self.ws_private.position_stream(self.handle_position)
        # self.ws_private.order_stream(self.handle_orders)
        self.ws_private.execution_stream(self.handle_execution)

    def print_s(self):
        if self.ticker:
            last_price = f'{float(self.ticker["lastPrice"]):.2f}'
            last_price = self.ticker["lastPrice"]
            print(last_price)
        return True

#
symbol = 'LINAUSDT'
bb = Bybit('1')
bb.connect()
bb.start_stream()

# result = bb.session.get_instruments_info(category='inverse', symbol=symbol)['result']['list']
# instrument_info = list(filter(lambda x: x['symbol'] == symbol, result))[0]
# result = bb.session.get_positions(category='inverse', symbol=symbol)['result']['list'][0]
# print(result)
# wallet_balance = bb.session.get_wallet_balance(accountType="CONTRACT", coin='ETH')['result']['list'][0]['coin'][0]
# print(wallet_balance)
# orders = bb.session.get_open_orders(category='inverse', symbol=symbol)['result']['list']
# print(orders)
# execution = bb.session.get_executions(category='inverse', symbol=symbol, execType='Trade', limit=2)['result']['list']
# print(execution)
while True:
    # This while loop is required for the program to run. You may execute
    # additional code for your trading logic here.

    if not bb.print_s():
        exit
    sleep(1)