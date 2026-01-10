"""Efficient bulk writer for wallet balance snapshots."""

import asyncio
import logging
from collections import deque
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional
from uuid import UUID

from grid_db import DatabaseFactory, WalletSnapshot, WalletSnapshotRepository


logger = logging.getLogger(__name__)


class WalletWriter:
    """Buffers and bulk-inserts wallet balance snapshots.

    Optimized for wallet balance tracking with configurable
    batch size and flush interval.

    Responsibilities:
    - Buffer wallet snapshots up to batch_size
    - Flush on batch_size reached OR flush_interval elapsed
    - Use bulk_insert for efficient inserts
    - Handle database errors with retry logic

    Example:
        writer = WalletWriter(db, batch_size=50, flush_interval=10.0)
        await writer.start_auto_flush()

        # In wallet callback:
        await writer.write(account_id, wallet_messages)

        # On shutdown:
        await writer.stop()
    """

    def __init__(
        self,
        db: DatabaseFactory,
        batch_size: int = 50,
        flush_interval: float = 10.0,
    ):
        """Initialize wallet writer.

        Args:
            db: DatabaseFactory instance for session management.
            batch_size: Number of snapshots to buffer before bulk insert.
            flush_interval: Maximum seconds between flushes.
        """
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._buffer: deque[WalletSnapshot] = deque()
        self._last_flush: datetime = datetime.now(UTC)
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()

        # Stats
        self._total_written = 0
        self._flush_count = 0

    async def write(self, account_id: UUID, messages: list[dict]) -> None:
        """Parse wallet messages and add to buffer.

        Args:
            account_id: Account ID for tagging snapshots.
            messages: List of raw wallet messages from WebSocket.
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
                repo = WalletSnapshotRepository(session)
                count = repo.bulk_insert(snapshots)
                self._total_written += count
                self._flush_count += 1
                logger.debug(
                    f"Flushed {count} wallet snapshots to database "
                    f"(total: {self._total_written})"
                )
        except Exception as e:
            logger.error(f"Error flushing wallet snapshots to database: {e}")
            # Re-add snapshots to buffer for retry
            self._buffer.extendleft(reversed(snapshots))

    async def start_auto_flush(self) -> None:
        """Start background task for periodic flushing."""
        if self._running:
            return

        self._running = True
        self._flush_task = asyncio.create_task(self._auto_flush_loop())
        logger.info(
            f"WalletWriter auto-flush started (interval={self._flush_interval}s)"
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
            f"WalletWriter stopped. Total written: {self._total_written}, "
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
                logger.error(f"Error in wallet auto-flush loop: {e}")

    def _messages_to_models(
        self,
        account_id: UUID,
        messages: list[dict],
    ) -> list[WalletSnapshot]:
        """Convert raw wallet messages to ORM models.

        Args:
            account_id: Account ID for tagging.
            messages: List of wallet message dicts from WebSocket.

        Returns:
            List of WalletSnapshot ORM models.

        Message structure (from Bybit wallet stream):
        {
            "data": [{
                "coin": [{
                    "coin": "USDT",
                    "walletBalance": "10000.0",
                    "availableToWithdraw": "9500.0"
                }],
                "updateTime": "1704067200000"
            }]
        }
        """
        snapshots = []
        local_ts = datetime.now(UTC)

        for msg in messages:
            data = msg.get("data", [])
            for wallet_data in data:
                # Parse timestamp
                update_time_ms = int(wallet_data.get("updateTime", 0))
                exchange_ts = datetime.fromtimestamp(update_time_ms / 1000, tz=UTC)

                # Parse coin balances
                coins = wallet_data.get("coin", [])
                for coin_data in coins:
                    try:
                        snapshots.append(
                            WalletSnapshot(
                                account_id=str(account_id),
                                exchange_ts=exchange_ts,
                                local_ts=local_ts,
                                coin=coin_data.get("coin", ""),
                                wallet_balance=Decimal(
                                    str(coin_data.get("walletBalance", "0"))
                                ),
                                available_balance=Decimal(
                                    str(coin_data.get("availableToWithdraw", "0"))
                                ),
                                raw_json=coin_data,
                            )
                        )
                    except Exception as e:
                        logger.warning(f"Error parsing wallet snapshot: {e}")
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
