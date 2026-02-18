"""Tests for recorder CLI entry point."""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from recorder.main import main, cli, setup_logging


class TestSetupLogging:
    def test_configures_console_handler(self):
        root = logging.getLogger()
        initial_handlers = len(root.handlers)

        setup_logging()

        assert len(root.handlers) > initial_handlers
        for handler in root.handlers[initial_handlers:]:
            handler.close()
        root.handlers = root.handlers[:initial_handlers]

    def test_debug_mode_sets_level(self):
        root = logging.getLogger()
        initial_handlers = len(root.handlers)

        setup_logging(debug=True)

        assert root.level == logging.DEBUG
        for handler in root.handlers[initial_handlers:]:
            handler.close()
        root.handlers = root.handlers[:initial_handlers]

    def test_reduces_library_noise(self):
        root = logging.getLogger()
        initial_handlers = len(root.handlers)

        setup_logging()

        assert logging.getLogger("pybit").level == logging.WARNING
        assert logging.getLogger("websocket").level == logging.WARNING

        for handler in root.handlers[initial_handlers:]:
            handler.close()
        root.handlers = root.handlers[:initial_handlers]


class TestMain:
    @pytest.mark.asyncio
    async def test_config_not_found(self):
        with patch("recorder.main.load_config", side_effect=FileNotFoundError("not found")):
            result = await main("/nonexistent/config.yaml")

        assert result == 1

    @pytest.mark.asyncio
    async def test_config_invalid(self):
        with patch("recorder.main.load_config", side_effect=ValueError("bad config")):
            result = await main("/bad/config.yaml")

        assert result == 1

    @pytest.mark.asyncio
    async def test_no_symbols_returns_1(self):
        mock_config = MagicMock()
        mock_config.symbols = []

        with patch("recorder.main.load_config", return_value=mock_config):
            result = await main("test.yaml")

        assert result == 1

    @pytest.mark.asyncio
    async def test_successful_startup_and_shutdown(self):
        mock_config = MagicMock()
        mock_config.symbols = ["BTCUSDT"]
        mock_config.testnet = True
        mock_config.database_url = "sqlite:///test.db"
        mock_config.account = None
        mock_config.health_log_interval = 300.0

        mock_recorder = AsyncMock()

        with patch("recorder.main.load_config", return_value=mock_config), \
             patch("recorder.main.Recorder", return_value=mock_recorder), \
             patch("recorder.main.DatabaseFactory") as MockDB, \
             patch("recorder.main.DatabaseSettings", return_value=MagicMock()):

            MockDB.return_value = MagicMock()
            result = await main("test.yaml")

        assert result == 0
        mock_recorder.start.assert_awaited_once()
        mock_recorder.run_until_shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recorder_error_returns_2(self):
        mock_config = MagicMock()
        mock_config.symbols = ["BTCUSDT"]
        mock_config.testnet = True
        mock_config.database_url = "sqlite:///test.db"
        mock_config.account = None
        mock_config.health_log_interval = 300.0

        mock_recorder = AsyncMock()
        mock_recorder.start.side_effect = Exception("ws failed")

        with patch("recorder.main.load_config", return_value=mock_config), \
             patch("recorder.main.Recorder", return_value=mock_recorder), \
             patch("recorder.main.DatabaseFactory") as MockDB, \
             patch("recorder.main.DatabaseSettings", return_value=MagicMock()):

            MockDB.return_value = MagicMock()
            result = await main("test.yaml")

        assert result == 2
        mock_recorder.stop.assert_awaited_once()


def _close_dangling_coro(mock_run):
    """Close unawaited coroutine from asyncio.run(mock_main(...))."""
    coro = mock_run.call_args[0][0]
    coro.close()


class TestCli:
    def test_parses_config_flag(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["recorder", "--config", "myconfig.yaml"]), \
             patch("recorder.main.setup_logging"), \
             patch("recorder.main.main", new=mock_main), \
             patch("recorder.main.asyncio.run", return_value=0) as mock_run, \
             pytest.raises(SystemExit) as exc_info:

            cli()

        assert exc_info.value.code == 0
        mock_main.assert_called_once_with("myconfig.yaml")
        mock_run.assert_called_once()
        _close_dangling_coro(mock_run)

    def test_parses_debug_flag(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["recorder", "--debug"]), \
             patch("recorder.main.setup_logging") as mock_setup, \
             patch("recorder.main.main", new=mock_main), \
             patch("recorder.main.asyncio.run", return_value=0) as mock_run, \
             pytest.raises(SystemExit):

            cli()

        mock_setup.assert_called_once_with(debug=True)
        _close_dangling_coro(mock_run)

    def test_keyboard_interrupt_returns_130(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["recorder"]), \
             patch("recorder.main.setup_logging"), \
             patch("recorder.main.main", new=mock_main), \
             patch("recorder.main.asyncio.run", side_effect=KeyboardInterrupt) as mock_run, \
             pytest.raises(SystemExit) as exc_info:

            cli()

        assert exc_info.value.code == 130
        _close_dangling_coro(mock_run)

    def test_default_config_is_none(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["recorder"]), \
             patch("recorder.main.setup_logging"), \
             patch("recorder.main.main", new=mock_main), \
             patch("recorder.main.asyncio.run", return_value=0) as mock_run, \
             pytest.raises(SystemExit):

            cli()

        mock_main.assert_called_once_with(None)
        _close_dangling_coro(mock_run)
