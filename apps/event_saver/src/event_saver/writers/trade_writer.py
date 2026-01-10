"""Efficient bulk writer for high-volume public trades."""

import asyncio
import logging
from collections import deque
from datetime import datetime, UTC
from typing import Optional

from grid_db import DatabaseFactory, PublicTrade, PublicTradeRepository
from gridcore.events import PublicTradeEvent


logger = logging.getLogger(__name__)


class TradeWriter:
    """Buffers and bulk-inserts public trades.

    Optimized for high-volume data ingestion with configurable
    batch size and flush interval.

    Responsibilities:
    - Buffer trades up to batch_size
    - Flush on batch_size reached OR flush_interval elapsed
    - Use bulk_save_objects for efficient inserts
    - Handle database errors with retry logic

    Example:
        writer = TradeWriter(db, batch_size=100, flush_interval=5.0)
        await writer.start_auto_flush()

        # In data callback:
        await writer.write(trade_events)

        # On shutdown:
        await writer.stop()
    """

    def __init__(
        self,
        db: DatabaseFactory,
        batch_size: int = 100,
        flush_interval: float = 5.0,
    ):
        """Initialize trade writer.

        Args:
            db: DatabaseFactory instance for session management.
            batch_size: Number of trades to buffer before bulk insert.
            flush_interval: Maximum seconds between flushes.
        """
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._buffer: deque[PublicTradeEvent] = deque()
        self._last_flush: datetime = datetime.now(UTC)
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()

        # Stats
        self._total_written = 0
        self._flush_count = 0

    async def write(self, events: list[PublicTradeEvent]) -> None:
        """Add events to buffer, flush if needed.

        Args:
            events: List of PublicTradeEvent to buffer.
        """
        async with self._lock:
            self._buffer.extend(events)

            # Flush if buffer exceeds batch size
            if len(self._buffer) >= self._batch_size:
                await self._flush_internal()

    async def flush(self) -> None:
        """Force flush buffered events to database."""
        async with self._lock:
            await self._flush_internal()

    async def _flush_internal(self) -> None:
        """Internal flush without lock (must be called with lock held)."""
        if not self._buffer:
            return

        events = list(self._buffer)
        self._buffer.clear()
        self._last_flush = datetime.now(UTC)

        try:
            models = self._events_to_models(events)
            with self._db.get_session() as session:
                repo = PublicTradeRepository(session)
                count = repo.bulk_insert(models)
                self._total_written += count
                self._flush_count += 1
                logger.debug(f"Flushed {count} trades to database (total: {self._total_written})")
        except Exception as e:
            logger.error(f"Error flushing trades to database: {e}")
            # Re-add events to buffer for retry
            self._buffer.extendleft(reversed(events))

    async def start_auto_flush(self) -> None:
        """Start background task for periodic flushing."""
        if self._running:
            return

        self._running = True
        self._flush_task = asyncio.create_task(self._auto_flush_loop())
        logger.info(f"TradeWriter auto-flush started (interval={self._flush_interval}s)")

    async def stop(self) -> None:
        """Stop auto-flush and flush remaining buffer."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        # Final flush
        await self.flush()
        logger.info(
            f"TradeWriter stopped. Total written: {self._total_written}, "
            f"Flushes: {self._flush_count}"
        )

    async def _auto_flush_loop(self) -> None:
        """Background loop for periodic flushing."""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)

                async with self._lock:
                    # Check if enough time has passed since last flush
                    elapsed = (datetime.now(UTC) - self._last_flush).total_seconds()
                    if elapsed >= self._flush_interval and self._buffer:
                        await self._flush_internal()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in auto-flush loop: {e}")

    def _events_to_models(self, events: list[PublicTradeEvent]) -> list[PublicTrade]:
        """Convert events to ORM models for bulk insert.

        Args:
            events: List of PublicTradeEvent.

        Returns:
            List of PublicTrade ORM models.
        """
        return [
            PublicTrade(
                symbol=event.symbol,
                trade_id=event.trade_id,
                exchange_ts=event.exchange_ts,
                local_ts=event.local_ts,
                side=event.side,
                price=event.price,
                size=event.size,
            )
            for event in events
        ]

    def get_stats(self) -> dict:
        """Get writer statistics.

        Returns:
            Dict with total_written, flush_count, buffer_size.
        """
        return {
            "total_written": self._total_written,
            "flush_count": self._flush_count,
            "buffer_size": len(self._buffer),
        }
