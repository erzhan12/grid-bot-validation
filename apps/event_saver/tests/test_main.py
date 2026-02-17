"""Tests for EventSaver main orchestrator."""

import asyncio
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from event_saver.main import EventSaver
from event_saver.config import EventSaverConfig
from event_saver.collectors import AccountContext
from gridcore.events import TickerEvent, PublicTradeEvent, ExecutionEvent, OrderUpdateEvent, EventType


@pytest.fixture
def config():
    """Minimal EventSaverConfig for testing."""
    return EventSaverConfig(
        symbols="BTCUSDT",
        testnet=True,
        batch_size=10,
        flush_interval=1.0,
        gap_threshold_seconds=5.0,
        database_url="sqlite:///:memory:",
    )


@pytest.fixture
def mock_db():
    """Mock DatabaseFactory."""
    return MagicMock()


@pytest.fixture
def saver(config, mock_db):
    """EventSaver instance with mocked dependencies."""
    return EventSaver(config=config, db=mock_db)


@pytest.fixture
def account_context():
    """Sample AccountContext."""
    return AccountContext(
        account_id=uuid4(),
        user_id=uuid4(),
        run_id=uuid4(),
        api_key="test_key",
        api_secret="test_secret",
        environment="testnet",
        symbols=["BTCUSDT"],
    )


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestInit:
    def test_initializes_components_to_none(self, saver):
        assert saver._public_collector is None
        assert saver._trade_writer is None
        assert saver._ticker_writer is None
        assert saver._execution_writer is None
        assert saver._order_writer is None
        assert saver._position_writer is None
        assert saver._wallet_writer is None
        assert saver._reconciler is None
        assert saver._running is False

    def test_stores_config_and_db(self, saver, config, mock_db):
        assert saver._config is config
        assert saver._db is mock_db

    def test_private_collectors_empty(self, saver):
        assert saver._private_collectors == {}


# ---------------------------------------------------------------------------
# add_account
# ---------------------------------------------------------------------------


class TestAddAccount:
    @pytest.mark.asyncio
    async def test_adds_account(self, saver, account_context):
        await saver.add_account(account_context)

        assert account_context.account_id in saver._private_collectors

    @pytest.mark.asyncio
    async def test_duplicate_account_ignored(self, saver, account_context):
        await saver.add_account(account_context)
        await saver.add_account(account_context)

        assert len(saver._private_collectors) == 1

    @pytest.mark.asyncio
    async def test_warns_without_run_id(self, saver, account_context, caplog):
        account_context.run_id = None

        import logging
        with caplog.at_level(logging.WARNING):
            await saver.add_account(account_context)

        assert "NOT persisted" in caplog.text

    @pytest.mark.asyncio
    async def test_starts_collector_if_running(self, saver, account_context):
        saver._running = True
        saver._event_loop = asyncio.get_running_loop()

        with patch("event_saver.main.PrivateCollector") as MockCollector:
            mock_instance = MagicMock()
            mock_instance.start = AsyncMock()
            MockCollector.return_value = mock_instance

            await saver.add_account(account_context)

            mock_instance.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_start_collector_if_not_running(self, saver, account_context):
        assert saver._running is False

        with patch("event_saver.main.PrivateCollector") as MockCollector:
            mock_instance = MagicMock()
            mock_instance.start = AsyncMock()
            MockCollector.return_value = mock_instance

            await saver.add_account(account_context)

            mock_instance.start.assert_not_awaited()


# ---------------------------------------------------------------------------
# remove_account
# ---------------------------------------------------------------------------


