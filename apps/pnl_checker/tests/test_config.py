"""Tests for pnl_checker config loading and validation."""

from decimal import Decimal

import pytest
import yaml

from pnl_checker.config import PnlCheckerConfig, load_config


class TestPnlCheckerConfigDefaults:
    """Test default values on PnlCheckerConfig."""

    def _minimal_config(self, **overrides):
        data = {
            "account": {"api_key": "test_key_0123456789", "api_secret": "test_secret_0123456789"},
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
            "account": {"api_key": "test_key_0123456789", "api_secret": "test_secret_0123456789"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": 0.01}],
        }
        cfg = PnlCheckerConfig(**data)
        assert cfg.symbols[0].tick_size == Decimal("0.01")

    def test_tick_size_zero_rejected(self):
        with pytest.raises(Exception):
            self._minimal_config(symbols=[{"symbol": "BTCUSDT", "tick_size": "0"}])

    def test_tick_size_negative_rejected(self):
        with pytest.raises(Exception):
            self._minimal_config(symbols=[{"symbol": "BTCUSDT", "tick_size": "-0.1"}])


class TestLoadConfig:
    """Test YAML loading via load_config()."""

    def test_loads_from_path(self, tmp_path):
        config_data = {
            "account": {"api_key": "mykey_0123456789", "api_secret": "mysecret_0123456789"},
            "symbols": [{"symbol": "ETHUSDT", "tick_size": "0.01"}],
            "tolerance": 0.05,
            "funding_max_pages": 10,
        }
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))

        assert cfg.account.api_key == "mykey_0123456789"
        assert cfg.tolerance == 0.05
        assert cfg.funding_max_pages == 10
        assert len(cfg.symbols) == 1
        assert cfg.symbols[0].symbol == "ETHUSDT"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_env_var_path(self, tmp_path, monkeypatch):
        config_data = {
            "account": {"api_key": "envkey_0123456789", "api_secret": "envsecret_0123456789"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }
        config_file = tmp_path / "env_config.yaml"
        config_file.write_text(yaml.dump(config_data))

        monkeypatch.setenv("PNL_CHECKER_CONFIG_PATH", str(config_file))

        cfg = load_config()

        assert cfg.account.api_key == "envkey_0123456789"


class TestAccountEnvVars:
    """Test environment variable overrides for credentials."""

    def _minimal_data(self):
        return {
            "account": {"api_key": "yaml_key_0123456789", "api_secret": "yaml_secret_0123456789"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }

    def test_env_vars_override_yaml(self, monkeypatch):
        monkeypatch.setenv("BYBIT_API_KEY", "env_key_0123456789")
        monkeypatch.setenv("BYBIT_API_SECRET", "env_secret_0123456789")
        cfg = PnlCheckerConfig(**self._minimal_data())

        assert cfg.account.api_key == "env_key_0123456789"
        assert cfg.account.api_secret == "env_secret_0123456789"

    def test_partial_env_var_overrides_only_that_field(self, monkeypatch):
        monkeypatch.setenv("BYBIT_API_KEY", "env_key_0123456789")
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        cfg = PnlCheckerConfig(**self._minimal_data())

        assert cfg.account.api_key == "env_key_0123456789"
        assert cfg.account.api_secret == "yaml_secret_0123456789"

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        data = {
            "account": {},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }
        with pytest.raises(Exception, match="API credentials required"):
            PnlCheckerConfig(**data)

    def test_short_api_key_rejected(self, monkeypatch):
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        data = {
            "account": {"api_key": "short", "api_secret": "valid_secret_0123456789"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }
        with pytest.raises(Exception, match="api_key appears invalid"):
            PnlCheckerConfig(**data)

    def test_short_api_secret_rejected(self, monkeypatch):
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        data = {
            "account": {"api_key": "valid_key_0123456789", "api_secret": "short"},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }
        with pytest.raises(Exception, match="api_secret appears invalid"):
            PnlCheckerConfig(**data)

    def test_env_only_no_yaml_credentials(self, monkeypatch):
        monkeypatch.setenv("BYBIT_API_KEY", "env_key_0123456789")
        monkeypatch.setenv("BYBIT_API_SECRET", "env_secret_0123456789")
        data = {
            "account": {},
            "symbols": [{"symbol": "BTCUSDT", "tick_size": "0.1"}],
        }
        cfg = PnlCheckerConfig(**data)

        assert cfg.account.api_key == "env_key_0123456789"
        assert cfg.account.api_secret == "env_secret_0123456789"
