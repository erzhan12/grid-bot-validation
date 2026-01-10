"""Efficient bulk writer for public ticker snapshots."""

import asyncio
import logging
from collections import deque
from datetime import datetime, UTC
from typing import Optional

from grid_db import DatabaseFactory, TickerSnapshot, TickerSnapshotRepository
from gridcore.events import TickerEvent


logger = logging.getLogger(__name__)


class TickerWriter:
    """Buffers and bulk-inserts ticker snapshots.

    Uses the same buffering pattern as TradeWriter/ExecutionWriter.
    """

    def __init__(
        self,
        db: DatabaseFactory,
        batch_size: int = 100,
        flush_interval: float = 5.0,
    ):
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._buffer: deque[TickerEvent] = deque()
        self._last_flush: datetime = datetime.now(UTC)
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()

        self._total_written = 0
        self._flush_count = 0

    async def write(self, events: list[TickerEvent]) -> None:
        async with self._lock:
            self._buffer.extend(events)
            if len(self._buffer) >= self._batch_size:
                await self._flush_internal()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_internal()

    async def _flush_internal(self) -> None:
        if not self._buffer:
            return

        events = list(self._buffer)
        self._buffer.clear()
        self._last_flush = datetime.now(UTC)

        try:
            models = self._events_to_models(events)
            with self._db.get_session() as session:
                repo = TickerSnapshotRepository(session)
                inserted = repo.bulk_insert(models)
                self._total_written += inserted
                self._flush_count += 1
        except Exception as e:
            logger.error(f"Error flushing tickers to database: {e}")
            self._buffer.extendleft(reversed(events))

    async def start_auto_flush(self) -> None:
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._auto_flush_loop())

    async def stop(self) -> None:
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        await self.flush()

    async def _auto_flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                async with self._lock:
                    elapsed = (datetime.now(UTC) - self._last_flush).total_seconds()
                    if elapsed >= self._flush_interval and self._buffer:
                        await self._flush_internal()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in ticker auto-flush loop: {e}")

    def _events_to_models(self, events: list[TickerEvent]) -> list[TickerSnapshot]:
        return [
            TickerSnapshot(
                symbol=e.symbol,
                exchange_ts=e.exchange_ts,
                local_ts=e.local_ts,
                last_price=e.last_price,
                mark_price=e.mark_price,
                bid1_price=e.bid1_price,
                ask1_price=e.ask1_price,
                funding_rate=e.funding_rate,
                raw_json=None,
            )
            for e in events
        ]

    def get_stats(self) -> dict:
        return {
            "total_written": self._total_written,
            "flush_count": self._flush_count,
            "buffer_size": len(self._buffer),
        }

