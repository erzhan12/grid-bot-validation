"""Efficient bulk writer for position snapshots."""

import asyncio
import logging
from collections import deque
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional
from uuid import UUID

from grid_db import DatabaseFactory, PositionSnapshot, PositionSnapshotRepository


logger = logging.getLogger(__name__)


class PositionWriter:
    """Buffers and bulk-inserts position snapshots.

    Optimized for position state tracking with configurable
    batch size and flush interval.

    Responsibilities:
    - Buffer position snapshots up to batch_size
    - Flush on batch_size reached OR flush_interval elapsed
    - Use bulk_insert for efficient inserts
    - Handle database errors with retry logic

    Example:
        writer = PositionWriter(db, batch_size=50, flush_interval=10.0)
        await writer.start_auto_flush()

        # In position callback:
        await writer.write(account_id, position_messages)

        # On shutdown:
        await writer.stop()
    """

    def __init__(
        self,
        db: DatabaseFactory,
        batch_size: int = 50,
        flush_interval: float = 10.0,
    ):
        """Initialize position writer.

        Args:
            db: DatabaseFactory instance for session management.
            batch_size: Number of snapshots to buffer before bulk insert.
            flush_interval: Maximum seconds between flushes.
        """
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._buffer: deque[PositionSnapshot] = deque()
        self._last_flush: datetime = datetime.now(UTC)
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()

        # Stats
        self._total_written = 0
        self._flush_count = 0

    async def write(self, account_id: UUID, messages: list[dict]) -> None:
        """Parse position messages and add to buffer.

        Args:
            account_id: Account ID for tagging snapshots.
            messages: List of raw position messages from WebSocket.
        """
        snapshots = self._messages_to_models(account_id, messages)
        if not snapshots:
            return

        async with self._lock:
            self._buffer.extend(snapshots)

            # Flush if buffer exceeds batch size
            if len(self._buffer) >= self._batch_size:
                await self._flush_internal()

    async def flush(self) -> None:
        """Force flush buffered snapshots to database."""
        async with self._lock:
            await self._flush_internal()

    async def _flush_internal(self) -> None:
        """Internal flush without lock (must be called with lock held)."""
        if not self._buffer:
            return

        snapshots = list(self._buffer)
        self._buffer.clear()
        self._last_flush = datetime.now(UTC)

        try:
            with self._db.get_session() as session:
                repo = PositionSnapshotRepository(session)
                count = repo.bulk_insert(snapshots)
                self._total_written += count
                self._flush_count += 1
                logger.debug(
                    f"Flushed {count} position snapshots to database "
                    f"(total: {self._total_written})"
                )
        except Exception as e:
            logger.error(f"Error flushing position snapshots to database: {e}")
            # Re-add snapshots to buffer for retry
            self._buffer.extendleft(reversed(snapshots))

    async def start_auto_flush(self) -> None:
        """Start background task for periodic flushing."""
        if self._running:
            return

        self._running = True
        self._flush_task = asyncio.create_task(self._auto_flush_loop())
        logger.info(
            f"PositionWriter auto-flush started (interval={self._flush_interval}s)"
        )

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
            f"PositionWriter stopped. Total written: {self._total_written}, "
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
                logger.error(f"Error in position auto-flush loop: {e}")

    def _messages_to_models(
        self,
        account_id: UUID,
        messages: list[dict],
    ) -> list[PositionSnapshot]:
        """Convert raw position messages to ORM models.

        Args:
            account_id: Account ID for tagging.
            messages: List of position message dicts from WebSocket.

        Returns:
            List of PositionSnapshot ORM models.

        Message structure (from Bybit position stream):
        {
            "data": [{
                "symbol": "BTCUSDT",
                "side": "Buy",
                "size": "0.5",
                "entryPrice": "50000.0",
                "liqPrice": "45000.0",
                "unrealisedPnl": "500.0",
                "updatedTime": "1704067200000"
            }]
        }
        """
        snapshots = []
        local_ts = datetime.now(UTC)

        for msg in messages:
            data = msg.get("data", [])
            for pos in data:
                try:
                    # Parse timestamp
                    updated_time_ms = int(pos.get("updatedTime", 0))
                    exchange_ts = datetime.fromtimestamp(
                        updated_time_ms / 1000, tz=UTC
                    )

                    snapshots.append(
                        PositionSnapshot(
                            account_id=str(account_id),
                            symbol=pos.get("symbol", ""),
                            exchange_ts=exchange_ts,
                            local_ts=local_ts,
                            side=pos.get("side", ""),
                            size=Decimal(str(pos.get("size", "0"))),
                            entry_price=Decimal(str(pos.get("entryPrice", "0"))),
                            liq_price=(
                                Decimal(str(pos.get("liqPrice")))
                                if pos.get("liqPrice")
                                else None
                            ),
                            unrealised_pnl=(
                                Decimal(str(pos.get("unrealisedPnl")))
                                if pos.get("unrealisedPnl")
                                else None
                            ),
                            raw_json=pos,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Error parsing position snapshot: {e}")
                    continue

        return snapshots

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
