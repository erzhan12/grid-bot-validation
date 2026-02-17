"""Tests for gridbot main entry point."""

import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gridbot.main import main, cli, setup_logging


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def test_configures_console_handler(self):
        root = logging.getLogger()
        initial_handlers = len(root.handlers)

        setup_logging()

        assert len(root.handlers) > initial_handlers
        # Clean up — close handlers before removing to avoid file-handle leaks
        for handler in root.handlers[initial_handlers:]:
            handler.close()
        root.handlers = root.handlers[:initial_handlers]

    def test_json_file_handler(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        root = logging.getLogger()
        initial_handlers = len(root.handlers)

        setup_logging(json_file=log_file)

        assert len(root.handlers) > initial_handlers + 1  # console + file
        # Clean up — close handlers before removing to avoid file-handle leaks
        for handler in root.handlers[initial_handlers:]:
            handler.close()
        root.handlers = root.handlers[:initial_handlers]

    def test_reduces_library_noise(self):
        root = logging.getLogger()
        initial_handlers = len(root.handlers)

        setup_logging()

        assert logging.getLogger("pybit").level == logging.WARNING
        assert logging.getLogger("websocket").level == logging.WARNING

        # Clean up — close handlers before removing to avoid file-handle leaks
        for handler in root.handlers[initial_handlers:]:
            handler.close()
        root.handlers = root.handlers[:initial_handlers]


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    @pytest.mark.asyncio
    async def test_config_not_found(self):
        with patch("gridbot.main.load_config", side_effect=FileNotFoundError("not found")):
            result = await main("/nonexistent/config.yaml")

        assert result == 1

    @pytest.mark.asyncio
    async def test_config_invalid(self):
        with patch("gridbot.main.load_config", side_effect=ValueError("bad config")):
            result = await main("/bad/config.yaml")

        assert result == 1

    @pytest.mark.asyncio
    async def test_successful_startup_and_shutdown(self):
        mock_config = MagicMock()
        mock_config.strategies = [MagicMock()]
        mock_config.database_url = None
        mock_config.notification = None

        mock_orchestrator = AsyncMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.asyncio.Event") as MockEvent:

            # Make shutdown_event.wait() return immediately
            mock_event = MagicMock()
            mock_event.wait = AsyncMock()
            MockEvent.return_value = mock_event

            result = await main("test.yaml")

        assert result == 0
        mock_orchestrator.start.assert_awaited_once()
        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_startup_error_returns_1(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = None
        mock_config.notification = None

        mock_orchestrator = AsyncMock()
        mock_orchestrator.start.side_effect = Exception("startup failed")

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"):

            result = await main("test.yaml")

        assert result == 1
        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_database_init_with_sqlite_url(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = "sqlite:///test.db"
        mock_config.notification = None

        mock_orchestrator = AsyncMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.DatabaseFactory") as MockDB, \
             patch("gridbot.main.DatabaseSettings") as MockSettings, \
             patch("gridbot.main.asyncio.Event") as MockEvent:

            mock_event = MagicMock()
            mock_event.wait = AsyncMock()
            MockEvent.return_value = mock_event
            MockSettings.return_value = MagicMock()

            result = await main("test.yaml")

        assert result == 0
        MockDB.assert_called_once()

    @pytest.mark.asyncio
    async def test_database_init_failure_is_warning(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = "sqlite:///test.db"
        mock_config.notification = None

        mock_orchestrator = AsyncMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.DatabaseFactory", side_effect=Exception("db error")), \
             patch("gridbot.main.DatabaseSettings", return_value=MagicMock()), \
             patch("gridbot.main.asyncio.Event") as MockEvent:

            mock_event = MagicMock()
            mock_event.wait = AsyncMock()
            MockEvent.return_value = mock_event

            result = await main("test.yaml")

        # DB failure is a warning, not fatal
        assert result == 0

    @pytest.mark.asyncio
    async def test_notifier_created_with_telegram_config(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = None
        mock_config.notification.telegram = MagicMock()

        mock_orchestrator = AsyncMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier") as MockNotifier, \
             patch("gridbot.main.asyncio.Event") as MockEvent:

            mock_event = MagicMock()
            mock_event.wait = AsyncMock()
            MockEvent.return_value = mock_event

            await main("test.yaml")

        MockNotifier.assert_called_once_with(mock_config.notification.telegram)


# ---------------------------------------------------------------------------
# cli()
# ---------------------------------------------------------------------------


class TestCli:
    def test_parses_config_flag(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["gridbot", "--config", "myconfig.yaml"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", new=mock_main), \
             patch("gridbot.main.asyncio.run", return_value=0) as mock_run, \
             pytest.raises(SystemExit) as exc_info:

            cli()

        assert exc_info.value.code == 0
        mock_main.assert_called_once_with("myconfig.yaml")
        mock_run.assert_called_once()
        # Close dangling coroutine from asyncio.run(mock_main(...))
        coro = mock_run.call_args[0][0]
        coro.close()

    def test_parses_debug_flag(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["gridbot", "--debug"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", new=mock_main), \
             patch("gridbot.main.asyncio.run", return_value=0) as mock_run, \
             pytest.raises(SystemExit):

            cli()

        assert logging.getLogger("gridbot").level == logging.DEBUG
        # Close dangling coroutine
        coro = mock_run.call_args[0][0]
        coro.close()

    def test_keyboard_interrupt_returns_130(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["gridbot"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", new=mock_main), \
             patch("gridbot.main.asyncio.run", side_effect=KeyboardInterrupt) as mock_run, \
             pytest.raises(SystemExit) as exc_info:

            cli()

        assert exc_info.value.code == 130
        # Close dangling coroutine
        coro = mock_run.call_args[0][0]
        coro.close()

    def test_default_config_is_none(self):
        mock_main = AsyncMock(return_value=0)
        with patch("sys.argv", ["gridbot"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", new=mock_main), \
             patch("gridbot.main.asyncio.run", return_value=0) as mock_run, \
             pytest.raises(SystemExit):

            cli()

        mock_main.assert_called_once_with(None)
        # Close dangling coroutine
        coro = mock_run.call_args[0][0]
        coro.close()
