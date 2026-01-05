# import telebot
import threading
from settings import Settings

# TODO Get rid of staticmethods


class TelegramExcBot:
    token = None
    chat_id = None
    bot = None

    @staticmethod
    def init_token():
        if TelegramExcBot.token is None:
            TelegramExcBot.token = Settings.telegram['token']
            TelegramExcBot.chat_id = Settings.telegram['chat_id']
            # TelegramExcBot.bot = telebot.TeleBot(TelegramExcBot.token)

    @staticmethod
    def send_message(text, chat_id=None, token=None):
        TelegramExcBot.init_token()
        if chat_id is None:
            chat_id = TelegramExcBot.chat_id
        # if token is not None:
            # bot = telebot.TeleBot(token)
        # else:
        #     bot = TelegramExcBot.bot
        # kwargs = {
        #     'chat_id': chat_id,
        #     'text': text
        # }
        # TelegramExcBot.__start_parallel_job(bot.send_message, **kwargs)

    @staticmethod
    def __start_parallel_job(job, **kwargs):
        try:
            t = threading.Thread(target=job, kwargs=kwargs)
            t.start()
        except Exception:
            pass
