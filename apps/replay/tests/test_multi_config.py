"""Tests for shared-wallet replay config validation."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from replay.multi_config import MultiReplayConfig, MultiReplayStrategyConfig


def _strategy(symbol: str = "SOLUSDT", strat_id: str = "solusdt_test"):
    return {
        "symbol": symbol,
        "strat_id": strat_id,
        "tick_size": "0.01",
        "grid_count": 10,
        "grid_step": 0.5,
    }


class TestMultiReplayConfig:
    def test_rejects_empty_strategies(self):
        """Empty strategy list is invalid and cannot false-green."""
        with pytest.raises(ValidationError):
            MultiReplayConfig(strategies=[])

    def test_strategy_requires_symbol_and_strat_id(self):
        """Each strategy has explicit replay identity."""
        with pytest.raises(ValidationError):
            MultiReplayStrategyConfig(symbol="SOLUSDT")
        with pytest.raises(ValidationError):
            MultiReplayStrategyConfig(strat_id="solusdt_test")

    def test_seed_requires_account_fields_when_enabled(self):
        """Account-level seed fields are required when seeding."""
        with pytest.raises(ValidationError, match="at_ts|account_id"):
            MultiReplayConfig(
                strategies=[_strategy()],
                seed={"enabled": True, "at_ts": datetime.now(timezone.utc)},
            )

    def test_rejects_duplicate_symbol(self):
        """Duplicate symbol would silently last-win in the engine's per-symbol maps."""
        with pytest.raises(ValidationError, match="duplicate strategy symbol"):
            MultiReplayConfig(
                strategies=[
                    _strategy("SOLUSDT", "a"),
                    _strategy("SOLUSDT", "b"),
                ]
            )

    def test_rejects_duplicate_strat_id(self):
        """Duplicate strat_id would collide the client_order_id namespace."""
        with pytest.raises(ValidationError, match="duplicate strategy strat_id"):
            MultiReplayConfig(
                strategies=[
                    _strategy("SOLUSDT", "dup"),
                    _strategy("LTCUSDT", "dup"),
                ]
            )

    def test_rejects_traded_base_as_collateral(self):
        """A traded base coin cannot also be modelled as spot collateral."""
        with pytest.raises(ValidationError, match="traded base"):
            MultiReplayConfig(
                strategies=[_strategy("SOLUSDT")],
                seed={"collateral_coins": ["SOL"]},
            )

    def test_decimal_fields_parse_from_strings(self):
        """Shared wallet numeric config keeps Decimal precision."""
        config = MultiReplayConfig(
            strategies=[_strategy()],
            initial_balance="60.25",
        )
        assert config.initial_balance == Decimal("60.25")
