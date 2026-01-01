# -*- coding: utf-8 -*-

from controller import Controller
from loggers import Loggers

if __name__ == '__main__':
    controller = Controller()
    try:
        controller.check_job()
    except Exception as e:
        Loggers.log_exception('{}: {}'.format(type(e), e))
