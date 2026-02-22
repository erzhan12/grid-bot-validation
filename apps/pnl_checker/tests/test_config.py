"""Tests for pnl_checker config loading and validation."""

from decimal import Decimal

import pytest
import yaml

from pnl_checker.config import PnlCheckerConfig, load_config


class TestPnlCheckerConfigDefaults:
    """Test default values on PnlCheckerConfig."""

    def _minimal_config(self, **overrides):
        data = {
            "account": {"api_key": "k", "api_secret": "s"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }
        data.update(overrides)
        return PnlCheckerConfig(**data)

    def test_default_tolerance(self):
        cfg = self._minimal_config()
        assert cfg.tolerance == 0.01

    def test_default_funding_max_pages(self):
        cfg = self._minimal_config()
        assert cfg.funding_max_pages == 20

    def test_custom_funding_max_pages(self):
        cfg = self._minimal_config(funding_max_pages=5)
        assert cfg.funding_max_pages == 5

    def test_funding_max_pages_must_be_positive(self):
        with pytest.raises(Exception):
            self._minimal_config(funding_max_pages=0)

    def test_default_risk_params(self):
        cfg = self._minimal_config()
        assert cfg.risk_params.min_liq_ratio == 0.8
        assert cfg.risk_params.max_margin == 8.0

    def test_tick_size_from_string(self):
        cfg = self._minimal_config()
        assert cfg.symbols[0].tick_size == Decimal("0.1")

    def test_tick_size_from_float(self):
        """tick_size can be provided as a float and gets converted."""
        data = {
            "account": {"api_key": "k", "api_secret": "s"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": 0.01}],
        }
        cfg = PnlCheckerConfig(**data)
        assert cfg.symbols[0].tick_size == Decimal("0.01")


class TestLoadConfig:
    """Test YAML loading via load_config()."""

    def test_loads_from_path(self, tmp_path):
        config_data = {
            "account": {"api_key": "mykey", "api_secret": "mysecret"},
            "symbols": [{"symbol": "ETHUSDT", "tick_size": "0.01"}],
            "tolerance": 0.05,
            "funding_max_pages": 10,
        }
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))

        assert cfg.account.api_key == "mykey"
        assert cfg.tolerance == 0.05
        assert cfg.funding_max_pages == 10
        assert len(cfg.symbols) == 1
        assert cfg.symbols[0].symbol == "ETHUSDT"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_env_var_path(self, tmp_path, monkeypatch):
        config_data = {
            "account": {"api_key": "envkey", "api_secret": "envsecret"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }
        config_file = tmp_path / "env_config.yaml"
        config_file.write_text(yaml.dump(config_data))

        monkeypatch.setenv("PNL_CHECKER_CONFIG_PATH", str(config_file))

        cfg = load_config()

        assert cfg.account.api_key == "envkey"
