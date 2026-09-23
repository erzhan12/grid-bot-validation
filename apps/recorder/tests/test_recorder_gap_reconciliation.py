"""Recorder-level disconnect → reconnect → REST reconciliation tests.

Hermetic: real Recorder + collectors + GapReconciler + in-memory SQLite.
Only the WS clients and BybitRestClient are faked (no real Bybit).
See docs/features/0107_PLAN.md and issue #211.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from bybit_adapter.ws_client import ConnectionState
from grid_db import PrivateExecution, PublicTrade
from recorder.recorder import Recorder


_POLL_INTERVAL = 0.01
_WAIT_TIMEOUT = 2.0
_NEGATIVE_DRAIN = 0.2


class FakePublicWS:
    """Stand-in for PublicWebSocketClient. Captures reconnect callback."""

    def __init__(self, **kwargs):
        self.symbols = kwargs.get("symbols")
        self.on_disconnect = kwargs.get("on_disconnect")
        self.on_reconnect = kwargs.get("on_reconnect")

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def get_connection_state(self) -> None:
        return None

    def fire_reconnect(self, disconnected_at: datetime, reconnected_at: datetime) -> None:
        if self.on_reconnect:
            self.on_reconnect(disconnected_at, reconnected_at)


class FakePrivateWS:
    """Stand-in for PrivateWebSocketClient. TCP probe is a mutable flag."""

    def __init__(self, **kwargs):
        self.on_disconnect = kwargs.get("on_disconnect")
        self.on_reconnect = kwargs.get("on_reconnect")
        self.alive = True
        self.last_message_ts = datetime.now(UTC)

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def is_socket_alive(self) -> bool:
        return self.alive

    def reset(self) -> None:
        self.alive = True

    def get_connection_state(self) -> ConnectionState:
        return ConnectionState(
            last_message_ts=self.last_message_ts,
            is_connected=True,
        )


def _make_fake_rest(*, trades=None, executions=None):
    """Build a BybitRestClient stand-in whose instances share canned data.

    Recorder.start() and GapReconciler.reconcile_executions each construct
    their own client. Shared lists/counters keep every instance consistent.
    """
    trades = list(trades or [])
    executions = list(executions or [])
    calls = {"recent_trades": 0, "executions": 0}

    class FakeRestClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_recent_trades(self, symbol, limit=1000):
            calls["recent_trades"] += 1
            return list(trades)

        def get_executions_all(
            self,
            symbol,
            start_time,
            end_time,
            max_pages,
            return_truncated=True,
        ):
            calls["executions"] += 1
            return (list(executions), False)

        def get_wallet_balance(self, account_type="UNIFIED"):
            return {"list": []}

        def get_positions(self, symbol):
            return []

        def get_open_orders(self, symbol, order_type="Limit"):
            return []

    FakeRestClient.calls = calls
    return FakeRestClient


@contextmanager
def _patched_network(fake_rest_cls):
    """Patch both REST import sites and both collector WS clients."""
    with (
        patch(
            "event_saver.collectors.public_collector.PublicWebSocketClient",
            FakePublicWS,
        ),
        patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient",
            FakePrivateWS,
        ),
        patch("recorder.recorder.BybitRestClient", fake_rest_cls),
        patch("event_saver.reconciler.BybitRestClient", fake_rest_cls),
    ):
        yield


def _count(db, model) -> int:
    with db.get_session() as session:
        return session.query(model).count()


async def _wait_for_count(db, model, expected: int, *, timeout: float = _WAIT_TIMEOUT) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        last = _count(db, model)
        if last == expected:
            return
        await asyncio.sleep(_POLL_INTERVAL)
    pytest.fail(
        f"timed out waiting for {expected} {model.__name__} rows; last={last}"
    )


async def _wait_until(pred, *, timeout: float = _WAIT_TIMEOUT, desc: str = "condition") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(_POLL_INTERVAL)
    pytest.fail(f"timed out waiting for {desc}")


def _public_trade_payload(gap_start: datetime, gap_end: datetime) -> dict:
    mid = gap_start + (gap_end - gap_start) / 2
    return {
        "execId": "gap-trade-1",
        "time": int(mid.timestamp() * 1000),
        "side": "Buy",
        "price": "50000.00",
        "size": "0.001",
    }


def _private_exec_payload(gap_start: datetime, gap_end: datetime) -> dict:
    mid = gap_start + (gap_end - gap_start) / 2
    return {
        "category": "linear",
        "execType": "Trade",
        "execId": "gap-exec-1",
        "orderId": "ord-1",
        "orderLinkId": "link-1",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "execPrice": "50000.00",
        "execQty": "0.001",
        "execFee": "0.01",
        "closedPnl": "0",
        "execTime": int(mid.timestamp() * 1000),
    }


class TestRecorderDisconnectReconciliation:
    """Vertical recorder path: collector gap → GapReconciler → persisted rows."""

    async def test_public_ws_disconnect_triggers_gap_reconciliation(
        self, config_with_trades_enabled, db
    ):
        gap_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        gap_end = gap_start + timedelta(seconds=30)
        fake_rest = _make_fake_rest(trades=[_public_trade_payload(gap_start, gap_end)])

        with _patched_network(fake_rest):
            recorder = Recorder(config=config_with_trades_enabled, db=db)
            await recorder.start()
            try:
                recorder._public_collector._ws_client.fire_reconnect(gap_start, gap_end)
                assert recorder._gap_count == 1
                await _wait_for_count(db, PublicTrade, 1)

                with db.get_session() as session:
                    row = session.query(PublicTrade).one()
                    assert row.trade_id == "gap-trade-1"
                    assert row.symbol == "BTCUSDT"
                    assert row.price == Decimal("50000.00")
            finally:
                await recorder.stop()

    async def test_private_ws_disconnect_triggers_gap_reconciliation(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        gap_end = datetime.now(UTC)
        fake_rest = _make_fake_rest(
            executions=[_private_exec_payload(gap_start, gap_end)]
        )

        with _patched_network(fake_rest):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                private_ws = recorder._private_collector._ws_client
                private_ws.last_message_ts = gap_start
                private_ws.alive = False
                await recorder._private_collector._ws_health_check_once()
                assert recorder._gap_count == 1
                await _wait_for_count(db, PrivateExecution, 1)

                with db.get_session() as session:
                    row = session.query(PrivateExecution).one()
                    assert row.exec_id == "gap-exec-1"
                    assert row.symbol == "BTCUSDT"
                    assert row.run_id == str(recorder._run_id)
            finally:
                await recorder.stop()

    async def test_reconnect_below_threshold_does_not_write(
        self, config_with_trades_enabled, db
    ):
        gap_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        gap_end = gap_start + timedelta(seconds=1)
        fake_rest = _make_fake_rest(trades=[_public_trade_payload(gap_start, gap_end)])

        with _patched_network(fake_rest):
            recorder = Recorder(config=config_with_trades_enabled, db=db)
            await recorder.start()
            try:
                recorder._public_collector._ws_client.fire_reconnect(gap_start, gap_end)
                assert recorder._gap_count == 1
                # Drain long enough that a mistakenly scheduled reconcile
                # (run_coroutine_threadsafe + to_thread) would have run.
                deadline = asyncio.get_event_loop().time() + _NEGATIVE_DRAIN
                while asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(_POLL_INTERVAL)
                assert fake_rest.calls["recent_trades"] == 0
                assert _count(db, PublicTrade) == 0
            finally:
                await recorder.stop()

    async def test_reconnect_does_not_duplicate_already_persisted_events(
        self, config_with_trades_enabled, db
    ):
        gap_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        gap_end = gap_start + timedelta(seconds=30)
        payload = _public_trade_payload(gap_start, gap_end)
        fake_rest = _make_fake_rest(trades=[payload])

        with db.get_session() as session:
            session.add(
                PublicTrade(
                    symbol="BTCUSDT",
                    trade_id="gap-trade-1",
                    exchange_ts=datetime.fromtimestamp(
                        payload["time"] / 1000, tz=UTC
                    ),
                    local_ts=datetime.now(UTC),
                    side="Buy",
                    price=Decimal("50000.00"),
                    size=Decimal("0.001"),
                )
            )

        with _patched_network(fake_rest):
            recorder = Recorder(config=config_with_trades_enabled, db=db)
            await recorder.start()
            try:
                recorder._public_collector._ws_client.fire_reconnect(gap_start, gap_end)
                assert recorder._gap_count == 1
                # Count is already 1; wait for REST (proves reconcile ran)
                # then drain the ON CONFLICT insert before asserting.
                await _wait_until(
                    lambda: fake_rest.calls["recent_trades"] >= 1,
                    desc="get_recent_trades call",
                )
                deadline = asyncio.get_event_loop().time() + _NEGATIVE_DRAIN
                while asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(_POLL_INTERVAL)
                assert _count(db, PublicTrade) == 1
            finally:
                await recorder.stop()