class TestRemoveAccount:
    @pytest.mark.asyncio
    async def test_removes_account(self, saver, account_context):
        await saver.add_account(account_context)
        saver.remove_account(account_context.account_id)

        assert account_context.account_id not in saver._private_collectors

    def test_remove_nonexistent_account_noop(self, saver):
        saver.remove_account(uuid4())  # No error


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.mark.asyncio
    async def test_starts_writers_and_public_collector(self, saver):
        with patch("event_saver.main.TradeWriter") as MockTW, \
             patch("event_saver.main.TickerWriter") as MockTickW, \
             patch("event_saver.main.ExecutionWriter") as MockEW, \
             patch("event_saver.main.OrderWriter") as MockOW, \
             patch("event_saver.main.PositionWriter") as MockPW, \
             patch("event_saver.main.WalletWriter") as MockWW, \
             patch("event_saver.main.PublicCollector") as MockPC, \
             patch("event_saver.main.GapReconciler"), \
             patch("event_saver.main.BybitRestClient"):

            for mock_cls in [MockTW, MockTickW, MockEW, MockOW, MockPW, MockWW]:
                instance = MagicMock()
                instance.start_auto_flush = AsyncMock()
                mock_cls.return_value = instance

            pc_instance = MagicMock()
            pc_instance.start = AsyncMock()
            MockPC.return_value = pc_instance

            await saver.start()

            assert saver._running is True
            assert saver._event_loop is not None
            pc_instance.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_if_already_running(self, saver):
        saver._running = True

        await saver.start()
        # Should not crash, just warn and return

    @pytest.mark.asyncio
    async def test_starts_private_collectors(self, saver, account_context):
        await saver.add_account(account_context)

        with patch("event_saver.main.TradeWriter") as MockTW, \
             patch("event_saver.main.TickerWriter") as MockTickW, \
             patch("event_saver.main.ExecutionWriter") as MockEW, \
             patch("event_saver.main.OrderWriter") as MockOW, \
             patch("event_saver.main.PositionWriter") as MockPW, \
             patch("event_saver.main.WalletWriter") as MockWW, \
             patch("event_saver.main.PublicCollector") as MockPC, \
             patch("event_saver.main.GapReconciler"), \
             patch("event_saver.main.BybitRestClient"):

            for mock_cls in [MockTW, MockTickW, MockEW, MockOW, MockPW, MockWW]:
                instance = MagicMock()
                instance.start_auto_flush = AsyncMock()
                mock_cls.return_value = instance

            pc_instance = MagicMock()
            pc_instance.start = AsyncMock()
            MockPC.return_value = pc_instance

            # Mock the already-added private collector
            mock_priv = saver._private_collectors[account_context.account_id]
            mock_priv.start = AsyncMock()

            await saver.start()

            mock_priv.start.assert_awaited_once()


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.mark.asyncio
    async def test_stops_all_components(self, saver):
        saver._running = True

        # Set up mock writers
        mock_trade = MagicMock()
        mock_trade.stop = AsyncMock()
        saver._trade_writer = mock_trade

        mock_ticker = MagicMock()
        mock_ticker.stop = AsyncMock()
        saver._ticker_writer = mock_ticker

        mock_exec = MagicMock()
        mock_exec.stop = AsyncMock()
        saver._execution_writer = mock_exec

        mock_order = MagicMock()
        mock_order.stop = AsyncMock()
        saver._order_writer = mock_order

        mock_pos = MagicMock()
        mock_pos.stop = AsyncMock()
        saver._position_writer = mock_pos

        mock_wallet = MagicMock()
        mock_wallet.stop = AsyncMock()
        saver._wallet_writer = mock_wallet

        mock_public = MagicMock()
        mock_public.stop = AsyncMock()
        saver._public_collector = mock_public

        await saver.stop()

        assert saver._running is False
        mock_trade.stop.assert_awaited_once()
        mock_ticker.stop.assert_awaited_once()
        mock_exec.stop.assert_awaited_once()
        mock_order.stop.assert_awaited_once()
        mock_pos.stop.assert_awaited_once()
        mock_wallet.stop.assert_awaited_once()
        mock_public.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_if_not_running(self, saver):
        assert saver._running is False
        await saver.stop()  # No error


# ---------------------------------------------------------------------------
# Handler methods
# ---------------------------------------------------------------------------


