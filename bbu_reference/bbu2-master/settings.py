import yaml
import os


class Settings(object):

    yaml = dict()
    keys = dict()
    server = dict()
    # CONSTANTS = dict()
    INTERVALS = dict()

    yamlfiles = ['conf/config.yaml', '../conf/config.yaml', '/opt/bbbenv/bbb/conf/config.yaml']
    keysfiles = ['conf/keys.yaml', '../conf/keys.yaml', '/opt/bbbenv/bbb/conf/keys.yaml']
    serverfiles = ['conf/server_config.yaml', '../conf/server_config.yaml', '/opt/bbbenv/bbb/conf/server_config.yaml']

    # JSON_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'
    # JSON_DATETIME_FORMAT1 = '%Y-%m-%dT%H:%M:%SZ'

    bm_keys = []
    default_key = {}
    amounts = []
    # bb_keys = []

    # START = 0
    # PERIOD = 0

    pair_timeframes = []

    # BUY = 'buy'
    # SELL = 'sell'

    # STATUS_BOUGHT = 'bought'
    # STATUS_SOLD = 'sold'

    DEBUG = False

    ERROR_PRICE = 0.13

    # users = dict()
    # AMOUNT = ''

    telegram = {}

    @staticmethod
    def read_settings():
        Settings.init()

    @staticmethod
    def init():

        Settings.yaml = Settings.__read_yaml()
        Settings.keys = Settings.__read_keys()
        Settings.server = Settings.__read_server()

        check_config = Settings.yaml['check']
        # Settings.START                          = int(check_config['start'])
        # Settings.PERIOD                         = int(check_config['period'])

        interval_config = Settings.yaml['intervals']
        Settings.INTERVALS['CHECK']           = float(interval_config['check'])

        Settings.bm_keys = Settings.keys['bm_keys']
        Settings.default_key = Settings.keys['default_key']
        Settings.amounts = Settings.yaml['amounts']

        Settings.DEBUG = Settings.server['debug']

        Settings.pair_timeframes = Settings.yaml['pair_timeframes']

        Settings.telegram = Settings.keys['telegram']

        Settings.__add_default()

    @staticmethod
    def __add_default():
        for bm in Settings.bm_keys:
            if 'exchange' not in bm:
                bm['exchange'] = 'bybit_usdt'
            if 'key' not in bm:
                bm['key'] = Settings.default_key['key']
            if 'secret' not in bm:
                bm['secret'] = Settings.default_key['secret']

        for pt in Settings.pair_timeframes:
            if 'exchange' not in pt:
                pt['exchange'] = 'bybit_usdt'
            if 'max_margin' not in pt:
                pt['max_margin'] = 5
            if 'greed_step' not in pt:
                pt['greed_step'] = 0.2
            if 'greed_count' not in pt:
                pt['greed_count'] = 40
            if 'direction' not in pt:
                pt['direction'] = 'long'
            if 'min_liq_ratio' not in pt:
                pt['min_liq_ratio'] = 0.8
            if 'max_liq_ratio' not in pt:
                pt['max_liq_ratio'] = 1.2
            if 'min_total_margin' not in pt:
                pt['min_total_margin'] = 0
            if 'long_koef' not in pt:
                pt['long_koef'] = 1.0
            if 'increase_same_position_on_low_margin' not in pt:
                pt['increase_same_position_on_low_margin'] = False

        for am in Settings.amounts:
            if 'is_testnet' not in am:
                am['is_testnet'] = False

    @staticmethod
    def __read_yaml():
        Settings.__add_realpath(Settings.yamlfiles)
        data = ''
        for filename in Settings.yamlfiles:
            try:
                with open(filename, 'r') as stream:
                    data = yaml.load(stream, yaml.FullLoader)
                    return data
            except OSError:
                continue
        return data

    @staticmethod
    def __read_server():
        Settings.__add_realpath(Settings.serverfiles)
        data = ''
        for filename in Settings.serverfiles:
            try:
                with open(filename, 'r') as stream:
                    data = yaml.load(stream, yaml.FullLoader)
                    return data
            except OSError:
                continue
        return data

    @staticmethod
    def __read_keys():
        Settings.__add_realpath(Settings.keysfiles)
        data = ''
        for filename in Settings.keysfiles:
            try:
                with open(filename, 'r') as stream:
                    data = yaml.load(stream, yaml.FullLoader)
                    return data
            except OSError:
                continue
        return data

    @staticmethod
    def __add_realpath(files):
        realfiles = list()
        for file in files:
            realfile = os.path.realpath(file)
            realfiles.append(realfile)
        files.extend(realfiles)