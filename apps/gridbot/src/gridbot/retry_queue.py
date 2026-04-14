"""Retry queue for failed intent execution.

Handles failed order placement and cancellation with exponential backoff.
The queue has no background thread — its owner calls ``process_due()``
from a polling loop. This matches the bbu2-style single-threaded
orchestrator (see 0017_PLAN.md).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from typing import Callable, Optional

from gridcore.intents import PlaceLimitIntent, CancelIntent


logger = logging.getLogger(__name__)


@dataclass
class RetryItem:
    """Item in the retry queue."""

    intent: PlaceLimitIntent | CancelIntent
    attempt_count: int = 0
    first_attempt_ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    next_retry_ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: str = ""

    def increment_attempt(self, error: str, backoff_seconds: float) -> None:
        """Increment attempt count and set next retry time.

        Args:
            error: Error message from last attempt.
            backoff_seconds: Seconds to wait before next retry.
        """
        self.attempt_count += 1
        self.last_error = error
        self.next_retry_ts = datetime.now(UTC) + timedelta(seconds=backoff_seconds)

    def is_due(self) -> bool:
        """Check if item is due for retry."""
        return datetime.now(UTC) >= self.next_retry_ts

    def elapsed_seconds(self) -> float:
        """Get seconds elapsed since first attempt."""
        return (datetime.now(UTC) - self.first_attempt_ts).total_seconds()


class RetryQueue:
    """Queue for retrying failed intents with exponential backoff.

    Items are retried up to max_attempts times or until max_elapsed_seconds
    has passed since the first attempt.

    There is no background thread: the owner must call ``process_due()``
    periodically (the orchestrator ticks it from its main loop).

    Example:
        queue = RetryQueue(
            executor_func=executor.execute_place,
            max_attempts=3,
            max_elapsed_seconds=30,
        )

        # Add failed intent to queue
        queue.add(intent, "Rate limited")

        # Process due items (call periodically from a main loop)
        queue.process_due()
    """

    def __init__(
        self,
        executor_func: Callable,
        max_attempts: int = 3,
        max_elapsed_seconds: float = 30.0,
        initial_backoff_seconds: float = 1.0,
        backoff_multiplier: float = 2.0,
        is_paused: Optional[Callable[[], bool]] = None,
    ):
        """Initialize retry queue.

        Args:
            executor_func: Function to call for retry (takes intent, returns result with success attr).
            max_attempts: Maximum retry attempts per intent.
            max_elapsed_seconds: Maximum seconds since first attempt.
            initial_backoff_seconds: Initial backoff delay.
            backoff_multiplier: Multiplier for exponential backoff.
            is_paused: Optional callable returning True to skip processing (e.g. auth cooldown).
        """
        self._executor_func = executor_func
        self._max_attempts = max_attempts
        self._max_elapsed_seconds = max_elapsed_seconds
        self._initial_backoff = initial_backoff_seconds
        self._backoff_multiplier = backoff_multiplier
        self._is_paused = is_paused or (lambda: False)

        self._queue: list[RetryItem] = []

    @property
    def size(self) -> int:
        """Number of items in queue."""
        return len(self._queue)

    def add(self, intent: PlaceLimitIntent | CancelIntent, error: str) -> None:
        """Add a failed intent to the retry queue.

        Args:
            intent: The failed intent.
            error: Error message from the failure.
        """
        item = RetryItem(
            intent=intent,
            attempt_count=1,  # First attempt already happened
            last_error=error,
            next_retry_ts=datetime.now(UTC) + timedelta(seconds=self._initial_backoff),
        )
        self._queue.append(item)

        logger.info(
            f"Added to retry queue: {type(intent).__name__} "
            f"(next retry in {self._initial_backoff}s)"
        )

    def remove(self, intent: PlaceLimitIntent | CancelIntent) -> bool:
        """Remove an intent from the queue.

        Args:
            intent: The intent to remove.

        Returns:
            True if intent was found and removed.
        """
        for i, item in enumerate(self._queue):
            if item.intent == intent:
                self._queue.pop(i)
                return True
        return False

    def clear(self) -> int:
        """Clear all items from the queue.

        Returns:
            Number of items cleared.
        """
        count = len(self._queue)
        self._queue.clear()
        return count

    def process_due(self) -> int:
        """Process all due items in the queue.

        Returns:
            Number of items processed (success or permanently failed).
        """
        if self._is_paused():
            return 0

        processed = 0
        items_to_remove = []

        for item in self._queue:
            if not item.is_due():
                continue

            # Check if we've exceeded limits
            if item.attempt_count >= self._max_attempts:
                logger.warning(
                    f"Retry exhausted (max attempts): {type(item.intent).__name__} "
                    f"after {item.attempt_count} attempts. Last error: {item.last_error}"
                )
                items_to_remove.append(item)
                processed += 1
                continue

            if item.elapsed_seconds() >= self._max_elapsed_seconds:
                logger.warning(
                    f"Retry exhausted (max time): {type(item.intent).__name__} "
                    f"after {item.elapsed_seconds():.1f}s. Last error: {item.last_error}"
                )
                items_to_remove.append(item)
                processed += 1
                continue

            # Re-check pause before each retry (cooldown may have activated mid-batch)
            if self._is_paused():
                break

            # Attempt retry
            logger.info(
                f"Retrying {type(item.intent).__name__} "
                f"(attempt {item.attempt_count + 1}/{self._max_attempts})"
            )

            try:
                result = self._executor_func(item.intent)

                if result.success:
                    logger.info(f"Retry succeeded: {type(item.intent).__name__}")
                    items_to_remove.append(item)
                    processed += 1
                else:
                    # Calculate backoff
                    backoff = self._initial_backoff * (
                        self._backoff_multiplier ** item.attempt_count
                    )
                    item.increment_attempt(result.error or "Unknown error", backoff)
                    logger.info(
                        f"Retry failed, will retry in {backoff:.1f}s: {result.error}"
                    )

            except Exception as e:
                backoff = self._initial_backoff * (
                    self._backoff_multiplier ** item.attempt_count
                )
                item.increment_attempt(str(e), backoff)
                logger.error(f"Retry exception: {e}")

        # Remove processed items
        for item in items_to_remove:
            self._queue.remove(item)

        return processed
