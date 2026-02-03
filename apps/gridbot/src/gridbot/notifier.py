"""Notification sender with Telegram support and throttling.

Sends alert messages via Telegram with per-error-type throttling
to avoid spam. Thread-safe for use from WebSocket callback threads.
"""

import logging
import threading
import traceback
from datetime import datetime, UTC, timedelta
from typing import Optional

from gridbot.config import TelegramConfig

logger = logging.getLogger(__name__)

# Throttle: max 1 alert per error type per this many seconds
_DEFAULT_THROTTLE_SECONDS = 60


class Notifier:
    """Sends alerts via Telegram with throttling.

    Thread-safe: can be called from WebSocket threads and asyncio tasks.
    Sends Telegram messages in a background thread to avoid blocking.

    If no Telegram config is provided, acts as a no-op (log-only).
    """

    def __init__(
        self,
        telegram_config: Optional[TelegramConfig] = None,
        throttle_seconds: int = _DEFAULT_THROTTLE_SECONDS,
    ):
        self._telegram_config = telegram_config
        self._throttle_seconds = throttle_seconds
        self._bot = None
        self._lock = threading.Lock()
        # Tracks last send time per error key for throttling
        self._last_sent: dict[str, datetime] = {}

        if telegram_config:
            try:
                import telebot

                self._bot = telebot.TeleBot(telegram_config.bot_token)
                logger.info("Telegram notifier initialized")
            except ImportError:
                logger.warning(
                    "pyTelegramBotAPI not installed, Telegram alerts disabled. "
                    "Install with: pip install pyTelegramBotAPI"
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Telegram bot: {e}")

    def alert(self, message: str, error_key: Optional[str] = None) -> None:
        """Send an alert message.

        Always logs. Sends to Telegram if configured and not throttled.

        Args:
            message: Alert text.
            error_key: Key for throttling (e.g., 'ws_callback_error').
                If None, the message itself is used as the key.
        """
        logger.error(f"ALERT: {message}")

        if self._bot is None:
            return

        key = error_key or message
        now = datetime.now(UTC)

        with self._lock:
            last = self._last_sent.get(key)
            if last and (now - last) < timedelta(seconds=self._throttle_seconds):
                return
            self._last_sent[key] = now

        # Send in background thread to avoid blocking
        thread = threading.Thread(
            target=self._send_telegram,
            args=(message,),
            daemon=True,
        )
        thread.start()

    def alert_exception(
        self, context: str, exc: Exception, error_key: Optional[str] = None
    ) -> None:
        """Send an alert for an exception.

        Logs the full traceback, sends a summary to Telegram.

        Args:
            context: Where the error happened (e.g., '_on_ticker').
            exc: The exception.
            error_key: Throttle key. Defaults to context string.
        """
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        logger.error(f"Exception in {context}: {exc}\n{''.join(tb)}")

        key = error_key or context
        self.alert(f"Gridbot: {context} - {type(exc).__name__}: {exc}", error_key=key)

    def _send_telegram(self, message: str) -> None:
        """Send a message via Telegram (runs in background thread)."""
        try:
            self._bot.send_message(
                chat_id=self._telegram_config.chat_id,
                text=message,
            )
        except Exception as e:
            logger.warning(f"Failed to send Telegram message: {e}")
