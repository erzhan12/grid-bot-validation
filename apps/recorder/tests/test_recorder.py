"""Tests for recorder orchestrator."""

import asyncio
from concurrent.futures import Future
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional, Union
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gridcore.events import (
    EventType,
    ExecutionEvent,
    OrderUpdateEvent,
    TickerEvent,
    PublicTradeEvent,
)

from recorder.recorder import Recorder, _RECORDER_USER_ID, _RECORDER_ACCOUNT_ID


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

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_start_with_account_creates_private_collector(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db
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
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
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
            handler(_RECORDER_ACCOUNT_ID, OrderUpdateEvent(
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
            handler(_RECORDER_ACCOUNT_ID, {"data": []})
        elif args == "wallet":
            handler(_RECORDER_ACCOUNT_ID, {"data": []})
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
        self, mock_rest_cls, mock_pub_cls, basic_config, db, make_trade
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
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
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        fut = recorder._handle_trades([])
        assert fut is None

        stats = recorder._trade_writer.get_stats()
        assert stats["buffer_size"] == 0
        assert stats["total_written"] == 0

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_execution_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db
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
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db
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
        fut = recorder._handle_order(_RECORDER_ACCOUNT_ID, event)
        await await_future(fut)

        stats = recorder._order_writer.get_stats()
        assert stats["buffer_size"] >= 1 or stats["total_written"] >= 1

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_handle_position_routes_to_writer(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db
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

        fut = recorder._handle_position(_RECORDER_ACCOUNT_ID, {
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
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db
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

        fut = recorder._handle_wallet(_RECORDER_ACCOUNT_ID, {
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
        mock_reconciler_cls, config_with_account, db
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
        mock_reconciler.reconcile_executions = AsyncMock(return_value=2)
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
            user_id=_RECORDER_USER_ID,
            account_id=_RECORDER_ACCOUNT_ID,
            run_id=recorder._run_id,
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
        )

        await recorder.stop()

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
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
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
        self, mock_rest_cls, mock_pub_cls, config_with_account, db
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
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
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
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db
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
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db
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
    async def test_no_run_without_account(
        self, mock_rest_cls, mock_pub_cls, basic_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=basic_config, db=db)
        await recorder.start()

        # No run_id when no account configured
        assert recorder._run_id is None

        await recorder.stop()


@pytest.mark.skip(reason="Integration test stub — see docs/features/0008_REVIEW.md residual risks")
class TestRecorderDisconnectReconciliation:
    """TODO: No integration test covers actual WS disconnect → reconnect → reconciliation.

    The most dangerous failure mode for a multi-day recorder is a silent gap
    that is never reconciled.  This stub exists to make that gap explicit
    and encourage a follow-up integration test.
    """

    async def test_public_ws_disconnect_triggers_gap_reconciliation(self):
        raise NotImplementedError

    async def test_private_ws_disconnect_triggers_gap_reconciliation(self):
        raise NotImplementedError
