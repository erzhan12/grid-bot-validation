"""Tests for TradeWriter, ExecutionWriter, TickerWriter, OrderWriter, PositionWriter, and WalletWriter."""

import pytest
from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import MagicMock, patch, AsyncMock
from uuid import uuid4

from gridcore.events import (
    PublicTradeEvent,
    ExecutionEvent,
    OrderUpdateEvent,
    TickerEvent,
    EventType,
)
from grid_db import DatabaseFactory

from event_saver.writers import TradeWriter, ExecutionWriter
from event_saver.writers.ticker_writer import TickerWriter
from event_saver.writers.order_writer import OrderWriter
from event_saver.writers.position_writer import PositionWriter
from event_saver.writers.wallet_writer import WalletWriter


@pytest.fixture
def mock_db():
    """Create mock DatabaseFactory."""
    db = MagicMock(spec=DatabaseFactory)
    session = MagicMock()
    db.get_session.return_value.__enter__ = MagicMock(return_value=session)
    db.get_session.return_value.__exit__ = MagicMock(return_value=False)
    return db


@pytest.fixture
def sample_trade_events():
    """Create sample trade events."""
    return [
        PublicTradeEvent(
            event_type=EventType.PUBLIC_TRADE,
            symbol="BTCUSDT",
            trade_id=f"trade_{i}",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            side="Buy" if i % 2 == 0 else "Sell",
            price=Decimal("50000.00"),
            size=Decimal("0.001"),
        )
        for i in range(5)
    ]


@pytest.fixture
def sample_execution_events():
    """Create sample execution events with run_id (required by model)."""
    user_id = uuid4()
    account_id = uuid4()
    run_id = uuid4()

    return [
        ExecutionEvent(
            event_type=EventType.EXECUTION,
            user_id=user_id,
            account_id=account_id,
            run_id=run_id,  # Required for model conversion
            exec_id=f"exec_{i}",
            order_id=f"order_{i}",
            order_link_id=f"link_{i}",
            symbol="BTCUSDT",
            side="Buy" if i % 2 == 0 else "Sell",
            price=Decimal("50000.00"),
            qty=Decimal("0.001"),
            fee=Decimal("0.01"),
            closed_pnl=Decimal("0"),
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
        )
        for i in range(5)
    ]


@pytest.fixture
def sample_ticker_events():
    """Create sample ticker events."""
    from datetime import timedelta

    now = datetime.now(UTC)
    return [
        TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=now,
            local_ts=now,
            last_price=Decimal("50000.00"),
            mark_price=Decimal("50001.00"),
            bid1_price=Decimal("49999.00"),
            ask1_price=Decimal("50002.00"),
            funding_rate=Decimal("0.0001"),
        ),
        TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=now + timedelta(milliseconds=1),
            local_ts=now + timedelta(milliseconds=1),
            last_price=Decimal("50010.00"),
            mark_price=Decimal("50011.00"),
            bid1_price=Decimal("50009.00"),
            ask1_price=Decimal("50012.00"),
            funding_rate=Decimal("0.0002"),
        ),
    ]


