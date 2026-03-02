"""Tests for scripts/check_tier_drift.py tier comparison logic."""

from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

# Import the functions under test directly from the script module.
import importlib
import sys
from pathlib import Path

# Add scripts directory to path so we can import check_tier_drift
_scripts_dir = str(Path(__file__).resolve().parent.parent.parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from check_tier_drift import compare_tiers, _rate_drift_pct, _fetch_live_tiers, main


# ---------------------------------------------------------------------------
# _rate_drift_pct
# ---------------------------------------------------------------------------

class TestRateDriftPct:
    """Tests for the _rate_drift_pct helper."""

    def test_identical_values_return_zero(self):
        assert _rate_drift_pct(Decimal("0.01"), Decimal("0.01")) == 0.0

    def test_both_zero_returns_zero(self):
        assert _rate_drift_pct(Decimal("0"), Decimal("0")) == 0.0

    def test_hardcoded_zero_live_nonzero_returns_inf(self):
        assert _rate_drift_pct(Decimal("0"), Decimal("0.05")) == float("inf")

    def test_positive_drift(self):
        # 0.01 → 0.02 = 100% drift
        drift = _rate_drift_pct(Decimal("0.01"), Decimal("0.02"))
        assert abs(drift - 1.0) < 1e-9

    def test_negative_drift_is_absolute(self):
        # 0.02 → 0.01 = 50% drift (absolute)
        drift = _rate_drift_pct(Decimal("0.02"), Decimal("0.01"))
        assert abs(drift - 0.5) < 1e-9

    def test_small_drift(self):
        # 0.01 → 0.0101 = 1% drift
        drift = _rate_drift_pct(Decimal("0.01"), Decimal("0.0101"))
        assert abs(drift - 0.01) < 1e-9


# ---------------------------------------------------------------------------
# compare_tiers
# ---------------------------------------------------------------------------

class TestCompareTiers:
    """Tests for the compare_tiers function."""

    HARDCODED = [
        (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
        (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
    ]

    def test_no_drift_returns_empty(self):
        """Identical tiers produce no drift messages."""
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, self.HARDCODED, 0.05)
        assert drifts == []

    def test_tier_count_mismatch(self):
        """Different tier counts produce a mismatch message."""
        live = self.HARDCODED[:1]  # only 1 tier vs 2
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, live, 0.05)
        assert any("tier count mismatch" in d for d in drifts)

    def test_mmr_rate_drift_detected(self):
        """MMR rate drift above threshold is reported."""
        live = [
            (Decimal("200000"), Decimal("0.02"), Decimal("0"), Decimal("0.02")),  # mmr doubled
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, live, 0.05)
        assert any("mmr_rate drift" in d for d in drifts)

    def test_imr_rate_drift_detected(self):
        """IMR rate drift above threshold is reported."""
        live = [
            (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.04")),  # imr doubled
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, live, 0.05)
        assert any("imr_rate drift" in d for d in drifts)

    def test_deduction_drift_detected(self):
        """Deduction drift above threshold is reported."""
        live = [
            (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.025"), Decimal("6000"), Decimal("0.05")),  # deduction doubled
        ]
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, live, 0.05)
        assert any("deduction drift" in d for d in drifts)

    def test_max_value_drift_detected(self):
        """max_value drift above threshold is reported (non-Infinity)."""
        live = [
            (Decimal("400000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),  # doubled
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, live, 0.05)
        assert any("max_value drift" in d for d in drifts)

    def test_infinity_max_value_skipped(self):
        """Infinity max_value comparison is skipped (no false drift)."""
        # Both have Infinity — should not trigger max_value drift
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, self.HARDCODED, 0.0)
        max_value_drifts = [d for d in drifts if "max_value drift" in d]
        assert max_value_drifts == []

    def test_drift_below_threshold_not_reported(self):
        """Drift within threshold is not reported."""
        live = [
            (Decimal("200000"), Decimal("0.0101"), Decimal("0"), Decimal("0.02")),  # 1% drift
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, live, 0.05)  # 5% threshold
        assert drifts == []

    def test_empty_tiers_count_mismatch(self):
        """Empty live tiers vs non-empty hardcoded reports mismatch."""
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, [], 0.05)
        assert any("tier count mismatch" in d for d in drifts)

    def test_zero_threshold_catches_any_difference(self):
        """Zero threshold catches even tiny differences."""
        live = [
            (Decimal("200001"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),  # tiny max_value diff
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]
        drifts = compare_tiers("BTCUSDT", self.HARDCODED, live, 0.0)
        assert len(drifts) > 0

    def test_drift_message_includes_symbol(self):
        """Drift messages include the symbol name."""
        live = [
            (Decimal("200000"), Decimal("0.05"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]
        drifts = compare_tiers("ETHUSDT", self.HARDCODED, live, 0.05)
        assert all("ETHUSDT" in d for d in drifts)


# ---------------------------------------------------------------------------
# _fetch_live_tiers
# ---------------------------------------------------------------------------

class TestFetchLiveTiers:
    """Tests for _fetch_live_tiers error handling."""

    def test_empty_api_response_raises(self):
        """Empty response from API raises RuntimeError."""
        # BybitRestClient is imported inside _fetch_live_tiers, so patch
        # at the source module where it's looked up.
        with patch("bybit_adapter.rest_client.BybitRestClient") as MockClass:
            mock_instance = MagicMock()
            mock_instance.get_risk_limit.return_value = []
            MockClass.return_value = mock_instance

            with pytest.raises(RuntimeError, match="Empty response"):
                _fetch_live_tiers("BTCUSDT")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

class TestMain:
    """Tests for the main() entry point."""

    def test_no_drift_returns_zero(self, capsys):
        """main() returns 0 when no drift is detected."""
        from gridcore.pnl import MM_TIERS

        with patch("check_tier_drift._fetch_live_tiers") as mock_fetch:
            # Return identical tiers for each symbol
            mock_fetch.side_effect = lambda sym: MM_TIERS[sym]
            with patch("sys.argv", ["check_tier_drift.py", "--threshold", "0.05"]):
                result = main()

        assert result == 0
        captured = capsys.readouterr()
        assert "No drift detected" in captured.out

    def test_drift_returns_one(self, capsys):
        """main() returns 1 when drift is detected."""
        from gridcore.pnl import MM_TIERS

        # Create modified tiers with large drift
        first_symbol = next(iter(MM_TIERS))
        modified = [
            (Decimal("999999"), Decimal("0.99"), Decimal("0"), Decimal("0.99")),
        ]

        with patch("check_tier_drift._fetch_live_tiers") as mock_fetch:
            mock_fetch.return_value = modified
            with patch("sys.argv", ["check_tier_drift.py", "--threshold", "0.05"]):
                result = main()

        assert result == 1
        captured = capsys.readouterr()
        assert "Drift detected" in captured.out

    def test_api_failure_reports_error(self, capsys):
        """main() includes API failures in drift report."""
        with patch("check_tier_drift._fetch_live_tiers") as mock_fetch:
            mock_fetch.side_effect = ConnectionError("timeout")
            with patch("sys.argv", ["check_tier_drift.py"]):
                result = main()

        assert result == 1
        captured = capsys.readouterr()
        assert "failed to fetch live tiers" in captured.out

    def test_custom_threshold_respected(self, capsys):
        """Custom --threshold value is passed through to compare_tiers."""
        from gridcore.pnl import MM_TIERS

        with patch("check_tier_drift._fetch_live_tiers") as mock_fetch:
            mock_fetch.side_effect = lambda sym: MM_TIERS[sym]
            with patch("sys.argv", ["check_tier_drift.py", "--threshold", "0.001"]):
                result = main()

        # With identical tiers, even strict threshold should pass
        assert result == 0
