"""Tests for the --watch freshness gate."""

from datetime import datetime, timedelta

from live_check.window import freshness_skip_reason, staleness_threshold

_NOW = datetime(2026, 7, 1, 12, 0, 0)
_LAG = timedelta(minutes=2)


class TestStalenessThreshold:
    def test_default_is_max_of_2lag_and_5min(self):
        """threshold = max(2*lag, 5 minutes)."""
        assert staleness_threshold(timedelta(minutes=2)) == timedelta(minutes=5)
        assert staleness_threshold(timedelta(minutes=10)) == timedelta(minutes=20)

    def test_override_wins(self):
        """Explicit override replaces the derived default."""
        assert staleness_threshold(
            timedelta(minutes=2), override=timedelta(minutes=30)
        ) == timedelta(minutes=30)


class TestFreshnessGate:
    def test_fresh_ticker_passes(self):
        """Recent ticker rows → no skip (quiet-but-healthy periods included:
        the probe is TickerSnapshot, never PrivateExecution, so zero fills
        with a flowing ticker does NOT trip the gate)."""
        latest = _NOW - _LAG - timedelta(minutes=1)
        threshold = staleness_threshold(_LAG)
        assert freshness_skip_reason(latest, _LAG, threshold, now=_NOW) is None

    def test_stale_ticker_skips(self):
        """Ticker older than the threshold → skip with a stale reason."""
        latest = _NOW - _LAG - timedelta(minutes=20)
        threshold = staleness_threshold(_LAG)
        reason = freshness_skip_reason(latest, _LAG, threshold, now=_NOW)
        assert reason is not None
        assert "stale" in reason

    def test_none_ticker_ts_skips_no_crash(self):
        """No ticker rows at all → SKIP reason 'no ticker data', never a
        TypeError from subtracting None."""
        threshold = staleness_threshold(_LAG)
        reason = freshness_skip_reason(None, _LAG, threshold, now=_NOW)
        assert reason == "no ticker data"

    def test_boundary_at_threshold_passes(self):
        """Age exactly == threshold is not yet stale (strict >)."""
        threshold = staleness_threshold(_LAG)
        latest = _NOW - _LAG - threshold
        assert freshness_skip_reason(latest, _LAG, threshold, now=_NOW) is None
