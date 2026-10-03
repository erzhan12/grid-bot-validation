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


class TestWalletCoinDefaults:
    def test_single_and_shared_seed_wallet_coins_match(self):
        """The coverage gate anchors on SeedConfig.wallet_coin's default;
        --shared seeds from MultiSeedConfig.wallet_coin. Keep them equal."""
        from replay.config import SeedConfig
        from replay.multi_config import MultiSeedConfig

        assert (SeedConfig.model_fields["wallet_coin"].default
                == MultiSeedConfig.model_fields["wallet_coin"].default)


class TestLagValidation:
    def test_config_lag_below_checkpoint_trail_rejected(self, strat):
        """0110 B2b: a config `lag` at the 85 s floor fails at load time."""
        with pytest.raises(ValidationError, match="LIVENESS_MARGIN"):
            LiveCheckConfig(strats=[strat], lag="85s")

    def test_default_lag_accepted(self, strat):
        """The 2m default clears the floor."""
        assert LiveCheckConfig(strats=[strat]).lag == "2m"


class TestStratsValidation:
    def test_empty_strats_rejected(self):
        """An empty strat list is a config error, not a false-green run."""
        with pytest.raises(ValidationError):
            LiveCheckConfig(strats=[])

    def test_missing_strats_rejected(self):
        """strats is required."""
        with pytest.raises(ValidationError):
            LiveCheckConfig()
