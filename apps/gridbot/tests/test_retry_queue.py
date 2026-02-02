"""Tests for gridbot retry queue module."""

import asyncio
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from unittest.mock import Mock, AsyncMock

import pytest

from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridbot.retry_queue import RetryQueue, RetryItem


@pytest.fixture
def place_intent():
    """Sample PlaceLimitIntent."""
    return PlaceLimitIntent.create(
        symbol="BTCUSDT",
        side="Buy",
        price=Decimal("50000.0"),
        qty=Decimal("0.001"),
        grid_level=10,
        direction="long",
    )


@pytest.fixture
def cancel_intent():
    """Sample CancelIntent."""
    return CancelIntent(
        symbol="BTCUSDT",
        order_id="order_123",
        reason="test",
    )


@pytest.fixture
def success_result():
    """Mock successful result."""
    result = Mock()
    result.success = True
    result.error = None
    return result


@pytest.fixture
def failure_result():
    """Mock failed result."""
    result = Mock()
    result.success = False
    result.error = "API error"
    return result


class TestRetryItem:
    """Tests for RetryItem dataclass."""

    def test_create_item(self, place_intent):
        """Test creating a retry item."""
        item = RetryItem(intent=place_intent)
        assert item.intent == place_intent
        assert item.attempt_count == 0
        assert item.last_error == ""

    def test_increment_attempt(self, place_intent):
        """Test incrementing attempt count."""
        item = RetryItem(intent=place_intent)
        item.increment_attempt("Rate limited", 2.0)

        assert item.attempt_count == 1
        assert item.last_error == "Rate limited"
        assert item.next_retry_ts > datetime.now(UTC)

    def test_is_due(self, place_intent):
        """Test is_due check."""
        item = RetryItem(intent=place_intent)
        # Default next_retry_ts is now, so should be due
        assert item.is_due() is True

        item.next_retry_ts = datetime.now(UTC) + timedelta(seconds=60)
        assert item.is_due() is False

    def test_elapsed_seconds(self, place_intent):
        """Test elapsed seconds calculation."""
        item = RetryItem(intent=place_intent)
        item.first_attempt_ts = datetime.now(UTC) - timedelta(seconds=10)

        elapsed = item.elapsed_seconds()
        assert 9.9 <= elapsed <= 10.1


class TestRetryQueueBasic:
    """Basic tests for RetryQueue."""

    def test_create_queue(self):
        """Test creating a queue."""
        queue = RetryQueue(executor_func=Mock())
        assert queue.size == 0
        assert queue.running is False

    def test_add_item(self, place_intent):
        """Test adding an item."""
        queue = RetryQueue(executor_func=Mock())
        queue.add(place_intent, "Error")

        assert queue.size == 1

    def test_remove_item(self, place_intent):
        """Test removing an item."""
        queue = RetryQueue(executor_func=Mock())
        queue.add(place_intent, "Error")

        removed = queue.remove(place_intent)
        assert removed is True
        assert queue.size == 0

    def test_remove_nonexistent(self, place_intent, cancel_intent):
        """Test removing nonexistent item."""
        queue = RetryQueue(executor_func=Mock())
        queue.add(place_intent, "Error")

        removed = queue.remove(cancel_intent)
        assert removed is False
        assert queue.size == 1

    def test_clear(self, place_intent, cancel_intent):
        """Test clearing the queue."""
        queue = RetryQueue(executor_func=Mock())
        queue.add(place_intent, "Error 1")
        queue.add(cancel_intent, "Error 2")

        count = queue.clear()
        assert count == 2
        assert queue.size == 0


