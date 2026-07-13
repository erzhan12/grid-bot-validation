"""Tests for live_check.window — window/lag math and the empty-window guard."""

from datetime import datetime, timedelta

import pytest

from live_check.main import check_strat
from live_check.window import Window, compute_window, parse_duration


class TestParseDuration:
    @pytest.mark.parametrize("text,expected", [
        ("30s", timedelta(seconds=30)),
        ("2m", timedelta(minutes=2)),
        ("10m", timedelta(minutes=10)),
        ("4h", timedelta(hours=4)),
        ("1d", timedelta(days=1)),
    ])
    def test_valid(self, text, expected):
        """Parses count+unit forms."""
        assert parse_duration(text) == expected

    @pytest.mark.parametrize("text", ["", "4", "h", "4hours", "-2m", "1.5h"])
    def test_invalid_raises(self, text):
        """Unparseable durations are a hard error."""
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration(text)


class TestComputeWindow:
    def test_end_is_now_minus_lag(self):
        """end = now − lag; start = end − last."""
        now = datetime(2026, 7, 1, 12, 0, 0)
        w = compute_window(timedelta(hours=4), timedelta(minutes=2), now=now)
        assert w.end == datetime(2026, 7, 1, 11, 58, 0)
        assert w.start == datetime(2026, 7, 1, 7, 58, 0)

    def test_rolling(self):
        """A later now shifts both bounds by the same amount."""
        last, lag = timedelta(hours=1), timedelta(minutes=2)
        w1 = compute_window(last, lag, now=datetime(2026, 7, 1, 12, 0, 0))
        w2 = compute_window(last, lag, now=datetime(2026, 7, 1, 12, 10, 0))
        assert w2.start - w1.start == timedelta(minutes=10)
        assert w2.end - w1.end == timedelta(minutes=10)


class TestEmptyWindowGuard:
    def test_zero_executions_reports_skip_never_pass(
        self, db, seeded_run_account, ts, strat, live_check_config
    ):
        """A window with 0 live executions is SKIP with a reason, not PASS."""
        window = Window(start=ts, end=ts + timedelta(hours=1))
        outcome = check_strat(
            strat, window, seeded_run_account.run_id,
            seeded_run_account.account_id, db, live_check_config,
        )
        assert outcome[0] == "skip"
        assert "no data in window" in outcome[1]
