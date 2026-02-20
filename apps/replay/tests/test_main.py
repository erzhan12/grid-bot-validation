"""Tests for replay CLI."""

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from replay.main import parse_args, parse_datetime, main


class TestParseArgs:
    """Tests for CLI argument parsing."""

    def test_default_args(self):
        args = parse_args([])
        assert args.config is None
        assert args.database_url is None
        assert args.run_id is None
        assert args.symbol is None
        assert args.start is None
        assert args.end is None
        assert args.output is None
        assert args.debug is False

    def test_all_args(self):
        args = parse_args([
            "--config", "replay.yaml",
            "--database-url", "sqlite:///test.db",
            "--run-id", "abc-123",
            "--symbol", "ETHUSDT",
            "--start", "2025-02-20",
            "--end", "2025-02-23",
            "--output", "results/custom",
            "--debug",
        ])
        assert args.config == "replay.yaml"
        assert args.database_url == "sqlite:///test.db"
        assert args.run_id == "abc-123"
        assert args.symbol == "ETHUSDT"
        assert args.start == "2025-02-20"
        assert args.end == "2025-02-23"
        assert args.output == "results/custom"
        assert args.debug is True


class TestParseDatetime:
    """Tests for datetime parsing."""

    def test_date_only(self):
        dt = parse_datetime("2025-02-20")
        assert dt == datetime(2025, 2, 20)

    def test_date_with_time(self):
        dt = parse_datetime("2025-02-20 14:30:00")
        assert dt == datetime(2025, 2, 20, 14, 30, 0)

    def test_slash_format(self):
        dt = parse_datetime("2025/02/20")
        assert dt == datetime(2025, 2, 20)

    def test_iso_format_with_t(self):
        dt = parse_datetime("2025-02-20T14:30:00")
        assert dt == datetime(2025, 2, 20, 14, 30, 0)

    def test_iso_format_with_utc_offset(self):
        dt = parse_datetime("2025-02-20T14:30:00+00:00")
        assert dt == datetime(2025, 2, 20, 14, 30, 0, tzinfo=timezone.utc)

    def test_iso_format_with_z_suffix(self):
        dt = parse_datetime("2025-02-20T14:30:00Z")
        assert dt == datetime(2025, 2, 20, 14, 30, 0, tzinfo=timezone.utc)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Unable to parse"):
            parse_datetime("not-a-date")


class TestMain:
    """Tests for main() entry point."""

    def test_missing_config_returns_1(self):
        result = main(["--config", "/nonexistent/path.yaml"])
        assert result == 1

    @patch("replay.main.load_config")
    def test_config_override_applied(self, mock_load_config):
        """CLI args override config values."""
        mock_config = MagicMock()
        mock_config.database_url = "sqlite:///test.db"
        mock_config.symbol = "BTCUSDT"
        mock_config.output_dir = "results/replay"
        mock_load_config.return_value = mock_config

        with patch("replay.main.DatabaseFactory"), \
             patch("replay.engine.ReplayEngine") as mock_engine_cls:
            mock_engine_cls.return_value.run.side_effect = ValueError("test stop")

            main([
                "--config", "test.yaml",
                "--symbol", "ETHUSDT",
                "--run-id", "custom-run",
                "--start", "2025-01-01",
                "--end", "2025-01-31",
                "--output", "custom/output",
            ])

        assert mock_config.symbol == "ETHUSDT"
        assert mock_config.run_id == "custom-run"
        assert mock_config.output_dir == "custom/output"
        assert mock_config.start_ts == datetime(2025, 1, 1)
        assert mock_config.end_ts == datetime(2025, 1, 31)

    @patch("replay.main.load_config")
    def test_value_error_returns_1(self, mock_load_config):
        """ValueError (config issues) returns exit code 1."""
        mock_config = MagicMock()
        mock_config.database_url = "sqlite:///test.db"
        mock_config.symbol = "BTCUSDT"
        mock_config.output_dir = "results/replay"
        mock_load_config.return_value = mock_config

        with patch("replay.main.DatabaseFactory"), \
             patch("replay.engine.ReplayEngine") as mock_engine_cls:
            mock_engine_cls.return_value.run.side_effect = ValueError("bad config")
            result = main(["--config", "test.yaml"])

        assert result == 1

    @patch("replay.main.load_config")
    def test_invalid_datetime_returns_1(self, mock_load_config):
        """Invalid datetime in CLI args returns exit code 1 (not traceback)."""
        mock_config = MagicMock()
        mock_config.database_url = "sqlite:///test.db"
        mock_config.symbol = "BTCUSDT"
        mock_config.output_dir = "results/replay"
        mock_load_config.return_value = mock_config

        result = main(["--config", "test.yaml", "--start", "not-a-date"])

        assert result == 1

    @patch("replay.main.load_config")
    def test_runtime_error_returns_2(self, mock_load_config):
        """Runtime exceptions return exit code 2."""
        mock_config = MagicMock()
        mock_config.database_url = "sqlite:///test.db"
        mock_config.symbol = "BTCUSDT"
        mock_config.output_dir = "results/replay"
        mock_load_config.return_value = mock_config

        with patch("replay.main.DatabaseFactory"), \
             patch("replay.engine.ReplayEngine") as mock_engine_cls:
            mock_engine_cls.return_value.run.side_effect = RuntimeError("crash")
            result = main(["--config", "test.yaml"])

        assert result == 2
