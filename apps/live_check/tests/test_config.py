"""Tests for live_check.config validation guards."""

import pytest
from pydantic import ValidationError

from live_check.config import LiveCheckConfig


class TestStratsValidation:
    def test_empty_strats_rejected(self):
        """An empty strat list is a config error, not a false-green run."""
        with pytest.raises(ValidationError):
            LiveCheckConfig(strats=[])

    def test_missing_strats_rejected(self):
        """strats is required."""
        with pytest.raises(ValidationError):
            LiveCheckConfig()