class TestTradeWriter:
    """Tests for TradeWriter."""

    def test_initialization(self, mock_db):
        """Test TradeWriter initialization."""
        writer = TradeWriter(
            db=mock_db,
            batch_size=50,
            flush_interval=3.0,
        )

        assert writer._batch_size == 50
        assert writer._flush_interval == 3.0
        assert len(writer._buffer) == 0
        assert writer._total_written == 0

    @pytest.mark.asyncio
    async def test_write_buffers_events(self, mock_db, sample_trade_events):
        """Test that write adds events to buffer."""
        writer = TradeWriter(db=mock_db, batch_size=100)

        await writer.write(sample_trade_events)

        assert len(writer._buffer) == 5

    @pytest.mark.asyncio
    async def test_write_flushes_on_batch_size(self, mock_db, sample_trade_events):
        """Test that write flushes when batch size is reached."""
        writer = TradeWriter(db=mock_db, batch_size=3)

        with patch.object(writer, "_flush_internal", new_callable=AsyncMock) as mock_flush:
            await writer.write(sample_trade_events)
            mock_flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, mock_db, sample_trade_events):
        """Test that flush clears the buffer."""
        writer = TradeWriter(db=mock_db, batch_size=100)

        # Add events without triggering auto-flush
        writer._buffer.extend(sample_trade_events)

        # Mock the repository
        mock_repo = MagicMock()
        mock_repo.bulk_insert.return_value = 5

        with patch("event_saver.writers.trade_writer.PublicTradeRepository", return_value=mock_repo):
            await writer.flush()

        assert len(writer._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_updates_stats(self, mock_db, sample_trade_events):
        """Test that flush updates statistics."""
        writer = TradeWriter(db=mock_db, batch_size=100)
        writer._buffer.extend(sample_trade_events)

        mock_repo = MagicMock()
        mock_repo.bulk_insert.return_value = 5

        with patch("event_saver.writers.trade_writer.PublicTradeRepository", return_value=mock_repo):
            await writer.flush()

        assert writer._total_written == 5
        assert writer._flush_count == 1

    def test_get_stats(self, mock_db):
        """Test get_stats returns correct values."""
        writer = TradeWriter(db=mock_db)
        writer._total_written = 100
        writer._flush_count = 10
        writer._buffer.extend([MagicMock()] * 5)

        stats = writer.get_stats()

        assert stats["total_written"] == 100
        assert stats["flush_count"] == 10
        assert stats["buffer_size"] == 5

    def test_events_to_models(self, mock_db, sample_trade_events):
        """Test event to model conversion."""
        writer = TradeWriter(db=mock_db)

        models = writer._events_to_models(sample_trade_events)

        assert len(models) == 5
        assert models[0].symbol == "BTCUSDT"
        assert models[0].trade_id == "trade_0"
        assert models[0].price == Decimal("50000.00")

    @pytest.mark.asyncio
    async def test_requeues_on_db_error(self, mock_db, sample_trade_events):
        """Flush failure re-queues buffered trade events for retry."""
        writer = TradeWriter(db=mock_db, batch_size=100)

        await writer.write(sample_trade_events)
        assert len(writer._buffer) == 5

        with patch("event_saver.writers.trade_writer.PublicTradeRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.side_effect = Exception("db down")

            await writer.flush()
            assert len(writer._buffer) == 5

            mock_repo.bulk_insert.side_effect = None
            mock_repo.bulk_insert.return_value = 5

            await writer.flush()
            assert len(writer._buffer) == 0


class TestTickerWriter:
    """Tests for TickerWriter."""

    def test_initialization(self, mock_db):
        """Test TickerWriter initialization."""
        writer = TickerWriter(
            db=mock_db,
            batch_size=50,
            flush_interval=3.0,
        )

        assert writer._batch_size == 50
        assert writer._flush_interval == 3.0
        assert len(writer._buffer) == 0
        assert writer._total_written == 0

    @pytest.mark.asyncio
    async def test_write_buffers_events(self, mock_db, sample_ticker_events):
        """Test that write adds events to buffer."""
        writer = TickerWriter(db=mock_db, batch_size=100)

        await writer.write(sample_ticker_events)

        assert len(writer._buffer) == 2

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, mock_db, sample_ticker_events):
        """Test that flush clears buffer and calls repository."""
        writer = TickerWriter(db=mock_db, batch_size=100)
        writer._buffer.extend(sample_ticker_events)

        with patch("event_saver.writers.ticker_writer.TickerSnapshotRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.return_value = 2

            await writer.flush()

            assert len(writer._buffer) == 0
            assert mock_repo.bulk_insert.call_count == 1

    @pytest.mark.asyncio
    async def test_requeues_on_db_error(self, mock_db, sample_ticker_events):
        """Flush failure re-queues buffered ticker events for retry."""
        writer = TickerWriter(db=mock_db, batch_size=100)
        writer._buffer.extend(sample_ticker_events)

        with patch("event_saver.writers.ticker_writer.TickerSnapshotRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.side_effect = Exception("db down")

            await writer.flush()
            assert len(writer._buffer) == 2

            mock_repo.bulk_insert.side_effect = None
            mock_repo.bulk_insert.return_value = 2

            await writer.flush()
            assert len(writer._buffer) == 0


class TestExecutionWriter:
    """Tests for ExecutionWriter."""

    def test_initialization(self, mock_db):
        """Test ExecutionWriter initialization."""
        writer = ExecutionWriter(
            db=mock_db,
            batch_size=25,
            flush_interval=2.0,
        )

        assert writer._batch_size == 25
        assert writer._flush_interval == 2.0
        assert len(writer._buffer) == 0
        assert writer._total_written == 0

    @pytest.mark.asyncio
    async def test_write_buffers_events(self, mock_db, sample_execution_events):
        """Test that write adds events to buffer."""
        writer = ExecutionWriter(db=mock_db, batch_size=100)

        await writer.write(sample_execution_events)

        assert len(writer._buffer) == 5

    @pytest.mark.asyncio
    async def test_write_flushes_on_batch_size(self, mock_db, sample_execution_events):
        """Test that write flushes when batch size is reached."""
        writer = ExecutionWriter(db=mock_db, batch_size=3)

        with patch.object(writer, "_flush_internal", new_callable=AsyncMock) as mock_flush:
            await writer.write(sample_execution_events)
            mock_flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, mock_db, sample_execution_events):
        """Test that flush clears the buffer."""
        writer = ExecutionWriter(db=mock_db, batch_size=100)
        writer._buffer.extend(sample_execution_events)

        mock_repo = MagicMock()
        mock_repo.bulk_insert.return_value = 5

        with patch("event_saver.writers.execution_writer.PrivateExecutionRepository", return_value=mock_repo):
            await writer.flush()

        assert len(writer._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_tracks_duplicates(self, mock_db, sample_execution_events):
        """Test that flush tracks duplicate count."""
        writer = ExecutionWriter(db=mock_db, batch_size=100)
        writer._buffer.extend(sample_execution_events)

        mock_repo = MagicMock()
        # Simulate 2 duplicates skipped
        mock_repo.bulk_insert.return_value = 3

        with patch("event_saver.writers.execution_writer.PrivateExecutionRepository", return_value=mock_repo):
            await writer.flush()

        assert writer._total_written == 3
        assert writer._duplicates_skipped == 2

    def test_get_stats(self, mock_db):
        """Test get_stats returns correct values including duplicates."""
        writer = ExecutionWriter(db=mock_db)
        writer._total_written = 50
        writer._flush_count = 5
        writer._duplicates_skipped = 3
        writer._buffer.extend([MagicMock()] * 2)

        stats = writer.get_stats()

        assert stats["total_written"] == 50
        assert stats["flush_count"] == 5
        assert stats["buffer_size"] == 2
        assert stats["duplicates_skipped"] == 3

    def test_events_to_models(self, mock_db, sample_execution_events):
        """Test event to model conversion."""
        writer = ExecutionWriter(db=mock_db)

        models = writer._events_to_models(sample_execution_events)

        assert len(models) == 5
        assert models[0].symbol == "BTCUSDT"
        assert models[0].exec_id == "exec_0"
        assert models[0].exec_price == Decimal("50000.00")
        assert models[0].run_id is not None
        assert models[0].account_id is not None

    def test_events_to_models_filters_without_run_id(self, mock_db):
        """Test that events without run_id are filtered out."""
        writer = ExecutionWriter(db=mock_db)

        # Create events without run_id
        events = [
            ExecutionEvent(
                event_type=EventType.EXECUTION,
                user_id=uuid4(),
                account_id=uuid4(),
                run_id=None,  # No run_id
                exec_id="exec_1",
                order_id="order_1",
                order_link_id="link_1",
                symbol="BTCUSDT",
                side="Buy",
                price=Decimal("50000.00"),
                qty=Decimal("0.001"),
                fee=Decimal("0.01"),
                closed_pnl=Decimal("0"),
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
            )
        ]

        models = writer._events_to_models(events)

        assert len(models) == 0

    @pytest.mark.asyncio
    async def test_requeues_on_db_error(self, mock_db, sample_execution_events):
        """Flush failure re-queues buffered execution events for retry."""
        writer = ExecutionWriter(db=mock_db, batch_size=100)

        await writer.write(sample_execution_events)
        assert len(writer._buffer) == 5

        with patch("event_saver.writers.execution_writer.PrivateExecutionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.side_effect = Exception("db down")

            await writer.flush()
            assert len(writer._buffer) == 5

            mock_repo.bulk_insert.side_effect = None
            mock_repo.bulk_insert.return_value = 5

            await writer.flush()
            assert len(writer._buffer) == 0

    @pytest.mark.asyncio
    async def test_start_stop_auto_flush(self, mock_db):
        """Test start and stop of auto-flush task."""
        writer = ExecutionWriter(db=mock_db, flush_interval=0.1)

        await writer.start_auto_flush()
        assert writer._running is True
        assert writer._flush_task is not None

        await writer.stop()
        assert writer._running is False
        assert writer._flush_task is None

class TestOrderWriter:
    """Test OrderWriter buffering and bulk insert."""

    def test_initialization(self, mock_db):
        """Test writer initialization."""
        writer = OrderWriter(db=mock_db, batch_size=50, flush_interval=10.0)

        assert writer._db == mock_db
        assert writer._batch_size == 50
        assert writer._flush_interval == 10.0
        assert len(writer._buffer) == 0

    @pytest.mark.asyncio
    async def test_write_buffers_events(self, mock_db):
        """Test that write adds events to buffer."""
        writer = OrderWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        events = [
            OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                order_id=f"order_{i}",
                order_link_id=f"link_{i}",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                status="New",
                side="Buy",
                price=Decimal("50000.00"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
                user_id=uuid4(),
                account_id=account_id,
                run_id=uuid4(),
            )
            for i in range(3)
        ]

        await writer.write(account_id, events)

        assert len(writer._buffer) == 3

    @pytest.mark.asyncio
    async def test_write_flushes_on_batch_size(self, mock_db):
        """Test that write flushes when batch size is reached."""
        writer = OrderWriter(db=mock_db, batch_size=2)

        account_id = uuid4()
        run_id = uuid4()
        events = [
            OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                order_id=f"order_{i}",
                order_link_id=f"link_{i}",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                status="New",
                side="Buy",
                price=Decimal("50000.00"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
                user_id=uuid4(),
                account_id=account_id,
                run_id=run_id,
            )
            for i in range(3)
        ]

        # Mock the repository
        with patch('event_saver.writers.order_writer.OrderRepository') as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.return_value = 3

            await writer.write(account_id, events)

            # Should have flushed all items when batch size reached (3 >= 2)
            assert len(writer._buffer) == 0
            assert mock_repo.bulk_insert.call_count == 1
            # Verify 3 orders were flushed
            call_args = mock_repo.bulk_insert.call_args[0][0]
            assert len(call_args) == 3

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, mock_db):
        """Test that flush clears the buffer."""
        writer = OrderWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        run_id = uuid4()
        events = [
            OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                order_id=f"order_{i}",
                order_link_id=f"link_{i}",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                status="New",
                side="Buy",
                price=Decimal("50000.00"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
                user_id=uuid4(),
                account_id=account_id,
                run_id=run_id,
            )
            for i in range(3)
        ]

        await writer.write(account_id, events)
        assert len(writer._buffer) == 3

        with patch('event_saver.writers.order_writer.OrderRepository') as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.return_value = 3

            await writer.flush()

            assert len(writer._buffer) == 0
            assert mock_repo.bulk_insert.call_count == 1

    @pytest.mark.asyncio
    async def test_filters_events_without_run_id(self, mock_db):
        """Test that events without run_id are filtered out."""
        writer = OrderWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        events = [
            OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                order_id="order_with_run",
                order_link_id="link_1",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                status="New",
                side="Buy",
                price=Decimal("50000.00"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
                user_id=uuid4(),
                account_id=account_id,
                run_id=uuid4(),  # Has run_id
            ),
            OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                order_id="order_without_run",
                order_link_id="link_2",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                status="New",
                side="Buy",
                price=Decimal("50000.00"),
                qty=Decimal("1.0"),
                leaves_qty=Decimal("1.0"),
                user_id=uuid4(),
                account_id=account_id,
                run_id=None,  # No run_id
            ),
        ]

        with patch('event_saver.writers.order_writer.OrderRepository') as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.return_value = 1

            await writer.write(account_id, events)
            await writer.flush()

            # Only 1 order should be persisted (the one with run_id)
            assert mock_repo.bulk_insert.call_count == 1
            call_args = mock_repo.bulk_insert.call_args[0][0]
            assert len(call_args) == 1
            assert call_args[0].order_id == "order_with_run"

    @pytest.mark.asyncio
    async def test_requeues_on_db_error(self, mock_db):
        """Flush failure re-queues buffered items for retry."""
        writer = OrderWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        run_id = uuid4()
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            order_id="order_1",
            order_link_id="link_1",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            status="New",
            side="Buy",
            price=Decimal("50000.00"),
            qty=Decimal("1.0"),
            leaves_qty=Decimal("1.0"),
            user_id=uuid4(),
            account_id=account_id,
            run_id=run_id,
        )

        await writer.write(account_id, [event])
        assert len(writer._buffer) == 1

        with patch("event_saver.writers.order_writer.OrderRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.side_effect = Exception("db down")

            # First flush fails -> item should be re-queued
            await writer.flush()
            assert len(writer._buffer) == 1

            # Second flush succeeds -> buffer cleared
            mock_repo.bulk_insert.side_effect = None
            mock_repo.bulk_insert.return_value = 1

            await writer.flush()
            assert len(writer._buffer) == 0
            assert mock_repo.bulk_insert.call_count == 2


class TestPositionWriter:
    """Test PositionWriter buffering and bulk insert."""

    def test_initialization(self, mock_db):
        """Test writer initialization."""
        writer = PositionWriter(db=mock_db, batch_size=50, flush_interval=10.0)

        assert writer._db == mock_db
        assert writer._batch_size == 50
        assert writer._flush_interval == 10.0
        assert len(writer._buffer) == 0

    @pytest.mark.asyncio
    async def test_write_buffers_snapshots(self, mock_db):
        """Test that write adds position snapshots to buffer."""
        writer = PositionWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        messages = [
            {
                "data": [
                    {
                        "category": "linear",
                        "symbol": f"SYMBOL{i}",
                        "side": "Buy",
                        "size": "1.0",
                        "entryPrice": "50000.00",
                        "liqPrice": "45000.00",
                        "unrealisedPnl": "100.50",
                        "updatedTime": "1700000000000",
                    }
                ]
            }
            for i in range(3)
        ]

        await writer.write(account_id, messages)

        assert len(writer._buffer) == 3

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, mock_db):
        """Test that flush clears the buffer."""
        writer = PositionWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        messages = [
            {
                "data": [
                    {
                        "category": "linear",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "1.0",
                        "entryPrice": "50000.00",
                        "liqPrice": "45000.00",
                        "unrealisedPnl": "100.50",
                        "updatedTime": "1700000000000",
                    }
                ]
            }
        ]

        await writer.write(account_id, messages)
        assert len(writer._buffer) == 1

        with patch('event_saver.writers.position_writer.PositionSnapshotRepository') as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.return_value = 1

            await writer.flush()

            assert len(writer._buffer) == 0
            assert mock_repo.bulk_insert.call_count == 1

    @pytest.mark.asyncio
    async def test_requeues_on_db_error(self, mock_db):
        """Flush failure re-queues buffered position snapshots for retry."""
        writer = PositionWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        messages = [
            {
                "data": [
                    {
                        "category": "linear",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "1.0",
                        "entryPrice": "50000.00",
                        "liqPrice": "45000.00",
                        "unrealisedPnl": "100.50",
                        "updatedTime": "1700000000000",
                    }
                ]
            }
        ]

        await writer.write(account_id, messages)
        assert len(writer._buffer) == 1

        with patch("event_saver.writers.position_writer.PositionSnapshotRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.side_effect = Exception("db down")

            await writer.flush()
            assert len(writer._buffer) == 1

            mock_repo.bulk_insert.side_effect = None
            mock_repo.bulk_insert.return_value = 1

            await writer.flush()
            assert len(writer._buffer) == 0


class TestWalletWriter:
    """Test WalletWriter buffering and bulk insert."""

    def test_initialization(self, mock_db):
        """Test writer initialization."""
        writer = WalletWriter(db=mock_db, batch_size=50, flush_interval=10.0)

        assert writer._db == mock_db
        assert writer._batch_size == 50
        assert writer._flush_interval == 10.0
        assert len(writer._buffer) == 0

    @pytest.mark.asyncio
    async def test_write_buffers_snapshots(self, mock_db):
        """Test that write adds wallet snapshots to buffer."""
        writer = WalletWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        messages = [
            {
                "data": [
                    {
                        "accountType": "UNIFIED",
                        "coin": [
                            {
                                "coin": "USDT",
                                "walletBalance": "10000.00",
                                "availableToWithdraw": "9500.00",
                            }
                        ],
                        "updateTime": "1700000000000",
                    }
                ]
            }
            for _ in range(3)
        ]

        await writer.write(account_id, messages)

        assert len(writer._buffer) == 3

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, mock_db):
        """Test that flush clears the buffer."""
        writer = WalletWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        messages = [
            {
                "data": [
                    {
                        "accountType": "UNIFIED",
                        "coin": [
                            {
                                "coin": "USDT",
                                "walletBalance": "10000.00",
                                "availableToWithdraw": "9500.00",
                            }
                        ],
                        "updateTime": "1700000000000",
                    }
                ]
            }
        ]

        await writer.write(account_id, messages)
        assert len(writer._buffer) == 1

        with patch('event_saver.writers.wallet_writer.WalletSnapshotRepository') as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.return_value = 1

            await writer.flush()

            assert len(writer._buffer) == 0
            assert mock_repo.bulk_insert.call_count == 1

    @pytest.mark.asyncio
    async def test_requeues_on_db_error(self, mock_db):
        """Flush failure re-queues buffered wallet snapshots for retry."""
        writer = WalletWriter(db=mock_db, batch_size=100)

        account_id = uuid4()
        messages = [
            {
                "data": [
                    {
                        "accountType": "UNIFIED",
                        "coin": [
                            {
                                "coin": "USDT",
                                "walletBalance": "10000.00",
                                "availableToWithdraw": "9500.00",
                            }
                        ],
                        "updateTime": "1700000000000",
                    }
                ]
            }
        ]

        await writer.write(account_id, messages)
        assert len(writer._buffer) == 1

        with patch("event_saver.writers.wallet_writer.WalletSnapshotRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.bulk_insert.side_effect = Exception("db down")

            await writer.flush()
            assert len(writer._buffer) == 1

            mock_repo.bulk_insert.side_effect = None
            mock_repo.bulk_insert.return_value = 1

            await writer.flush()
            assert len(writer._buffer) == 0
