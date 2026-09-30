"""Tests for recorder orchestrator."""

import asyncio
from concurrent.futures import Future
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional, Union
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from gridcore.events import (
    EventType,
    ExecutionEvent,
    OrderUpdateEvent,
    TickerEvent,
    PublicTradeEvent,
)

from event_saver.reconciler import ExecutionRecoveryResult
from grid_db import PublicTrade, RecoveryStatus, Run
from recorder.config import RecorderConfig
from event_saver.collectors import CollectorStartError
from recorder.recorder import Recorder

# Local sentinels for fallback-mode tests (no account: block configured).
# Match the placeholder UUIDs the recorder uses when self._config.account is None.
TEST_RECORDER_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
TEST_RECORDER_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000002")


def _mock_collectors(mock_pub_cls, mock_priv_cls) -> None:
    """Stub both collector classes with awaitable start/stop."""
    mock_pub = MagicMock()
    mock_pub.start = AsyncMock()
    mock_pub.stop = AsyncMock()
    mock_pub.get_connection_state.return_value = None
    mock_pub_cls.return_value = mock_pub
    mock_priv = MagicMock()
    mock_priv.start = AsyncMock()
    mock_priv.stop = AsyncMock()
    mock_priv_cls.return_value = mock_priv


async def await_future(fut: Union[Optional[Future], list[Future]]) -> None:
    """Deterministically await handler future(s) on the event loop."""
    if fut is None:
        return
    if isinstance(fut, list):
        for f in fut:
            await asyncio.wrap_future(f)
    else:
        await asyncio.wrap_future(fut)


@pytest.fixture
def make_ticker():
    """Factory for TickerEvent."""
    def _make(symbol="BTCUSDT", price="50000.0"):
        return TickerEvent(
            event_type=EventType.TICKER,
            symbol=symbol,
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal(price),
            mark_price=Decimal(price),
            bid1_price=Decimal(price),
            ask1_price=Decimal(price),
            funding_rate=Decimal("0.0001"),
        )
    return _make


@pytest.fixture
def make_trade():
    """Factory for PublicTradeEvent."""
    def _make(symbol="BTCUSDT", price="50000.0", trade_id="t1"):
        return PublicTradeEvent(
            event_type=EventType.PUBLIC_TRADE,
            symbol=symbol,
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            trade_id=trade_id,
            side="Buy",
            price=Decimal(price),
            size=Decimal("0.001"),
        )
    return _make


