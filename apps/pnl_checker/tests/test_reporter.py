"""Tests for pnl_checker reporter."""

import json
from decimal import Decimal

from pnl_checker.reporter import _redact_config, save_json, print_console
from pnl_checker.config import PnlCheckerConfig
from pnl_checker.comparator import ComparisonResult, PositionComparison, FieldComparison


def _make_config(**overrides):
    data = {
        "account": {"api_key": "real_key_123", "api_secret": "real_secret_456"},
        "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
    }
    data.update(overrides)
    return PnlCheckerConfig(**data)


def _make_comparison():
    return ComparisonResult(
        positions=[
            PositionComparison(
                symbol="BTCUSDT",
                direction="long",
                fields=[
                    FieldComparison(
                        field_name="Unrealized PnL (mark)",
                        bybit_value=Decimal("10"),
                        our_value=Decimal("10"),
                        delta=Decimal("0"),
                        passed=True,
                    ),
                ],
            ),
        ],
        tolerance=0.01,
    )


class TestRedactConfig:
    """Test _redact_config helper."""

    def test_credentials_redacted(self):
        config = _make_config()
        result = _redact_config(config)

        assert result["api_key"] == "[REDACTED]"
        assert result["api_secret"] == "[REDACTED]"

    def test_symbols_preserved(self):
        config = _make_config()
        result = _redact_config(config)

        assert len(result["symbols"]) == 1
        assert result["symbols"][0]["symbol"] == "BTCUSDT"

    def test_funding_max_pages_included(self):
        config = _make_config(funding_max_pages=10)
        result = _redact_config(config)

        assert result["funding_max_pages"] == 10

    def test_tolerance_included(self):
        config = _make_config(tolerance=0.05)
        result = _redact_config(config)

        assert result["tolerance"] == 0.05

    def test_risk_params_included(self):
        config = _make_config()
        result = _redact_config(config)

        assert "risk_params" in result
        assert result["risk_params"]["min_liq_ratio"] == 0.8


class TestSaveJson:
    """Test save_json file output."""

    def test_creates_file(self, tmp_path):
        comparison = _make_comparison()
        config = _make_config()

        filepath = save_json(comparison, config, str(tmp_path))

        assert filepath.startswith(str(tmp_path))
        with open(filepath) as f:
            data = json.load(f)
        assert "timestamp" in data

    def test_includes_config_key(self, tmp_path):
        comparison = _make_comparison()
        config = _make_config()

        filepath = save_json(comparison, config, str(tmp_path))

        with open(filepath) as f:
            data = json.load(f)
        assert "config" in data
        assert data["config"]["api_key"] == "[REDACTED]"

    def test_includes_summary(self, tmp_path):
        comparison = _make_comparison()
        config = _make_config()

        filepath = save_json(comparison, config, str(tmp_path))

        with open(filepath) as f:
            data = json.load(f)
        assert data["summary"]["all_passed"] is True


class TestPrintConsole:
    """Test print_console doesn't crash."""

    def test_doesnt_crash_with_results(self):
        comparison = _make_comparison()
        # Should not raise
        print_console(comparison)

    def test_doesnt_crash_empty(self):
        comparison = ComparisonResult(tolerance=0.01)
        print_console(comparison)
