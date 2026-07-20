"""Tests for importer CLI parsing and datetime discipline (feature 0093)."""

from datetime import datetime

import pytest

from importer.config import build_parser, parse_utc, to_naive_utc

_BASE_ARGS = [
    "--source", "db",
    "--source-url", "sqlite:///src.db",
]


class TestConfig:
    def test_symbols_required(self):
        """Parser rejects an invocation without --symbols."""
        with pytest.raises(SystemExit):
            build_parser().parse_args(_BASE_ARGS)

    def test_symbols_parsed_and_uppercased(self):
        """Comma-separated symbols are split, stripped and uppercased."""
        args = build_parser().parse_args(
            _BASE_ARGS + ["--symbols", "btcusdt, ethusdt"]
        )
        assert args.symbols == ["BTCUSDT", "ETHUSDT"]

    def test_source_choice_validated(self):
        """--source outside {db,http} is rejected."""
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["--source", "ftp", "--source-url", "x", "--symbols", "BTCUSDT"]
            )

    def test_naive_iso_parsed_as_utc(self):
        """Naive ISO input is taken as UTC unchanged."""
        assert parse_utc("2026-07-01T12:30:00") == datetime(2026, 7, 1, 12, 30)

    def test_z_suffix_converted_to_naive(self):
        """Aware ...Z input is converted to UTC and returned naive."""
        result = parse_utc("2026-07-01T12:30:00Z")
        assert result == datetime(2026, 7, 1, 12, 30)
        assert result.tzinfo is None

    def test_offset_converted_to_naive_utc(self):
        """+05:00 input shifts to UTC and strips tzinfo (TypeError hazard)."""
        result = parse_utc("2026-07-01T12:30:00+05:00")
        assert result == datetime(2026, 7, 1, 7, 30)
        assert result.tzinfo is None

    def test_invalid_datetime_rejected(self):
        """Garbage datetime input fails argparse validation."""
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                _BASE_ARGS + ["--symbols", "BTCUSDT", "--start", "not-a-date"]
            )

    def test_to_naive_utc_passthrough(self):
        """Naive datetimes pass through to_naive_utc unchanged."""
        dt = datetime(2026, 7, 1, 12, 0)
        assert to_naive_utc(dt) is dt

    def test_batch_size_must_be_positive(self):
        """--batch-size 0 / negative are rejected (LIMIT 0/-1 footguns)."""
        for bad in ("0", "-1"):
            with pytest.raises(SystemExit):
                build_parser().parse_args(
                    _BASE_ARGS + ["--symbols", "BTCUSDT", "--batch-size", bad]
                )

    def test_ohlc_threshold_must_be_unit_fraction(self):
        """--ohlc-threshold outside (0, 1] is rejected."""
        for bad in ("0", "-0.5", "1.5"):
            with pytest.raises(SystemExit):
                build_parser().parse_args(
                    _BASE_ARGS + ["--symbols", "BTCUSDT", "--ohlc-threshold", bad]
                )
        args = build_parser().parse_args(
            _BASE_ARGS + ["--symbols", "BTCUSDT", "--ohlc-threshold", "0.9"]
        )
        assert args.ohlc_threshold == 0.9
