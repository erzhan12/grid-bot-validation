"""Tests for bybit_adapter.position_utils (feature 0111, issue #292)."""

import pytest

from bybit_adapter.position_utils import leg_side


class TestLegSide:
    @pytest.mark.parametrize("side", ["Buy", "Sell"])
    def test_nonempty_side_wins(self, side):
        """A populated side is returned as-is, whatever positionIdx says."""
        assert leg_side({"side": side, "positionIdx": 0}) == side
        assert leg_side({"side": side, "positionIdx": 2}) == side

    @pytest.mark.parametrize(
        "position_idx, expected",
        [(1, "Buy"), (2, "Sell"), ("1", "Buy"), ("2", "Sell")],
    )
    def test_flat_hedge_leg_from_position_idx(self, position_idx, expected):
        """Bybit sends side="" for a flat leg; hedge mode still names the leg."""
        assert leg_side({"side": "", "positionIdx": position_idx}) == expected

    def test_one_way_flat_keeps_empty_side(self):
        """positionIdx 0 (one-way mode) has no leg to resolve."""
        assert leg_side({"side": "", "positionIdx": 0}) == ""

    def test_null_side_resolves_from_position_idx(self):
        """A null side (not just empty) still resolves the hedge leg."""
        assert leg_side({"side": None, "positionIdx": 1}) == "Buy"
        assert leg_side({"side": None, "positionIdx": "2"}) == "Sell"

    def test_missing_fields_keep_empty_side(self):
        """No side and no positionIdx: unresolved, never raises."""
        assert leg_side({}) == ""
        assert leg_side({"side": ""}) == ""
        assert leg_side({"side": None}) == ""
        assert leg_side({"positionIdx": None}) == ""
