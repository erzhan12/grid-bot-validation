# from settings import Settings
import time
# import datetime
# from loggers import Loggers
# from datetime import datetime
# from controller import Controller
# from db_files import DbFiles
# import bybit

# Settings.read_settings()
# Loggers.init_loggers()
from pybit import WebSocket
key = 'socket_key'
secret = 'socket_secret'


symbol = 'ETHUSD'
subs = [
    # "klineV2.1.ETHUSD",
    # "candle.1.BSWUSDT",
    # "order"
    # "execution",  # use to get last filled
    "position"
]
ws = WebSocket(
    # "wss://stream.bybit.com/realtime",
    # "wss://stream.bybit.com/realtime_public",
    "wss://stream.bybit.com/realtime_private",
    api_key=key,
    api_secret=secret,
    subscriptions=subs
)
while True:
    data = ws.fetch(subs[0])
    time.sleep(1)
#     print(datetime.now())
#     ws.ping()
    if data:
        try:
#             # print(f"{data['BTUSD']['size']}, {data['BTCUSD']['entry_price']}")
            print(data)
#             time.sleep(10)
        except (KeyError, ValueError):
            pass
# print('Go')
# time.sleep(1)
# from BybitWebsocket import BybitWebsocket
#
# ws = BybitWebsocket(wsURL="wss://stream.bybit.com/realtime",
#                     api_key=key, api_secret=secret)
# ws.subscribe_position()
# while True:
#     data = ws.get_data("position")
#     if data:
#         print(data)
# from pybit import HTTP
# session = HTTP("https://api.bybit.com",
#                api_key=key, api_secret=secret)
# r = session.my_position(symbol=symbol)['result']
# ration = float(r['position_value']) / float(r['wallet_balance'])
# print(ration)
# print(float(r['position_margin'])*100)
# orders = session.query_active_order(  # Active orders
#             symbol="BTCUSD",
# )
# orders = session.get_active_order(  # Orders history
#         symbol="BTCUSD",
#         order_status='Filled',
#         # limit=1
# )['result']['data']
# orders = sorted(orders, key=lambda d: d['updated_at'], reverse=True)
# r = session.place_active_order(symbol='BTCUSD', side='Buy', order_type='Limit',
#                                qty=1, price=60000, time_in_force='GoodTillCancel')
# print(len(orders['result']))

# ord_type = 'Limit'
# limits = list(filter(lambda x: x['order_type'] == ord_type and
#                                x['order_status'] in ['Active', 'New', 'PartiallyFilled'], orders['result']))
# print(limits)
# import bybit
# bybit = bybit.bybit(test=False, api_key=key, api_secret=secret)
# symbol = 'BTCUSD'
# r = bybit.Order.Order_getOrders(symbol=symbol).result()
# try:
#     data = r[0]['result']['data']
#     limits = list(filter(lambda x: x['order_type'] == ord_type and
#                                    x['order_status'] in ['Active', 'New', 'PartiallyFilled'], data))
# except:
#     pass
# print(limits)