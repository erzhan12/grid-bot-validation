"""Tests for the pre-0080 window guard (both floors)."""

from datetime import datetime

import pytest

from live_check.window import POST_0080_CUTOFF, check_post_0080_floors


class TestPost0080Guard:
    def test_start_before_run_start_raises(self):
        """Run floor: window.start < run.start_ts is a hard error."""
        run_start = datetime(2026, 7, 3, 0, 0, 0)
        with pytest.raises(ValueError, match="run's\\s+start_ts"):
            check_post_0080_floors(datetime(2026, 7, 2, 0, 0, 0), run_start)

    def test_start_before_absolute_cutoff_raises(self):
        """Absolute floor fires even when start >= run.start_ts.

        A --run-id pointed at a PRE-0080 recording passes the run floor but
        must still be rejected (954→44 matching collapse).
        """
        run_start = datetime(2026, 6, 1, 0, 0, 0)  # pre-0080 recording
        start = datetime(2026, 6, 10, 0, 0, 0)  # >= run_start, < cutoff
        with pytest.raises(ValueError, match="0080"):
            check_post_0080_floors(start, run_start)

    def test_post_cutoff_post_run_start_passes(self):
        """Both floors satisfied → no error."""
        check_post_0080_floors(
            datetime(2026, 7, 5, 0, 0, 0), datetime(2026, 7, 3, 0, 0, 0)
        )

    def test_cutoff_constant_value(self):
        """Cutoff pins the 0080 merge moment 2026-06-17T23:07:00Z (naive UTC)."""
        assert POST_0080_CUTOFF == datetime(2026, 6, 17, 23, 7, 0)
        assert POST_0080_CUTOFF.tzinfo is None
