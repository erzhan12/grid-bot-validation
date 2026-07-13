"""Aware-vs-naive datetime regression tests (mandatory naive-UTC normalization).

SQLite stores timestamps tz-stripped; an aware-vs-naive comparison or
subtraction raises TypeError. Every window/cutoff/freshness code path must
normalize to naive UTC first.
"""

from datetime import datetime, timedelta, timezone

import pytest

from live_check.window import (
    check_post_0080_floors,
    compute_window,
    freshness_skip_reason,
    staleness_threshold,
    to_naive_utc,
)

_TZ_PLUS5 = timezone(timedelta(hours=5))


class TestToNaiveUtc:
    def test_aware_converted_to_utc_then_stripped(self):
        """+05:00 input lands on the equivalent UTC wall time, naive."""
        aware = datetime(2026, 7, 1, 17, 0, 0, tzinfo=_TZ_PLUS5)
        naive = to_naive_utc(aware)
        assert naive == datetime(2026, 7, 1, 12, 0, 0)
        assert naive.tzinfo is None

    def test_naive_passes_through(self):
        """Naive input is assumed UTC and returned unchanged."""
        dt = datetime(2026, 7, 1, 12, 0, 0)
        assert to_naive_utc(dt) is dt


class TestWindowMathWithAwareInputs:
    def test_compute_window_with_aware_now(self):
        """Aware now → naive-UTC bounds, no TypeError downstream."""
        aware_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        w = compute_window(timedelta(hours=4), timedelta(minutes=2), now=aware_now)
        assert w.start.tzinfo is None and w.end.tzinfo is None
        assert w.end == datetime(2026, 7, 1, 11, 58, 0)
        # Comparable against a SQLite-style naive timestamp without raising.
        assert w.start < datetime(2026, 7, 1, 12, 0, 0)

    def test_cutoff_guard_with_aware_inputs(self):
        """Aware window start + aware run start compare cleanly vs the cutoff."""
        start = datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc)
        run_start = datetime(2026, 7, 3, 0, 0, 0, tzinfo=_TZ_PLUS5)
        check_post_0080_floors(start, run_start)  # no TypeError

    def test_cutoff_guard_aware_still_fires(self):
        """Normalization keeps the guard semantics — pre-cutoff aware start
        (post-cutoff only via its +05:00 offset) is still rejected."""
        start = datetime(2026, 6, 18, 3, 0, 0, tzinfo=_TZ_PLUS5)  # 06-17 22:00 UTC
        run_start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="0080"):
            check_post_0080_floors(start, run_start)

    def test_freshness_subtraction_with_aware_inputs(self):
        """Aware ticker ts and aware now subtract cleanly, no TypeError."""
        now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        latest = datetime(2026, 7, 1, 16, 57, 0, tzinfo=_TZ_PLUS5)  # 11:57 UTC
        threshold = staleness_threshold(timedelta(minutes=2))
        assert freshness_skip_reason(
            latest, timedelta(minutes=2), threshold, now=now
        ) is None
