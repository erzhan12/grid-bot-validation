"""Tests for gridbot main entry point."""

import logging
from unittest.mock import MagicMock, patch

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
    def test_config_not_found(self):
        with patch("gridbot.main.load_config", side_effect=FileNotFoundError("not found")):
            result = main("/nonexistent/config.yaml")

        assert result == 1

    def test_config_invalid(self):
        with patch("gridbot.main.load_config", side_effect=ValueError("bad config")):
            result = main("/bad/config.yaml")

        assert result == 1

    def test_successful_startup_and_shutdown(self):
        mock_config = MagicMock()
        mock_config.strategies = [MagicMock()]
        mock_config.database_url = None
        mock_config.notification = None

        mock_orchestrator = MagicMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.signal.signal"):

            result = main("test.yaml")

        assert result == 0
        mock_orchestrator.start.assert_called_once()
        mock_orchestrator.run.assert_called_once()
        mock_orchestrator.stop.assert_called_once()

    def test_startup_error_returns_1(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = None
        mock_config.notification = None

        mock_orchestrator = MagicMock()
        mock_orchestrator.start.side_effect = Exception("startup failed")

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.signal.signal"):

            result = main("test.yaml")

        assert result == 1
        mock_orchestrator.stop.assert_called_once()

    def test_database_init_with_sqlite_url(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = "sqlite:///test.db"
        mock_config.notification = None

        mock_orchestrator = MagicMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.DatabaseFactory") as MockDB, \
             patch("gridbot.main.DatabaseSettings") as MockSettings, \
             patch("gridbot.main.signal.signal"):

            MockSettings.return_value = MagicMock()

            result = main("test.yaml")

        assert result == 0
        MockDB.assert_called_once()

    def test_database_init_failure_is_warning(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = "sqlite:///test.db"
        mock_config.notification = None

        mock_orchestrator = MagicMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.DatabaseFactory", side_effect=Exception("db error")), \
             patch("gridbot.main.DatabaseSettings", return_value=MagicMock()), \
             patch("gridbot.main.signal.signal"):

            result = main("test.yaml")

        # DB failure is a warning, not fatal
        assert result == 0

    def test_notifier_created_with_telegram_config(self):
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = None
        mock_config.notification.telegram = MagicMock()

        mock_orchestrator = MagicMock()

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier") as MockNotifier, \
             patch("gridbot.main.signal.signal"):

            main("test.yaml")

        MockNotifier.assert_called_once_with(mock_config.notification.telegram)

    def test_signal_handler_calls_request_stop(self):
        """Verify the installed SIGINT handler triggers orchestrator.request_stop()."""
        mock_config = MagicMock()
        mock_config.strategies = []
        mock_config.database_url = None
        mock_config.notification = None

        mock_orchestrator = MagicMock()
        captured_handlers = {}

        def fake_signal(sig, handler):
            captured_handlers[sig] = handler

        with patch("gridbot.main.load_config", return_value=mock_config), \
             patch("gridbot.main.Orchestrator", return_value=mock_orchestrator), \
             patch("gridbot.main.Notifier"), \
             patch("gridbot.main.signal.signal", side_effect=fake_signal):

            main("test.yaml")

        # Invoke the captured SIGINT handler — must call request_stop()
        import signal as _signal
        handler = captured_handlers[_signal.SIGINT]
        handler(_signal.SIGINT, None)
        mock_orchestrator.request_stop.assert_called_once()


# ---------------------------------------------------------------------------
# cli()
# ---------------------------------------------------------------------------


class TestCli:
    def test_parses_config_flag(self):
        with patch("sys.argv", ["gridbot", "--config", "myconfig.yaml"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", return_value=0) as mock_main, \
             pytest_raises_system_exit() as exc_info:

            cli()

        assert exc_info.value.code == 0
        mock_main.assert_called_once_with("myconfig.yaml")

    def test_parses_debug_flag(self):
        with patch("sys.argv", ["gridbot", "--debug"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", return_value=0), \
             pytest_raises_system_exit():

            cli()

        assert logging.getLogger("gridbot").level == logging.DEBUG

    def test_keyboard_interrupt_returns_130(self):
        with patch("sys.argv", ["gridbot"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", side_effect=KeyboardInterrupt), \
             pytest_raises_system_exit() as exc_info:

            cli()

        assert exc_info.value.code == 130

    def test_default_config_is_none(self):
        with patch("sys.argv", ["gridbot"]), \
             patch("gridbot.main.setup_logging"), \
             patch("gridbot.main.main", return_value=0) as mock_main, \
             pytest_raises_system_exit():

            cli()

        mock_main.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def pytest_raises_system_exit():
    """Shim so the with-chain above stays readable. Returns a pytest.raises(SystemExit)."""
    import pytest
    return pytest.raises(SystemExit)
