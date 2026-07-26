"""Tests for source-aware importer CLI and secret preflight."""

from datetime import datetime

import pytest

from importer.config import (
    ConfigurationError,
    build_parser,
    load_market_data_api_key,
    parse_utc,
    preflight_http,
    to_naive_utc,
)

_BASE_ARGS = [
    "--source", "db",
    "--source-url", "sqlite:///src.db",
]
_HTTP_ARGS = [
    "--source",
    "http",
    "--source-url",
    "https://EXAMPLE.com:443/deploy/",
    "--symbols",
    "BtCuSdT",
    "--start",
    "2026-07-01T00:00:00+05:00",
    "--end",
    "2026-07-02T00:00:00+05:00",
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

    def test_http_symbol_case_and_offset_bounds_are_preserved_normalized(self):
        args = build_parser().parse_args(_HTTP_ARGS)
        assert args.symbols == ["BtCuSdT"]
        assert args.start == datetime(2026, 6, 30, 19)
        assert args.end == datetime(2026, 7, 1, 19)
        assert args.http_base_url == "https://example.com/deploy"
        assert args.http_origin_label == "http:https://example.com"

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

    def test_http_batch_is_capped_but_db_is_not(self):
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(_HTTP_ARGS + ["--batch-size", "10001"])
        assert caught.value.code == 2
        db = build_parser().parse_args(
            _BASE_ARGS
            + ["--symbols", "btcusdt", "--batch-size", "10001"]
        )
        assert db.batch_size == 10001

    @pytest.mark.parametrize(
        "extra",
        [
            [],
            ["--start", "2026-07-01T00:00:00Z"],
        ],
    )
    def test_http_requires_both_bounds(self, extra):
        args = [
            "--source",
            "http",
            "--source-url",
            "https://example.test",
            "--symbols",
            "BTCUSDT",
            *extra,
        ]
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(args)
        assert caught.value.code == 2

    def test_http_reversed_bounds_and_control_symbol_rejected_without_echo(
        self, capsys
    ):
        reversed_args = _HTTP_ARGS.copy()
        reversed_args[reversed_args.index("--start") + 1] = (
            "2026-07-03T00:00:00Z"
        )
        with pytest.raises(SystemExit):
            build_parser().parse_args(reversed_args)
        unsafe = _HTTP_ARGS.copy()
        unsafe[unsafe.index("--symbols") + 1] = "BTC\nSECRET"
        with pytest.raises(SystemExit):
            build_parser().parse_args(unsafe)
        assert "BTC\nSECRET" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "url",
        [
            " example.test",
            "ftp://example.test",
            "https://user:password@example.test",
            "https://example.test?",
            "https://example.test#",
            "https://example.test:99999",
            "https://[::1",
            "https://example.test/\npath",
        ],
    )
    def test_invalid_http_urls_are_safe_parser_errors(self, url, capsys):
        args = _HTTP_ARGS.copy()
        args[args.index("--source-url") + 1] = url
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(args)
        assert caught.value.code == 2
        assert url not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "left,right",
        [
            ("HTTPS://EXAMPLE.COM:443/api/", "https://example.com/api"),
            (
                "https://bücher.example/api",
                "https://xn--bcher-kva.example/api",
            ),
            ("https://example.com/a/../api", "https://example.com/api"),
            ("https://example.com/api/%7e", "https://example.com/api/~"),
        ],
    )
    def test_canonical_url_equivalence(self, left, right):
        assert preflight_http(left) == preflight_http(right)

    def test_distinct_base_paths_and_ports_remain_distinct(self):
        assert preflight_http("https://example.com/a") != preflight_http(
            "https://example.com/b"
        )
        assert preflight_http("https://example.com:444/a") != preflight_http(
            "https://example.com/a"
        )

    @pytest.mark.parametrize(
        "key",
        [" secret", "secret ", "a\nb", "a:b", "é", "abc=def"],
    )
    def test_invalid_bearer_values_never_echo(self, key, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_API_KEY", key)
        with pytest.raises(ConfigurationError) as caught:
            load_market_data_api_key()
        assert key not in str(caught.value)
        assert caught.value.__cause__ is None

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_or_empty_bearer_disables_auth(self, value, monkeypatch):
        if value is None:
            monkeypatch.delenv("MARKET_DATA_API_KEY", raising=False)
        else:
            monkeypatch.setenv("MARKET_DATA_API_KEY", value)
        assert load_market_data_api_key() is None

    def test_valid_bearer_is_preserved_exactly(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_API_KEY", "opaque-token/+=")
        assert load_market_data_api_key() == "opaque-token/+="

    def test_bearer_rejects_excessive_padding(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_API_KEY", "opaque-token/+===")
        with pytest.raises(ConfigurationError) as caught:
            load_market_data_api_key()
        assert caught.value.__cause__ is None
        assert "opaque-token" not in str(caught.value)

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