class TestRecorderStartStop:
    """Tests for Recorder lifecycle."""

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_start_creates_public_collector(
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        mock_pub_cls.assert_called_once()
        mock_pub.start.assert_awaited_once()

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_start_without_account_no_private_collector(
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        assert recorder._private_collector is None
        assert recorder._execution_writer is None

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_no_trade_writer_when_capture_disabled(
        self, mock_rest_cls, mock_pub_cls, db, make_trade
    ):
        """When capture_public_trades=False, trade writer is not created
        and _handle_trades does not attempt to write."""
        config = RecorderConfig(
            symbols=["BTCUSDT"],
            capture_public_trades=False,
            database_url="sqlite:///:memory:",
            testnet=True,
            batch_size=10,
            flush_interval=1.0,
            health_log_interval=60.0,
        )

        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config, db=db)
        await recorder.start()

        assert recorder._trade_writer is None
        assert recorder._ticker_writer is not None

        # on_trades should have been passed as None to PublicCollector
        call_kwargs = mock_pub_cls.call_args[1]
        assert call_kwargs["on_trades"] is None

        # Calling _handle_trades directly should be a no-op (returns None)
        events = [make_trade(trade_id=f"t{i}") for i in range(3)]
        result = recorder._handle_trades(events)
        assert result is None

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_collateral_symbols_merged_into_public_subscription(
        self, mock_rest_cls, mock_pub_cls, db
    ):
        """Feature 0065: collateral_symbols are added to the PublicCollector
        subscription set, de-duplicated and order-preserving (traded symbols
        first)."""
        config = RecorderConfig(
            symbols=["LTCUSDT"],
            collateral_symbols=["SOLUSDT", "LTCUSDT"],  # LTCUSDT dup collapses
            database_url="sqlite:///:memory:",
            testnet=True,
            batch_size=10,
            flush_interval=1.0,
            health_log_interval=60.0,
        )

        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config, db=db)
        await recorder.start()

        call_kwargs = mock_pub_cls.call_args[1]
        assert call_kwargs["symbols"] == ["LTCUSDT", "SOLUSDT"]

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_start_with_account_creates_private_collector(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        mock_priv_cls.assert_called_once()
        mock_priv.start.assert_awaited_once()
        assert recorder._execution_writer is not None
        assert recorder._order_writer is not None
        assert recorder._position_writer is not None
        assert recorder._wallet_writer is not None

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stop_flushes_writers(
        self, mock_rest_cls, mock_pub_cls, config_with_trades_enabled, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config_with_trades_enabled, db=db)
        await recorder.start()

        # Writers should be initialized
        assert recorder._trade_writer is not None
        assert recorder._ticker_writer is not None

        await recorder.stop()

        # After stop, running should be False
        assert recorder._running is False

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stop_idempotent(
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()
        await recorder.stop()
        # Second stop should not raise
        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_start_twice_warns(
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()
        # Second start should be no-op (already running)
        await recorder.start()
        # Still only one collector start
        assert mock_pub.start.await_count == 1

        await recorder.stop()


class TestRecorderHandlers:
    """Tests for data routing handlers."""

    @pytest.mark.parametrize("handler_name,args", [
        ("_handle_ticker", "ticker"),
        ("_handle_trades", "trades"),
        ("_handle_execution", "execution"),
        ("_handle_order", "order"),
        ("_handle_position", "position"),
        ("_handle_wallet", "wallet"),
        ("_handle_public_gap", "gap"),
        ("_handle_private_gap", "gap"),
    ])
    def test_handlers_before_start_are_safe_noops(
        self, handler_name, args, basic_config, db, make_ticker, make_trade
    ):
        """Calling any handler on a not-started Recorder must not raise."""
        recorder = Recorder(config=basic_config, db=db)
        handler = getattr(recorder, handler_name)

        now = datetime.now(UTC)
        if args == "ticker":
            handler(make_ticker())
        elif args == "trades":
            handler([make_trade()])
        elif args == "execution":
            handler(ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol="BTCUSDT",
                exchange_ts=now,
                local_ts=now,
                exec_id="e1",
                order_id="o1",
                side="Buy",
                price=Decimal("50000"),
                qty=Decimal("0.001"),
            ))
        elif args == "order":
            handler(TEST_RECORDER_ACCOUNT_ID, OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                exchange_ts=now,
                local_ts=now,
                order_id="o1",
                status="New",
                side="Buy",
                price=Decimal("50000"),
                qty=Decimal("0.001"),
            ))
        elif args == "position":
            handler(TEST_RECORDER_ACCOUNT_ID, {"data": []})
        elif args == "wallet":
            handler(TEST_RECORDER_ACCOUNT_ID, {"data": []})
        elif args == "gap":
            if handler_name == "_handle_public_gap":
                handler("BTCUSDT", now, now)
            else:
                handler(now, now)

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_ticker_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, basic_config, db, make_ticker
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        event = make_ticker()
        fut = recorder._handle_ticker(event)
        await await_future(fut)

        stats = recorder._ticker_writer.get_stats()
        assert stats["buffer_size"] >= 1 or stats["total_written"] >= 1

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_trades_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, config_with_trades_enabled, db, make_trade
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config_with_trades_enabled, db=db)
        await recorder.start()

        events = [make_trade(trade_id=f"t{i}") for i in range(5)]
        fut = recorder._handle_trades(events)
        await await_future(fut)

        stats = recorder._trade_writer.get_stats()
        assert stats["buffer_size"] + stats["total_written"] >= 5

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_trades_empty_list_is_noop(
        self, mock_rest_cls, mock_pub_cls, config_with_trades_enabled, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config_with_trades_enabled, db=db)
        await recorder.start()

        fut = recorder._handle_trades([])
        assert fut is None

        stats = recorder._trade_writer.get_stats()
        assert stats["buffer_size"] == 0
        assert stats["total_written"] == 0

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_public_trades_written_when_capture_enabled(
        self, mock_rest_cls, mock_pub_cls, config_with_trades_enabled, db, make_trade
    ):
        """When capture_public_trades=True, trade events are persisted to DB."""
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config_with_trades_enabled, db=db)
        await recorder.start()

        # Inject trade events
        events = [make_trade(trade_id=f"persist_{i}") for i in range(3)]
        fut = recorder._handle_trades(events)
        await await_future(fut)

        # Flush remaining buffer
        await recorder._trade_writer.flush()

        # Verify trades persisted to DB
        with db.get_session() as session:
            rows = session.query(PublicTrade).all()
            assert len(rows) == 3
            trade_ids = {r.trade_id for r in rows}
            assert trade_ids == {"persist_0", "persist_1", "persist_2"}

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_public_trades_not_written_when_capture_disabled(
        self, mock_rest_cls, mock_pub_cls, basic_config, db, make_trade
    ):
        """When capture_public_trades=False (default), no trades are written to DB."""
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        # Trade writer should not exist
        assert recorder._trade_writer is None

        # on_trades should have been passed as None to PublicCollector
        call_kwargs = mock_pub_cls.call_args[1]
        assert call_kwargs["on_trades"] is None

        # Calling _handle_trades should be a no-op
        events = [make_trade(trade_id=f"should_not_persist_{i}") for i in range(3)]
        result = recorder._handle_trades(events)
        assert result is None

        # Verify no trades in DB
        with db.get_session() as session:
            rows = session.query(PublicTrade).all()
            assert len(rows) == 0

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_execution_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="e1",
            order_id="o1",
            side="Buy",
            price=Decimal("50000"),
            qty=Decimal("0.001"),
        )
        fut = recorder._handle_execution(event)
        await await_future(fut)

        stats = recorder._execution_writer.get_stats()
        assert stats["buffer_size"] >= 1 or stats["total_written"] >= 1

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_order_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="o1",
            status="New",
            side="Buy",
            price=Decimal("50000"),
            qty=Decimal("0.001"),
        )
        fut = recorder._handle_order(recorder._account_id, event)
        await await_future(fut)

        stats = recorder._order_writer.get_stats()
        assert stats["buffer_size"] >= 1 or stats["total_written"] >= 1

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_position_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        fut = recorder._handle_position(recorder._account_id, {
            "data": [{
                "symbol": "BTCUSDT",
                "side": "Buy",
                "size": "0.1",
                "entryPrice": "50000.0",
                "liqPrice": "45000.0",
                "unrealisedPnl": "100.0",
                "updatedTime": "1704067200000",
            }],
        })
        await await_future(fut)

        stats = recorder._position_writer.get_stats()
        assert stats["buffer_size"] >= 1 or stats["total_written"] >= 1

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_wallet_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        fut = recorder._handle_wallet(recorder._account_id, {
            "data": [{
                "coin": [{
                    "coin": "USDT",
                    "walletBalance": "10000.0",
                    "availableToWithdraw": "9500.0",
                }],
                "updateTime": "1704067200000",
            }],
        })
        await await_future(fut)

        stats = recorder._wallet_writer.get_stats()
        assert stats["buffer_size"] >= 1 or stats["total_written"] >= 1

        await recorder.stop()

    @patch("recorder.recorder.GapReconciler")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_public_gap_triggers_reconciler(
        self, mock_rest_cls, mock_pub_cls, mock_reconciler_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_reconciler = MagicMock()
        mock_reconciler.reconcile_public_trades = AsyncMock(return_value=5)
        mock_reconciler.get_stats.return_value = {}
        mock_reconciler_cls.return_value = mock_reconciler

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        gap_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        gap_end = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
        fut = recorder._handle_public_gap("BTCUSDT", gap_start, gap_end)

        assert recorder._gap_count == 1

        await await_future(fut)

        mock_reconciler.reconcile_public_trades.assert_awaited_once_with(
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
        )

        await recorder.stop()

    @patch("recorder.recorder.GapReconciler")
    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_private_gap_triggers_reconciler(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        mock_reconciler_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        mock_reconciler = MagicMock()
        mock_reconciler.reconcile_executions = AsyncMock(
            return_value=ExecutionRecoveryResult(
                status=RecoveryStatus.RECOVERED, inserted=2, duplicates=0
            )
        )
        mock_reconciler.get_stats.return_value = {}
        mock_reconciler_cls.return_value = mock_reconciler

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        gap_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        gap_end = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
        futs = recorder._handle_private_gap(gap_start, gap_end)

        assert recorder._gap_count == 1

        await await_future(futs)

        mock_reconciler.reconcile_executions.assert_awaited_once_with(
            user_id=recorder._user_id,
            account_id=recorder._account_id,
            run_id=recorder._run_id,
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
        )

        await recorder.stop()

    @patch("recorder.recorder.PrivateStreamGapRepository")
    @patch("recorder.recorder.GapReconciler")
    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_gap_row_write_failure_still_runs_recovery(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, mock_reconciler_cls,
        mock_gap_repo_cls, config_with_account, db, db_with_gridbot_seed,
        caplog,
    ):
        """A failed gap-row INSERT is logged; the REST backfill still runs."""
        _mock_collectors(mock_pub_cls, mock_priv_cls)
        mock_reconciler = MagicMock()
        mock_reconciler.reconcile_executions = AsyncMock(
            return_value=ExecutionRecoveryResult(RecoveryStatus.RECOVERED)
        )
        mock_reconciler.get_stats.return_value = {}
        mock_reconciler_cls.return_value = mock_reconciler
        mock_gap_repo_cls.return_value.add_gap.side_effect = RuntimeError("locked")

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()
        try:
            with caplog.at_level("ERROR", logger="recorder.recorder"):
                futs = recorder._handle_private_gap(
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC),
                )
                await await_future(futs)
            mock_reconciler.reconcile_executions.assert_awaited_once()
            mock_gap_repo_cls.return_value.set_outcome.assert_not_called()
            assert "Failed to record private stream gap" in caplog.text
        finally:
            await recorder.stop()

    @patch("recorder.recorder.PrivateStreamGapRepository")
    async def test_cancelled_recovery_is_persisted_failed_after_log_callback(
        self, mock_gap_repo_cls, config_with_account, db
    ):
        """Cancel runs both callbacks in order; the gap is stored FAILED."""
        recorder = Recorder(config=config_with_account, db=db)
        fut = Future()
        fut.add_done_callback(recorder._log_future_error("private reconciliation"))
        fut.add_done_callback(recorder._persist_gap_outcome(7, "BTCUSDT"))
        fut.cancel()
        mock_gap_repo_cls.return_value.set_outcome.assert_called_once_with(
            7,
            run_id=str(recorder._run_id),
            status=RecoveryStatus.FAILED,
            inserted=0,
            duplicates=0,
            reason="recovery cancelled",
        )

    @patch("recorder.recorder.PrivateStreamGapRepository")
    async def test_gap_outcome_uses_run_id_of_the_gap_row(
        self, mock_gap_repo_cls, config_with_account, db
    ):
        """The outcome is scoped to the run the gap row was written under,
        captured when the callback is built, not when it fires."""
        recorder = Recorder(config=config_with_account, db=db)
        recorder._run_id = uuid4()
        row_run_id = str(recorder._run_id)
        cb = recorder._persist_gap_outcome(7, "BTCUSDT")
        recorder._run_id = "another-run"
        fut = Future()
        fut.set_result(ExecutionRecoveryResult(RecoveryStatus.RECOVERED))
        cb(fut)
        call = mock_gap_repo_cls.return_value.set_outcome.call_args
        assert call.kwargs["run_id"] == row_run_id

    async def test_gap_outcome_write_failure_is_logged_not_raised(
        self, config_with_account, db, caplog
    ):
        """A failed outcome UPDATE (unknown gap id) is logged, never raised."""
        recorder = Recorder(config=config_with_account, db=db)
        fut = Future()
        fut.set_result(ExecutionRecoveryResult(RecoveryStatus.RECOVERED))
        with caplog.at_level("ERROR", logger="recorder.recorder"):
            recorder._persist_gap_outcome(999999, "BTCUSDT")(fut)
        assert "Failed to persist gap outcome" in caplog.text

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_private_gap_no_reconcile_without_account(
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        gap_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        gap_end = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
        recorder._handle_private_gap(gap_start, gap_end)

        # Still increments count even without reconciliation
        assert recorder._gap_count == 1

        await recorder.stop()


class TestRecorderStats:
    """Tests for get_stats."""

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stats_include_uptime(
        self, mock_rest_cls, mock_pub_cls, config_with_trades_enabled, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config_with_trades_enabled, db=db)
        await recorder.start()

        # Genuine timing need: uptime must be non-zero for meaningful stats
        await asyncio.sleep(0.1)
        stats = recorder.get_stats()

        assert "uptime_seconds" in stats
        assert stats["uptime_seconds"] >= 0
        assert "gaps_detected" in stats
        assert "trades" in stats
        assert "tickers" in stats
        assert "reconciler" in stats

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stats_include_private_writers_when_account(
        self, mock_rest_cls, mock_pub_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        with patch("recorder.recorder.PrivateCollector") as mock_priv_cls:
            mock_priv = MagicMock()
            mock_priv.start = AsyncMock()
            mock_priv.stop = AsyncMock()
            mock_priv_cls.return_value = mock_priv

            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()

            stats = recorder.get_stats()
            assert "executions" in stats
            assert "orders" in stats
            assert "positions" in stats
            assert "wallets" in stats

            await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stats_include_message_rates(
        self, mock_rest_cls, mock_pub_cls, config_with_trades_enabled, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=config_with_trades_enabled, db=db)
        await recorder.start()

        # Genuine timing need: uptime must be non-zero for msgs_per_sec
        await asyncio.sleep(0.1)
        stats = recorder.get_stats()

        # After start with non-zero uptime, writer stats should include rate
        assert "msgs_per_sec" in stats["trades"]
        assert "msgs_per_sec" in stats["tickers"]

        await recorder.stop()

    def test_stats_before_start(self, basic_config, db):
        recorder = Recorder(config=basic_config, db=db)
        stats = recorder.get_stats()
        assert stats["uptime_seconds"] == 0.0
        assert stats["gaps_detected"] == 0


class TestRecorderRunPersistence:
    """Tests for P1 fix: synthetic Run for private stream persistence."""

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_seed_creates_run_with_valid_id(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        # run_id should be set
        assert recorder._run_id is not None

        # PrivateCollector should have been created with the run_id
        call_kwargs = mock_priv_cls.call_args
        context = call_kwargs.kwargs.get("context") or call_kwargs[1].get("context")
        assert context.run_id == recorder._run_id

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_run_record_exists_in_db(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        mock_priv = MagicMock()
        mock_priv.start = AsyncMock()
        mock_priv.stop = AsyncMock()
        mock_priv_cls.return_value = mock_priv

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        # Verify Run row exists in DB
        from grid_db import Run
        with db.get_session() as session:
            run = session.get(Run, str(recorder._run_id))
            assert run is not None
            assert run.status == "running"
            assert run.run_type == "recording"

        await recorder.stop()

        # After stop, run should be marked completed
        with db.get_session() as session:
            run = session.get(Run, str(recorder._run_id))
            assert run.status == "completed"
            assert run.end_ts is not None

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_run_created_without_account(
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        # Run is always created (replay engine needs it for time range)
        assert recorder._run_id is not None

        await recorder.stop()

        # Verify run is marked completed
        with db.get_session() as session:
            run = session.get(Run, str(recorder._run_id))
            assert run is not None
            assert run.run_type == "recording"
            assert run.status == "completed"


def _mock_collector(start=None):
    collector = MagicMock()
    collector.start = start or AsyncMock()
    collector.stop = AsyncMock()
    collector.get_connection_state.return_value = None
    return collector


class TestStartupOrder:
    """Feature 0110 B1c-2: the run starts on a confirmed private session."""

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_rest_snapshot_runs_after_confirmed_private_session(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed,
    ):
        """The t=0 REST snapshot is taken after the private collector has
        started and the session row exists, so it lies inside the session."""
        order = []
        mock_pub_cls.return_value = _mock_collector(
            AsyncMock(side_effect=lambda: order.append("public"))
        )
        mock_priv_cls.return_value = _mock_collector(
            AsyncMock(side_effect=lambda: order.append("private"))
        )
        recorder = Recorder(config=config_with_account, db=db)

        async def _snapshot():
            order.append(("snapshot", recorder._private_session_id is not None))

        with patch.object(recorder, "_write_initial_rest_snapshot", _snapshot):
            await recorder.start()
        try:
            assert order == ["public", "private", ("snapshot", True)]
        finally:
            await recorder.stop()

    @pytest.mark.parametrize("failing", ["public", "private"])
    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_collector_start_failure_emits_incomplete_and_raises(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog, failing,
    ):
        """A collector that does not come up ends the start: the launcher
        sentinel is emitted, no session row and no REST snapshot are written,
        and the error propagates."""
        boom = AsyncMock(side_effect=CollectorStartError("socket not ready"))
        mock_pub = _mock_collector(boom if failing == "public" else None)
        mock_pub_cls.return_value = mock_pub
        mock_priv_cls.return_value = _mock_collector(
            boom if failing == "private" else None
        )
        recorder = Recorder(config=config_with_account, db=db)
        snapshot = AsyncMock()
        with patch.object(recorder, "_write_initial_rest_snapshot", snapshot):
            with caplog.at_level("WARNING", logger="recorder.recorder"):
                with pytest.raises(CollectorStartError):
                    await recorder.start()
        try:
            sentinels = [
                r for r in caplog.records
                if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"
            ]
            assert len(sentinels) == 1
            # the launcher's classifier greps this line for the cause
            assert "Recorder start aborted: socket not ready" in caplog.text
            snapshot.assert_not_awaited()
            assert recorder._private_session_id is None
        finally:
            await recorder.stop(error=True)
        mock_pub.stop.assert_awaited_once()  # the started collector is stopped

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_any_collector_init_error_emits_the_sentinel(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog,
    ):
        """Every exit path of the start emits one launcher sentinel, also an
        unexpected error (not only CollectorStartError)."""
        mock_pub_cls.side_effect = RuntimeError("constructor blew up")
        recorder = Recorder(config=config_with_account, db=db)
        with caplog.at_level("WARNING", logger="recorder.recorder"):
            with pytest.raises(RuntimeError):
                await recorder.start()
        try:
            sentinels = [
                r for r in caplog.records
                if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"
            ]
            assert len(sentinels) == 1
        finally:
            await recorder.stop(error=True)

    @pytest.mark.parametrize("stage", ["collectors", "snapshot"])
    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_cancelled_start_emits_the_sentinel(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog, stage,
    ):
        """A start cancelled by a shutdown signal still emits exactly one
        launcher sentinel, whether it was in the collectors or in the REST
        snapshot."""
        never = asyncio.Event()

        async def _parked():
            await never.wait()

        mock_pub_cls.return_value = _mock_collector(
            AsyncMock(side_effect=_parked) if stage == "collectors" else None
        )
        mock_priv_cls.return_value = _mock_collector()
        recorder = Recorder(config=config_with_account, db=db)
        with patch.object(recorder, "_write_initial_rest_snapshot", _parked):
            with caplog.at_level("WARNING", logger="recorder.recorder"):
                task = asyncio.create_task(recorder.start())
                await asyncio.sleep(0.1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        try:
            sentinels = [
                r for r in caplog.records
                if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"
            ]
            assert len(sentinels) == 1
            if stage == "collectors":  # the cause is named, not blank
                assert "Recorder start aborted: CancelledError" in caplog.text
        finally:
            await recorder.stop(error=True)

    async def test_writer_init_failure_emits_the_sentinel(
        self, config_with_account, db, db_with_gridbot_seed, caplog
    ):
        """A failure before the collectors (writers / run seeding) also emits
        the launcher sentinel, so the launcher does not wait out its bound."""
        recorder = Recorder(config=config_with_account, db=db)
        with patch.object(
            recorder, "_init_writers", AsyncMock(side_effect=RuntimeError("db"))
        ):
            with caplog.at_level("WARNING", logger="recorder.recorder"):
                with pytest.raises(RuntimeError):
                    await recorder.start()
        try:
            sentinels = [
                r for r in caplog.records
                if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"
            ]
            assert len(sentinels) == 1
        finally:
            await recorder.stop(error=True)

    async def test_request_shutdown_ends_run_until_shutdown(
        self, basic_config, db
    ):
        """request_shutdown() is what a signal handler calls: it makes
        run_until_shutdown() return and stop the recorder."""
        recorder = Recorder(config=basic_config, db=db)
        recorder.request_shutdown()
        with patch.object(recorder, "stop", AsyncMock()) as stop:
            await asyncio.wait_for(recorder.run_until_shutdown(), timeout=2)
        stop.assert_awaited_once()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_snapshot_error_emits_the_sentinel(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog,
    ):
        """An exception escaping the REST snapshot also emits the launcher
        sentinel, so the launcher does not wait out its bound on a recorder
        that has already exited."""
        mock_pub_cls.return_value = _mock_collector()
        mock_priv_cls.return_value = _mock_collector()
        recorder = Recorder(config=config_with_account, db=db)
        with patch.object(
            recorder,
            "_write_initial_rest_snapshot",
            AsyncMock(side_effect=RuntimeError("unexpected")),
        ):
            with caplog.at_level("WARNING", logger="recorder.recorder"):
                with pytest.raises(RuntimeError):
                    await recorder.start()
        try:
            sentinels = [
                r for r in caplog.records
                if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"
            ]
            assert len(sentinels) == 1
        finally:
            await recorder.stop(error=True)
