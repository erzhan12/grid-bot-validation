"""Per-account rate limit tracking for Bybit API.

Bybit rate limits (per API key):
- Order submission: 10 requests/second per category
- Query: 20 requests/second

This module provides a sliding window rate limiter with exponential backoff
support for handling 429 responses.

Reference: https://bybit-exchange.github.io/docs/v5/rate-limit
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from typing import Literal


RequestType = Literal["order", "query"]


@dataclass
class RateLimitConfig:
    """Configuration for Bybit rate limits.

    Attributes:
        order_rate: Maximum order requests per window (default: 10)
        query_rate: Maximum query requests per window (default: 20)
        window_seconds: Sliding window size in seconds (default: 1.0)
        backoff_base: Base delay in seconds for exponential backoff (default: 1.0)
        max_backoff: Maximum backoff delay in seconds (default: 60.0)
    """

    order_rate: int = 10
    query_rate: int = 20
    window_seconds: float = 1.0
    backoff_base: float = 1.0
    max_backoff: float = 60.0


@dataclass
class RateLimiter:
    """Track and enforce per-account rate limits.

    Uses a sliding window algorithm to track request timestamps and determine
    available capacity. Supports exponential backoff when rate limit errors
    (HTTP 429) are received from the API.

    Responsibilities:
    - Track request timestamps in sliding window
    - Calculate available capacity
    - Provide wait time until next available slot
    - Implement exponential backoff on 429 response

    Example:
        limiter = RateLimiter()

        # Before making a request
        if limiter.can_request("order"):
            limiter.record_request("order")
            # make the request
        else:
            wait = limiter.wait_time("order")
            await asyncio.sleep(wait)

        # On 429 response
        limiter.record_rate_limit_hit()
    """

    config: RateLimitConfig = field(default_factory=RateLimitConfig)
    _order_timestamps: deque = field(default_factory=deque, init=False)
    _query_timestamps: deque = field(default_factory=deque, init=False)
    _backoff_until: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC), init=False)
    _consecutive_429s: int = field(default=0, init=False)

    def can_request(self, request_type: RequestType) -> bool:
        """Check if a request can be made within rate limits.

        Args:
            request_type: "order" or "query"

        Returns:
            True if request can be made, False if rate limited
        """
        now = datetime.now(UTC)

        # Check if in backoff period
        if now < self._backoff_until:
            return False

        # Clean old timestamps and check capacity
        self._cleanup_old_timestamps(now)
        return self._get_current_count(request_type) < self._get_limit(request_type)

    def record_request(self, request_type: RequestType) -> None:
        """Record a request timestamp.

        Should be called immediately after making a successful request.

        Args:
            request_type: "order" or "query"
        """
        now = datetime.now(UTC)
        timestamps = self._get_timestamps(request_type)
        timestamps.append(now)

    def wait_time(self, request_type: RequestType) -> float:
        """Calculate seconds to wait before next request is allowed.

        Args:
            request_type: "order" or "query"

        Returns:
            Seconds to wait (0.0 if request can be made now)
        """
        now = datetime.now(UTC)

        # Check backoff period first
        if now < self._backoff_until:
            return (self._backoff_until - now).total_seconds()

        self._cleanup_old_timestamps(now)

        # Check if under limit
        count = self._get_current_count(request_type)
        limit = self._get_limit(request_type)
        if count < limit:
            return 0.0

        # Calculate wait time based on oldest timestamp in window
        timestamps = self._get_timestamps(request_type)
        if not timestamps:
            return 0.0

        oldest = timestamps[0]
        window = timedelta(seconds=self.config.window_seconds)
        available_at = oldest + window
        wait = (available_at - now).total_seconds()
        return max(0.0, wait)

    def record_rate_limit_hit(self) -> None:
        """Record a 429 response, activating exponential backoff.

        Should be called when a 429 Too Many Requests response is received.
        Each consecutive 429 doubles the backoff delay.
        """
        self._consecutive_429s += 1
        backoff_seconds = min(
            self.config.backoff_base * (2 ** (self._consecutive_429s - 1)),
            self.config.max_backoff,
        )
        self._backoff_until = datetime.now(UTC) + timedelta(seconds=backoff_seconds)

    def record_success(self) -> None:
        """Record a successful request, resetting consecutive 429 counter.

        Should be called when a request completes successfully (not 429).
        """
        self._consecutive_429s = 0

    def get_backoff_remaining(self) -> float:
        """Get remaining backoff time in seconds.

        Returns:
            Seconds remaining in backoff period (0.0 if not in backoff)
        """
        now = datetime.now(UTC)
        if now >= self._backoff_until:
            return 0.0
        return (self._backoff_until - now).total_seconds()

    def get_available_capacity(self, request_type: RequestType) -> int:
        """Get number of requests that can be made immediately.

        Args:
            request_type: "order" or "query"

        Returns:
            Number of available request slots
        """
        now = datetime.now(UTC)
        if now < self._backoff_until:
            return 0

        self._cleanup_old_timestamps(now)
        count = self._get_current_count(request_type)
        limit = self._get_limit(request_type)
        return max(0, limit - count)

    def reset(self) -> None:
        """Reset all rate limit state.

        Useful for testing or when credentials change.
        """
        self._order_timestamps.clear()
        self._query_timestamps.clear()
        self._backoff_until = datetime.min.replace(tzinfo=UTC)
        self._consecutive_429s = 0

    def _cleanup_old_timestamps(self, now: datetime) -> None:
        """Remove timestamps outside the sliding window."""
        window_start = now - timedelta(seconds=self.config.window_seconds)

        for timestamps in [self._order_timestamps, self._query_timestamps]:
            while timestamps and timestamps[0] < window_start:
                timestamps.popleft()

    def _get_timestamps(self, request_type: RequestType) -> deque:
        """Get the timestamp deque for the request type."""
        if request_type == "order":
            return self._order_timestamps
        return self._query_timestamps

    def _get_current_count(self, request_type: RequestType) -> int:
        """Get current request count within the window."""
        return len(self._get_timestamps(request_type))

    def _get_limit(self, request_type: RequestType) -> int:
        """Get the rate limit for the request type."""
        if request_type == "order":
            return self.config.order_rate
        return self.config.query_rate