class TestRetryQueueProcessing:
    """Tests for RetryQueue.process_due."""

    @pytest.mark.asyncio
    async def test_process_due_success(self, place_intent, success_result):
        """Test processing succeeds and removes item."""
        executor = Mock(return_value=success_result)
        queue = RetryQueue(
            executor_func=executor,
            max_attempts=3,
            initial_backoff_seconds=0.01,  # Fast for testing
        )
        queue.add(place_intent, "Initial error")

        # Wait for item to be due
        await asyncio.sleep(0.02)

        processed = await queue.process_due()

        assert processed == 1
        assert queue.size == 0
        executor.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_due_failure_retries(self, place_intent, failure_result):
        """Test processing failure increments attempt count."""
        executor = Mock(return_value=failure_result)
        queue = RetryQueue(
            executor_func=executor,
            max_attempts=3,
            initial_backoff_seconds=0.01,
        )
        queue.add(place_intent, "Initial error")

        # Wait for item to be due
        await asyncio.sleep(0.02)

        processed = await queue.process_due()

        # Item not removed, still in queue with incremented attempt
        assert processed == 0
        assert queue.size == 1
        assert queue._queue[0].attempt_count == 2  # 1 initial + 1 retry

    @pytest.mark.asyncio
    async def test_process_due_max_attempts(self, place_intent, failure_result):
        """Test item removed after max attempts."""
        executor = Mock(return_value=failure_result)
        queue = RetryQueue(
            executor_func=executor,
            max_attempts=2,
            initial_backoff_seconds=0.01,
        )
        queue.add(place_intent, "Initial error")

        # First retry
        await asyncio.sleep(0.02)
        await queue.process_due()
        assert queue.size == 1

        # Second retry - should be removed (max_attempts=2, we've now done 2)
        await asyncio.sleep(0.05)
        processed = await queue.process_due()

        assert processed == 1
        assert queue.size == 0

    @pytest.mark.asyncio
    async def test_process_due_max_elapsed(self, place_intent, failure_result):
        """Test item removed after max elapsed time."""
        executor = Mock(return_value=failure_result)
        queue = RetryQueue(
            executor_func=executor,
            max_attempts=100,  # High so time limit triggers first
            max_elapsed_seconds=0.05,
            initial_backoff_seconds=0.01,
        )
        queue.add(place_intent, "Initial error")

        # Wait for max elapsed time
        await asyncio.sleep(0.06)

        processed = await queue.process_due()

        assert processed == 1
        assert queue.size == 0

    @pytest.mark.asyncio
    async def test_process_due_not_due_yet(self, place_intent, success_result):
        """Test items not due are skipped."""
        executor = Mock(return_value=success_result)
        queue = RetryQueue(
            executor_func=executor,
            initial_backoff_seconds=10.0,  # Long backoff
        )
        queue.add(place_intent, "Initial error")

        # Process immediately (item not due yet)
        processed = await queue.process_due()

        assert processed == 0
        assert queue.size == 1
        executor.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_due_async_executor(self, place_intent, success_result):
        """Test processing with async executor function."""
        executor = AsyncMock(return_value=success_result)
        queue = RetryQueue(
            executor_func=executor,
            initial_backoff_seconds=0.01,
        )
        queue.add(place_intent, "Initial error")

        await asyncio.sleep(0.02)
        processed = await queue.process_due()

        assert processed == 1
        assert queue.size == 0
        executor.assert_called_once()


class TestRetryQueueBackgroundTask:
    """Tests for RetryQueue background task."""

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Test starting and stopping background task."""
        queue = RetryQueue(
            executor_func=Mock(),
            check_interval_seconds=0.01,
        )

        await queue.start()
        assert queue.running is True

        await queue.stop()
        assert queue.running is False

    @pytest.mark.asyncio
    async def test_background_processing(self, place_intent, success_result):
        """Test background task processes items."""
        executor = Mock(return_value=success_result)
        queue = RetryQueue(
            executor_func=executor,
            initial_backoff_seconds=0.01,
            check_interval_seconds=0.01,
        )

        queue.add(place_intent, "Initial error")
        await queue.start()

        # Wait for background processing
        await asyncio.sleep(0.05)

        await queue.stop()

        assert queue.size == 0
        executor.assert_called()

    @pytest.mark.asyncio
    async def test_double_start(self):
        """Test starting twice is idempotent."""
        queue = RetryQueue(
            executor_func=Mock(),
            check_interval_seconds=0.01,
        )

        await queue.start()
        await queue.start()  # Should be no-op

        assert queue.running is True

        await queue.stop()


class TestRetryQueueBackoff:
    """Tests for exponential backoff."""

    @pytest.mark.asyncio
    async def test_exponential_backoff(self, place_intent, failure_result):
        """Test backoff increases exponentially."""
        executor = Mock(return_value=failure_result)
        queue = RetryQueue(
            executor_func=executor,
            max_attempts=5,
            initial_backoff_seconds=1.0,
            backoff_multiplier=2.0,
        )
        queue.add(place_intent, "Initial error")

        # Force item to be due
        queue._queue[0].next_retry_ts = datetime.now(UTC)

        await queue.process_due()

        # Check backoff: initial(1.0) * multiplier^attempt = 1.0 * 2^1 = 2.0
        item = queue._queue[0]
        expected_retry = datetime.now(UTC) + timedelta(seconds=2.0)
        # Allow some tolerance
        assert abs((item.next_retry_ts - expected_retry).total_seconds()) < 0.1
