import logging
import os
import traceback
from datetime import datetime
from TelegramExcBot import TelegramExcBot


class Loggers:
    logger_check = None
    logger_orders = None
    logger_exceptions = None
    dir = ''
    log_day = ''
    LOG_DAY_FORMAT = '%y%m%d'

    @staticmethod
    def init_loggers():
        Loggers.dir = Loggers.init_dir()
        Loggers.basic_config()
        Loggers.logger_orders = Loggers.init_logger('orders', '%(asctime)s: %(message)s')
        Loggers.logger_check = Loggers.init_logger('check', '%(asctime)s: %(message)s')
        Loggers.logger_exceptions = Loggers.init_logger('exceptions', '%(asctime)s: %(message)s')

    @staticmethod
    def init_logger(logger_name, logger_format):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)

        filename = Loggers.dir + logger_name + '.log'
        handler = logging.FileHandler(filename=filename, mode='a')
        handler.setLevel(logging.INFO)

        formatter = logging.Formatter(logger_format)

        handler.setFormatter(formatter)
        logger.addHandler(handler)
        return logger

    @staticmethod
    def basic_config():
        filename = Loggers.dir + 'log.log'
        logger_format = '%(asctime)s: %(message)s'
        logger = logging.root

        logger.setLevel(logging.DEBUG)

        handler = logging.FileHandler(filename=filename, mode='a')
        handler.setLevel(logging.INFO)

        formatter = logging.Formatter(logger_format)

        handler.setFormatter(formatter)
        logger.addHandler(handler)

    @staticmethod
    def init_dir():
        Loggers.log_day = datetime.utcnow().strftime(Loggers.LOG_DAY_FORMAT)
        directory = 'logs/{}/{}/{}/'.format(datetime.utcnow().year, datetime.utcnow().month, Loggers.log_day)
        Loggers.ensure_dir(directory)
        return directory

    @staticmethod
    def ensure_dir(f):
        d = os.path.dirname(f)
        if not os.path.exists(d):
            os.makedirs(d)

    @staticmethod
    def check_new_day():
        day = datetime.now().strftime(Loggers.LOG_DAY_FORMAT)
        if day != Loggers.log_day:
            Loggers.init_loggers()

    @staticmethod
    def log_exception(message):
        Loggers.logger_exceptions.info(message)
        Loggers.logger_exceptions.info(traceback.format_exc())
        TelegramExcBot.send_message('Bbu: {}'.format(message))

    @staticmethod
    def log_order(message):
        Loggers.logger_orders.info(message)
        TelegramExcBot.send_message('Bbu: {}'.format(message))
