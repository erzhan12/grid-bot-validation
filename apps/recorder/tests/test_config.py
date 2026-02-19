"""Tests for recorder configuration."""

import tempfile

import pytest
import yaml

from recorder.config import RecorderConfig, AccountConfig, load_config


class TestRecorderConfig:
    """Tests for RecorderConfig model."""

    def test_defaults(self):
        config = RecorderConfig()
        assert config.symbols == []
        assert config.database_url == "sqlite:///recorder.db"
        assert config.testnet is False
        assert config.batch_size == 100
        assert config.flush_interval == 5.0
        assert config.gap_threshold_seconds == 5.0
        assert config.health_log_interval == 300.0
        assert config.account is None

    def test_custom_values(self):
        config = RecorderConfig(
            symbols=["BTCUSDT", "ETHUSDT"],
            database_url="sqlite:///custom.db",
            testnet=True,
            batch_size=50,
            flush_interval=2.0,
            gap_threshold_seconds=10.0,
            health_log_interval=60.0,
        )
        assert config.symbols == ["BTCUSDT", "ETHUSDT"]
        assert config.testnet is True
        assert config.batch_size == 50
        assert config.flush_interval == 2.0

    def test_with_account(self):
        config = RecorderConfig(
            symbols=["BTCUSDT"],
            account=AccountConfig(api_key="key1", api_secret="secret1"),
        )
        assert config.account is not None
        assert config.account.api_key.get_secret_value() == "key1"
        assert config.account.api_secret.get_secret_value() == "secret1"

    def test_account_secrets_redacted_in_repr(self):
        config = RecorderConfig(
            symbols=["BTCUSDT"],
            account=AccountConfig(api_key="key1", api_secret="secret1"),
        )
        text = repr(config)
        assert "key1" not in text
        assert "secret1" not in text
        assert "**********" in text

    def test_without_account(self):
        config = RecorderConfig(symbols=["BTCUSDT"])
        assert config.account is None

    def test_batch_size_must_be_positive(self):
        with pytest.raises(ValueError):
            RecorderConfig(batch_size=0)

    def test_flush_interval_must_be_positive(self):
        with pytest.raises(ValueError):
            RecorderConfig(flush_interval=0)


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_valid_yaml(self, tmp_path):
        config_file = tmp_path / "recorder.yaml"
        config_file.write_text(yaml.dump({
            "symbols": ["BTCUSDT"],
            "database_url": "sqlite:///test.db",
            "testnet": True,
        }))

        config = load_config(str(config_file))
        assert config.symbols == ["BTCUSDT"]
        assert config.testnet is True

    def test_load_with_account(self, tmp_path):
        config_file = tmp_path / "recorder.yaml"
        config_file.write_text(yaml.dump({
            "symbols": ["BTCUSDT"],
            "account": {
                "api_key": "mykey",
                "api_secret": "mysecret",
            },
        }))

        config = load_config(str(config_file))
        assert config.account is not None
        assert config.account.api_key.get_secret_value() == "mykey"

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/recorder.yaml")

    def test_no_config_found(self, monkeypatch):
        monkeypatch.delenv("RECORDER_CONFIG_PATH", raising=False)
        monkeypatch.chdir(tempfile.mkdtemp())
        with pytest.raises(FileNotFoundError, match="No config file found"):
            load_config()

    def test_env_var_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "custom.yaml"
        config_file.write_text(yaml.dump({"symbols": ["ETHUSDT"]}))
        monkeypatch.setenv("RECORDER_CONFIG_PATH", str(config_file))

        config = load_config()
        assert config.symbols == ["ETHUSDT"]

    def test_default_search_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RECORDER_CONFIG_PATH", raising=False)
        monkeypatch.chdir(tmp_path)

        # Create conf/recorder.yaml
        conf_dir = tmp_path / "conf"
        conf_dir.mkdir()
        config_file = conf_dir / "recorder.yaml"
        config_file.write_text(yaml.dump({"symbols": ["BTCUSDT"]}))

        config = load_config()
        assert config.symbols == ["BTCUSDT"]

    def test_invalid_config_values_raise_error(self, tmp_path):
        config_file = tmp_path / "recorder.yaml"
        config_file.write_text(yaml.dump({
            "symbols": ["BTCUSDT"],
            "batch_size": -5,  # Invalid
        }))

        with pytest.raises(ValueError):
            load_config(str(config_file))

    def test_malformed_yaml_raises_value_error(self, tmp_path):
        config_file = tmp_path / "recorder.yaml"
        config_file.write_text("symbols: [unclosed bracket")

        with pytest.raises(ValueError, match="Invalid YAML"):
            load_config(str(config_file))

    def test_empty_yaml_returns_defaults(self, tmp_path):
        config_file = tmp_path / "recorder.yaml"
        config_file.write_text("")  # empty file → safe_load returns None

        config = load_config(str(config_file))
        assert config.symbols == []
        assert config.testnet is False
