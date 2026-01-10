"""Order writer for buffering and bulk-inserting order updates."""

import asyncio
import logging
from collections import deque
from datetime import datetime, UTC
from typing import Optional
from uuid import UUID

from grid_db import DatabaseFactory, Order, OrderRepository
from gridcore.events import OrderUpdateEvent


logger = logging.getLogger(__name__)


class OrderWriter:
    """Buffers and bulk-inserts order updates.

    Maintains a buffer of order updates and flushes to database when:
    - Buffer size reaches batch_size
    - flush_interval seconds have elapsed since last flush
    - Manual flush() is called
    - Writer is stopped

    Example:
        writer = OrderWriter(db=db, batch_size=100, flush_interval=5.0)
        await writer.start_auto_flush()

        # Write orders
        await writer.write([order_event1, order_event2])

        # Later...
        await writer.stop()
    """

    def __init__(
        self,
        db: DatabaseFactory,
        batch_size: int = 100,
        flush_interval: float = 5.0,
    ):
        """Initialize order writer.

        Args:
            db: DatabaseFactory for database access.
            batch_size: Number of orders to buffer before auto-flush.
            flush_interval: Seconds between auto-flushes.
        """
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        # Buffer
        self._buffer: deque[tuple[UUID, OrderUpdateEvent]] = deque()
        self._lock = asyncio.Lock()

        # Auto-flush task
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        # Stats
        self._total_written = 0
        self._total_flushed = 0

    async def write(self, account_id: UUID, events: list[OrderUpdateEvent]) -> None:
        """Buffer order events for later flush.

        Args:
            account_id: Account ID for tagging.
            events: List of OrderUpdateEvent instances.
        """
        if not events:
            return

        async with self._lock:
            for event in events:
                self._buffer.append((account_id, event))

            # Auto-flush if buffer is full
            if len(self._buffer) >= self._batch_size:
                await self._flush_internal()

    async def flush(self) -> None:
        """Flush all buffered orders to database."""
        async with self._lock:
            await self._flush_internal()

    async def _flush_internal(self) -> None:
        """Internal flush (must be called with lock held)."""
        if not self._buffer:
            return

        items: list[tuple[UUID, OrderUpdateEvent]] = []
        while self._buffer:
            items.append(self._buffer.popleft())

        models: list[Order] = []
        retry_items: list[tuple[UUID, OrderUpdateEvent]] = []

        for account_id, event in items:
            # Skip events without run_id
            if not event.run_id:
                logger.warning(
                    f"Skipping order {event.order_id} without run_id "
                    f"(account={account_id})"
                )
                continue

            # Convert event to model
            try:
                models.append(
                    Order(
                    run_id=str(event.run_id),
                    account_id=str(account_id),
                    order_id=event.order_id,
                    order_link_id=event.order_link_id if event.order_link_id else None,
                    symbol=event.symbol,
                    exchange_ts=event.exchange_ts,
                    local_ts=datetime.now(UTC),
                    status=event.status,
                    side=event.side,
                    price=event.price,
                    qty=event.qty,
                    leaves_qty=event.leaves_qty,
                    raw_json=None,
                )
                )
                retry_items.append((account_id, event))
            except Exception as e:
                logger.error(f"Error converting order event to model: {e}")
                continue

        if not models:
            return

        # Bulk insert
        try:
            with self._db.get_session() as session:
                repo = OrderRepository(session)
                inserted = repo.bulk_insert(models)

                self._total_written += inserted
                self._total_flushed += 1

                logger.debug(
                    f"Flushed {inserted} orders to database "
                    f"(total written: {self._total_written})"
                )
        except Exception as e:
            logger.error(f"Error flushing orders to database: {e}")
            # Re-queue events for retry on transient DB errors (preserve order)
            self._buffer.extendleft(reversed(retry_items))

    async def _auto_flush_loop(self) -> None:
        """Background task to flush buffer periodically."""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    async def start_auto_flush(self) -> None:
        """Start background auto-flush task."""
        if self._running:
            return

        self._running = True
        self._flush_task = asyncio.create_task(self._auto_flush_loop())
        logger.debug("Order writer auto-flush started")

    async def stop(self) -> None:
        """Stop writer and flush remaining buffer."""
        if not self._running:
            return

        logger.debug("Stopping order writer...")
        self._running = False

        # Cancel auto-flush task
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self.flush()

        logger.info(
            f"Order writer stopped. "
            f"Total written: {self._total_written}, "
            f"Total flushes: {self._total_flushed}"
        )

    def get_stats(self) -> dict:
        """Get writer statistics.

        Returns:
            Dict with total_written, total_flushed, buffer_size.
        """
        return {
            "total_written": self._total_written,
            "total_flushed": self._total_flushed,
            "buffer_size": len(self._buffer),
        }
