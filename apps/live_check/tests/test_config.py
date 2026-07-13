"""Tests for live_check.config validation guards."""

import pytest
from pydantic import ValidationError

from live_check.config import LiveCheckConfig, load_config


class TestLoadConfig:
    def test_empty_yaml_file_raises_value_error(self, tmp_path):
        """yaml.safe_load returns None on an empty file — clean error, not
        a TypeError from LiveCheckConfig(**None)."""
        empty = tmp_path / "live_check.yaml"
        empty.write_text("")
        with pytest.raises(ValueError, match="Empty or invalid YAML"):
            load_config(str(empty))


class TestStratsValidation:
    def test_empty_strats_rejected(self):
        """An empty strat list is a config error, not a false-green run."""
        with pytest.raises(ValidationError):
            LiveCheckConfig(strats=[])

    def test_missing_strats_rejected(self):
        """strats is required."""
        with pytest.raises(ValidationError):
            LiveCheckConfig()