class TestHandlers:
    @pytest.fixture(autouse=True)
    def saver_with_loop(self, saver):
        """Set up saver with event loop; close loop on teardown."""
        loop = asyncio.new_event_loop()
        saver._event_loop = loop
        saver._running = True
        yield
        loop.close()

    def test_handle_ticker_schedules_write(self, saver):
        mock_writer = MagicMock()
        mock_writer.write = AsyncMock()
        saver._ticker_writer = mock_writer

        now = datetime(2025, 1, 1)
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=now,
            local_ts=now,
            last_price=100000.0,
        )

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_ticker(event)
            mock_rcts.assert_called_once()
            coro = mock_rcts.call_args[0][0]
            coro.close()

    def test_handle_ticker_noop_without_writer(self, saver):
        saver._ticker_writer = None

        now = datetime(2025, 1, 1)
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=now,
            local_ts=now,
            last_price=100000.0,
        )

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_ticker(event)
            mock_rcts.assert_not_called()

    def test_handle_trades_schedules_write(self, saver):
        mock_writer = MagicMock()
        mock_writer.write = AsyncMock()
        saver._trade_writer = mock_writer

        now = datetime(2025, 1, 1)
        events = [
            PublicTradeEvent(
                event_type=EventType.PUBLIC_TRADE,
                symbol="BTCUSDT",
                exchange_ts=now,
                local_ts=now,
                trade_id="t1",
                side="Buy",
                price=100000.0,
                size=0.1,
            ),
        ]

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_trades(events)
            mock_rcts.assert_called_once()
            coro = mock_rcts.call_args[0][0]
            coro.close()

    def test_handle_trades_noop_on_empty_list(self, saver):
        saver._trade_writer = MagicMock()

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_trades([])
            mock_rcts.assert_not_called()

    def test_handle_execution_schedules_write(self, saver):
        mock_writer = MagicMock()
        mock_writer.write = AsyncMock()
        saver._execution_writer = mock_writer

        now = datetime(2025, 1, 1)
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=now,
            local_ts=now,
            exec_id="e1",
            order_id="o1",
            price=100000.0,
            qty=0.1,
        )

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_execution(event)
            mock_rcts.assert_called_once()
            coro = mock_rcts.call_args[0][0]
            coro.close()

    def test_handle_order_schedules_write(self, saver):
        mock_writer = MagicMock()
        mock_writer.write = AsyncMock()
        saver._order_writer = mock_writer

        now = datetime(2025, 1, 1)
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=now,
            local_ts=now,
            order_id="o1",
            status="New",
        )

        account_id = uuid4()
        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_order(account_id, event)
            mock_rcts.assert_called_once()
            coro = mock_rcts.call_args[0][0]
            coro.close()

    def test_handle_position_schedules_write(self, saver):
        mock_writer = MagicMock()
        mock_writer.write = AsyncMock()
        saver._position_writer = mock_writer

        message = {"data": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}]}
        account_id = uuid4()

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_position(account_id, message)
            mock_rcts.assert_called_once()
            coro = mock_rcts.call_args[0][0]
            coro.close()

    def test_handle_wallet_schedules_write(self, saver):
        mock_writer = MagicMock()
        mock_writer.write = AsyncMock()
        saver._wallet_writer = mock_writer

        message = {"data": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]}
        account_id = uuid4()

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_wallet(account_id, message)
            mock_rcts.assert_called_once()
            coro = mock_rcts.call_args[0][0]
            coro.close()


# ---------------------------------------------------------------------------
# Gap handling
# ---------------------------------------------------------------------------


class TestGapHandling:
    @pytest.fixture(autouse=True)
    def saver_with_loop(self, saver):
        """Set up saver with event loop; close loop on teardown."""
        loop = asyncio.new_event_loop()
        saver._event_loop = loop
        yield
        loop.close()

    def test_handle_public_gap_triggers_reconciliation(self, saver):
        saver._reconciler = MagicMock()
        saver._reconciler.reconcile_public_trades = AsyncMock()

        gap_start = datetime(2025, 1, 1, 0, 0, 0)
        gap_end = datetime(2025, 1, 1, 0, 0, 10)

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_public_gap("BTCUSDT", gap_start, gap_end)
            mock_rcts.assert_called_once()
            coro = mock_rcts.call_args[0][0]
            coro.close()

    def test_handle_private_gap_triggers_reconciliation(self, saver, account_context):
        saver._reconciler = MagicMock()
        saver._reconciler.reconcile_executions = AsyncMock()

        gap_start = datetime(2025, 1, 1, 0, 0, 0)
        gap_end = datetime(2025, 1, 1, 0, 0, 10)

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_private_gap(account_context, gap_start, gap_end)
            assert mock_rcts.call_count == 1  # One symbol: BTCUSDT
            coro = mock_rcts.call_args[0][0]
            coro.close()

    def test_handle_public_gap_noop_without_reconciler(self, saver):
        saver._reconciler = None

        with patch("event_saver.main.asyncio.run_coroutine_threadsafe") as mock_rcts:
            saver._handle_public_gap("BTCUSDT", datetime.now(), datetime.now())
            mock_rcts.assert_not_called()


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


class TestGetStats:
    def test_returns_basic_stats(self, saver):
        stats = saver.get_stats()

        assert stats["running"] is False
        assert stats["symbols"] == ["BTCUSDT"]
        assert stats["accounts"] == 0

    def test_includes_writer_stats_when_available(self, saver):
        mock_writer = MagicMock()
        mock_writer.get_stats.return_value = {"total_writes": 100}
        saver._trade_writer = mock_writer

        stats = saver.get_stats()

        assert stats["trade_writer"] == {"total_writes": 100}
