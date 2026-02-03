"""Tests for gridbot notifier module."""

import time
from datetime import datetime, UTC, timedelta
from unittest.mock import Mock, MagicMock

import pytest

from gridbot.config import TelegramConfig
from gridbot.notifier import Notifier


class TestNotifierNoTelegram:
    """Tests for notifier without Telegram config."""

    def test_create_without_config(self):
        """Test creating notifier without Telegram config."""
        notifier = Notifier()
        assert notifier._bot is None

    def test_alert_logs_without_telegram(self):
        """Test alert logs but doesn't crash without Telegram."""
        notifier = Notifier()
        # Should not raise
        notifier.alert("test message")

    def test_alert_exception_logs_without_telegram(self):
        """Test alert_exception logs traceback without Telegram."""
        notifier = Notifier()
        try:
            raise ValueError("test error")
        except ValueError as e:
            # Should not raise
            notifier.alert_exception("test_context", e)


class TestNotifierWithTelegram:
    """Tests for notifier with Telegram bot injected."""

    @pytest.fixture
    def notifier_with_bot(self):
        """Create a notifier with a mocked bot injected."""
        config = TelegramConfig(bot_token="test_token", chat_id="12345")
        notifier = Notifier()  # no config, so no import attempt
        notifier._telegram_config = config
        notifier._bot = MagicMock()
        return notifier

    def test_alert_sends_telegram(self, notifier_with_bot):
        """Test alert sends message via Telegram."""
        notifier_with_bot.alert("test message")
        time.sleep(0.1)

        notifier_with_bot._bot.send_message.assert_called_once_with(
            chat_id="12345",
            text="test message",
        )

    def test_alert_exception_sends_summary(self, notifier_with_bot):
        """Test alert_exception sends summary via Telegram."""
        try:
            raise ValueError("bad value")
        except ValueError as e:
            notifier_with_bot.alert_exception("_on_ticker", e)

        time.sleep(0.1)

        notifier_with_bot._bot.send_message.assert_called_once()
        call_text = notifier_with_bot._bot.send_message.call_args.kwargs["text"]
        assert "_on_ticker" in call_text
        assert "ValueError" in call_text
        assert "bad value" in call_text


class TestNotifierThrottle:
    """Tests for alert throttling."""

    @pytest.fixture
    def notifier_with_bot(self):
        config = TelegramConfig(bot_token="t", chat_id="c")
        notifier = Notifier(throttle_seconds=60)
        notifier._telegram_config = config
        notifier._bot = MagicMock()
        return notifier

    def test_throttle_blocks_duplicate(self, notifier_with_bot):
        """Test second alert with same key within throttle window is blocked."""
        notifier_with_bot.alert("first", error_key="key1")
        time.sleep(0.05)
        notifier_with_bot.alert("second", error_key="key1")
        time.sleep(0.1)

        # Only one send_message call (first alert)
        assert notifier_with_bot._bot.send_message.call_count == 1

    def test_throttle_allows_different_keys(self, notifier_with_bot):
        """Test alerts with different keys are not throttled."""
        notifier_with_bot.alert("msg1", error_key="key1")
        notifier_with_bot.alert("msg2", error_key="key2")
        time.sleep(0.1)

        assert notifier_with_bot._bot.send_message.call_count == 2

    def test_throttle_allows_after_window(self, notifier_with_bot):
        """Test alert is allowed after throttle window expires."""
        notifier_with_bot.alert("first", error_key="key1")
        time.sleep(0.05)

        # Simulate time passing by backdating the last_sent entry
        notifier_with_bot._last_sent["key1"] = datetime.now(UTC) - timedelta(seconds=61)

        notifier_with_bot.alert("second", error_key="key1")
        time.sleep(0.1)

        assert notifier_with_bot._bot.send_message.call_count == 2

    def test_send_failure_does_not_crash(self, notifier_with_bot):
        """Test Telegram send failure is caught gracefully."""
        notifier_with_bot._bot.send_message.side_effect = Exception("network error")
        # Should not raise
        notifier_with_bot.alert("test")
        time.sleep(0.1)


class TestNotifierInit:
    """Tests for Notifier initialization with real telebot."""

    def test_init_with_telegram_config_creates_bot(self):
        """Test that passing a TelegramConfig creates a bot instance."""
        config = TelegramConfig(bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", chat_id="12345")
        notifier = Notifier(config)
        # telebot is installed, so bot should be created
        assert notifier._bot is not None
