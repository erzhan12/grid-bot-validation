"""Tests for RateLimiter sliding window implementation."""

import pytest
from datetime import datetime, timedelta, UTC
from unittest.mock import patch

from bybit_adapter.rate_limiter import RateLimiter, RateLimitConfig


class TestRateLimiterBasic:
    """Tests for basic rate limiter functionality."""

    def test_can_request_under_limit(self):
        """Test that requests are allowed under the limit."""
        limiter = RateLimiter()
        assert limiter.can_request("order") is True
        assert limiter.can_request("query") is True

    def test_can_request_at_limit(self):
        """Test that requests are blocked at the limit."""
        config = RateLimitConfig(order_rate=2, window_seconds=1.0)
        limiter = RateLimiter(config=config)

        # Record 2 requests
        limiter.record_request("order")
        limiter.record_request("order")

        # Should be at limit
        assert limiter.can_request("order") is False

    def test_record_request_tracks_timestamps(self):
        """Test that requests are recorded."""
        limiter = RateLimiter()

        limiter.record_request("order")
        assert limiter.get_available_capacity("order") == 9  # 10 - 1

    def test_different_request_types_tracked_separately(self):
        """Test that order and query limits are independent."""
        config = RateLimitConfig(order_rate=2, query_rate=3)
        limiter = RateLimiter(config=config)

        # Use all order capacity
        limiter.record_request("order")
        limiter.record_request("order")

        # Query should still be available
        assert limiter.can_request("order") is False
        assert limiter.can_request("query") is True


class TestSlidingWindow:
    """Tests for sliding window cleanup."""

    def test_old_timestamps_cleaned_up(self):
        """Test that old timestamps are removed from the window."""
        config = RateLimitConfig(order_rate=2, window_seconds=0.1)
        limiter = RateLimiter(config=config)

        # Record 2 requests (at limit)
        limiter.record_request("order")
        limiter.record_request("order")
        assert limiter.can_request("order") is False

        # Wait for window to pass (using mock time)
        with patch("bybit_adapter.rate_limiter.datetime") as mock_dt:
            future_time = datetime.now(UTC) + timedelta(seconds=0.2)
            mock_dt.now.return_value = future_time
            mock_dt.min = datetime.min

            # Old requests should be cleaned up
            assert limiter.can_request("order") is True

    def test_get_available_capacity(self):
        """Test available capacity calculation."""
        config = RateLimitConfig(order_rate=5)
        limiter = RateLimiter(config=config)

        assert limiter.get_available_capacity("order") == 5

        limiter.record_request("order")
        limiter.record_request("order")

        assert limiter.get_available_capacity("order") == 3


class TestWaitTime:
    """Tests for wait time calculation."""

    def test_wait_time_zero_when_under_limit(self):
        """Test that wait time is 0 when under limit."""
        limiter = RateLimiter()
        assert limiter.wait_time("order") == 0.0

    def test_wait_time_positive_at_limit(self):
        """Test that wait time is positive when at limit."""
        config = RateLimitConfig(order_rate=1, window_seconds=1.0)
        limiter = RateLimiter(config=config)

        limiter.record_request("order")
        wait = limiter.wait_time("order")

        assert wait > 0
        assert wait <= 1.0


class TestBackoff:
    """Tests for exponential backoff on 429 responses."""

    def test_record_rate_limit_hit_activates_backoff(self):
        """Test that 429 response triggers backoff."""
        limiter = RateLimiter()

        # Initially no backoff
        assert limiter.get_backoff_remaining() == 0.0

        # Record a 429
        limiter.record_rate_limit_hit()

        # Should now have backoff
        assert limiter.get_backoff_remaining() > 0

    def test_backoff_is_exponential(self):
        """Test that consecutive 429s increase backoff exponentially."""
        config = RateLimitConfig(backoff_base=1.0, max_backoff=60.0)
        limiter = RateLimiter(config=config)

        # First 429: 1 second
        limiter.record_rate_limit_hit()
        first_backoff = limiter.get_backoff_remaining()

        # Reset and record another
        limiter.reset()
        limiter.record_rate_limit_hit()
        limiter.record_rate_limit_hit()
        second_backoff = limiter.get_backoff_remaining()

        # Second should be ~2x first (exponential)
        assert second_backoff > first_backoff

    def test_backoff_respects_max(self):
        """Test that backoff doesn't exceed max_backoff."""
        config = RateLimitConfig(backoff_base=1.0, max_backoff=5.0)
        limiter = RateLimiter(config=config)

        # Record many 429s
        for _ in range(10):
            limiter.record_rate_limit_hit()

        # Should not exceed max
        assert limiter.get_backoff_remaining() <= 5.0

    def test_can_request_false_during_backoff(self):
        """Test that requests are blocked during backoff."""
        limiter = RateLimiter()
        limiter.record_rate_limit_hit()

        # Should be blocked by backoff
        assert limiter.can_request("order") is False
        assert limiter.can_request("query") is False

    def test_record_success_resets_consecutive_counter(self):
        """Test that successful requests reset the 429 counter."""
        config = RateLimitConfig(backoff_base=1.0)
        limiter = RateLimiter(config=config)

        # Record a 429
        limiter.record_rate_limit_hit()

        # Reset manually (as if backoff expired) and record success
        limiter._backoff_until = datetime.min.replace(tzinfo=UTC)
        limiter.record_success()

        # Next 429 should start from 1 second again
        limiter.record_rate_limit_hit()
        assert limiter.get_backoff_remaining() <= 1.0


class TestReset:
    """Tests for limiter reset functionality."""

    def test_reset_clears_all_state(self):
        """Test that reset clears all tracking state."""
        limiter = RateLimiter()

        # Add some state
        limiter.record_request("order")
        limiter.record_request("query")
        limiter.record_rate_limit_hit()

        # Reset
        limiter.reset()

        # All state should be cleared
        assert limiter.get_available_capacity("order") == 10
        assert limiter.get_available_capacity("query") == 20
        assert limiter.get_backoff_remaining() == 0.0
        assert limiter.can_request("order") is True


class TestConfiguration:
    """Tests for custom configuration."""

    def test_custom_order_rate(self):
        """Test custom order rate limit."""
        config = RateLimitConfig(order_rate=5)
        limiter = RateLimiter(config=config)

        assert limiter.get_available_capacity("order") == 5

    def test_custom_query_rate(self):
        """Test custom query rate limit."""
        config = RateLimitConfig(query_rate=10)
        limiter = RateLimiter(config=config)

        assert limiter.get_available_capacity("query") == 10

    def test_custom_window_seconds(self):
        """Test custom sliding window size."""
        config = RateLimitConfig(order_rate=1, window_seconds=2.0)
        limiter = RateLimiter(config=config)

        limiter.record_request("order")
        wait = limiter.wait_time("order")

        # Wait time should be up to 2 seconds
        assert 0 < wait <= 2.0
