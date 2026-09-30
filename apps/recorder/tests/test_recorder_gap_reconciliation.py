"""Recorder-level disconnect → reconnect → REST reconciliation tests.

Hermetic: real Recorder + collectors + GapReconciler + in-memory SQLite.
Only the WS clients and BybitRestClient are faked (no real Bybit).
See docs/features/0107_PLAN.md and issue #211.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from bybit_adapter.ws_client import ConnectionState
from gridcore.events import EventType, ExecutionEvent
from grid_db import (
    PrivateExecution,
    PrivateStreamGap,
    PrivateStreamGapRepository,
    PrivateStreamSession,
    PrivateStreamSessionRepository,
    PublicTrade,
    RecoveryStatus,
)
from event_saver.collectors.private_collector import _LIVENESS_MARGIN
from recorder.recorder import Recorder


_POLL_INTERVAL = 0.01
_WAIT_TIMEOUT = 2.0
_NEGATIVE_DRAIN = 0.2
_TEST_SYMBOL = "BTCUSDT"
_TEST_PRICE = "50000.00"
_TEST_TRADE_ID = "gap-trade-1"
_TEST_EXEC_ID = "gap-exec-1"
_FIXED_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class FakePublicWS:
    """Stand-in for PublicWebSocketClient. Captures reconnect callback."""

    def __init__(self, **kwargs):
        self.symbols = kwargs.get("symbols")
        self.on_disconnect = kwargs.get("on_disconnect")
        self.on_reconnect = kwargs.get("on_reconnect")

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def get_connection_state(self) -> None:
        # PublicCollector.start() never reads this in the gap path;
        # PublicCollector.get_connection_state() just forwards it.
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
        self.last_message_ts = _FIXED_TS
        self._identity = object()
        self.on_reset = None  # test hook: runs inside reset()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def is_socket_alive(self) -> bool:
        return self.alive

    def reset(self) -> None:
        if self.on_reset is not None:
            self.on_reset()
        self.alive = True
        self._identity = object()  # a real reset builds a new pybit socket

    # Feature 0110 B1b readiness surface: always ready, stable identity.
    def wait_ready(self, timeout: float) -> bool:
        return True

    def socket_identity(self) -> object:
        return self._identity

    def is_authenticated(self) -> bool:
        return True

    def get_connection_state(self) -> ConnectionState:
        return ConnectionState(
            last_message_ts=self.last_message_ts,
            is_connected=True,
        )


def _make_fake_rest(*, trades=None, executions=None, executions_exc=None):
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
            if symbol != _TEST_SYMBOL:
                raise ValueError(f"unexpected symbol {symbol!r}")
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
            if symbol != _TEST_SYMBOL:
                raise ValueError(f"unexpected symbol {symbol!r}")
            calls["executions"] += 1
            if executions_exc is not None:
                raise executions_exc
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
def _patched_network(fake_rest_cls) -> Generator[None, None, None]:
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


def _gap_rows(db) -> list:
    with db.get_session() as session:
        rows = session.query(PrivateStreamGap).all()
        session.expunge_all()
        return rows


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


async def _drain_event_loop(seconds: float = _NEGATIVE_DRAIN) -> None:
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL)


async def _wait_until(pred, *, timeout: float = _WAIT_TIMEOUT, desc: str = "condition") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(_POLL_INTERVAL)
    pytest.fail(f"timed out waiting for {desc}")


def _public_trade_payload(gap_start: datetime, gap_end: datetime) -> dict:
    """Bybit public-trade REST payload with ``time`` at the gap midpoint."""

    mid = gap_start + (gap_end - gap_start) / 2
    return {
        "execId": _TEST_TRADE_ID,
        "time": int(mid.timestamp() * 1000),
        "side": "Buy",
        "price": _TEST_PRICE,
        "size": "0.001",
    }


def _private_exec_payload(
    gap_start: datetime, gap_end: datetime, *, closed_size: str = "0"
) -> dict:
    """Documented ``/v5/execution/list`` row with ``execTime`` at the gap midpoint.

    Matches Bybit's REST shape (feature 0110): ``category`` lives on the
    result envelope, so rows carry none, and rows carry no PnL field.
    """

    mid = gap_start + (gap_end - gap_start) / 2
    return {
        "execType": "Trade",
        "execId": _TEST_EXEC_ID,
        "orderId": "ord-1",
        "orderLinkId": "link-1",
        "symbol": _TEST_SYMBOL,
        "side": "Buy",
        "execPrice": _TEST_PRICE,
        "execQty": "0.001",
        "execFee": "0.01",
        "closedSize": closed_size,
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
                    assert row.trade_id == _TEST_TRADE_ID
                    assert row.symbol == _TEST_SYMBOL
                    assert row.price == Decimal(_TEST_PRICE)
            finally:
                await recorder.stop()

    @pytest.mark.parametrize(
        "closed_size, expected_pnl",
        [("0", Decimal("0")), ("0.001", None)],
        ids=["opening-fill-known-zero", "closing-fill-unknown"],
    )
    async def test_private_ws_disconnect_triggers_gap_reconciliation(
        self, config_with_account, db, db_with_gridbot_seed,
        closed_size, expected_pnl,
    ):
        # Recent: the collector's gap end is the real reconnect time, and
        # Bybit caps the recovery window at 7 days.
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        gap_end = gap_start + timedelta(seconds=30)
        fake_rest = _make_fake_rest(
            executions=[
                _private_exec_payload(gap_start, gap_end, closed_size=closed_size)
            ]
        )

        with _patched_network(fake_rest):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                private_ws = recorder._private_collector._ws_client
                # 0110 B1b: gap start = last healthy probe − liveness margin,
                # never before the collector connected.
                recorder._private_collector._connected_at = gap_start
                recorder._private_collector._last_healthy_ts = (
                    gap_start + _LIVENESS_MARGIN
                )
                private_ws.alive = False
                await recorder._private_collector._ws_health_check_once()
                assert recorder._gap_count == 1
                await _wait_for_count(db, PrivateExecution, 1)

                with db.get_session() as session:
                    row = session.query(PrivateExecution).one()
                    assert row.exec_id == _TEST_EXEC_ID
                    assert row.symbol == _TEST_SYMBOL
                    assert row.run_id == str(recorder._run_id)
                    assert row.closed_pnl == expected_pnl

                await _wait_until(
                    lambda: _gap_rows(db)
                    and _gap_rows(db)[0].recovery_status != RecoveryStatus.PENDING,
                    desc="gap outcome persisted",
                )
                (gap,) = _gap_rows(db)
                assert gap.run_id == str(recorder._run_id)
                assert gap.symbol == _TEST_SYMBOL
                assert gap.gap_start.replace(tzinfo=UTC) == gap_start
                assert gap.recovery_status == RecoveryStatus.RECOVERED
                assert (gap.inserted, gap.duplicates) == (1, 0)
            finally:
                await recorder.stop()

    async def test_failed_private_recovery_is_persisted_as_failed(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A REST failure leaves a FAILED gap row with a reason, not silence."""
        # Recent: the collector's gap end is the real reconnect time, and
        # Bybit caps the recovery window at 7 days.
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        fake_rest = _make_fake_rest(
            executions_exc=ValueError("expected result.category='linear'")
        )

        with _patched_network(fake_rest):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                private_ws = recorder._private_collector._ws_client
                # 0110 B1b: gap start = last healthy probe − liveness margin,
                # never before the collector connected.
                recorder._private_collector._connected_at = gap_start
                recorder._private_collector._last_healthy_ts = (
                    gap_start + _LIVENESS_MARGIN
                )
                private_ws.alive = False
                await recorder._private_collector._ws_health_check_once()
                await _wait_until(
                    lambda: _gap_rows(db)
                    and _gap_rows(db)[0].recovery_status != RecoveryStatus.PENDING,
                    desc="gap outcome persisted",
                )
                (gap,) = _gap_rows(db)
                assert gap.recovery_status == RecoveryStatus.FAILED
                assert "result.category" in gap.reason
                assert _count(db, PrivateExecution) == 0
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
                await _drain_event_loop()
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
                    symbol=_TEST_SYMBOL,
                    trade_id=_TEST_TRADE_ID,
                    exchange_ts=datetime.fromtimestamp(
                        payload["time"] / 1000, tz=UTC
                    ),
                    local_ts=_FIXED_TS,
                    side="Buy",
                    price=Decimal(_TEST_PRICE),
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
                await _drain_event_loop()
                assert _count(db, PublicTrade) == 1
            finally:
                await recorder.stop()


# ---------------------------------------------------------------------------
# Private-stream coverage: sessions, open gaps, checkpoint (feature 0110 B1c-1)
# ---------------------------------------------------------------------------


async def _dead_socket_probe(recorder, gap_start: datetime) -> None:
    """Run one probe on a dead socket whose last healthy time dates the gap."""
    collector = recorder._private_collector
    collector._connected_at = gap_start
    collector._last_healthy_ts = gap_start + _LIVENESS_MARGIN
    collector._ws_client.alive = False
    await collector._ws_health_check_once()


def _session_row(db) -> PrivateStreamSession:
    with db.get_session() as session:
        row = session.query(PrivateStreamSession).one()
        session.expunge_all()
        return row


def _backdate_session(db, minutes: int = 10) -> datetime:
    """Move the session start back so a fresh checkpoint can advance it."""
    ts = datetime.now(UTC) - timedelta(minutes=minutes)
    with db.get_session() as session:
        row = session.query(PrivateStreamSession).one()
        row.connected_at = ts
        row.last_checkpoint_ts = ts
    return _session_row(db).last_checkpoint_ts


def _execution_event(recorder, exec_id: str) -> ExecutionEvent:
    return ExecutionEvent(
        event_type=EventType.EXECUTION,
        symbol=_TEST_SYMBOL,
        exchange_ts=datetime.now(UTC),
        local_ts=datetime.now(UTC),
        exec_id=exec_id,
        order_id="o1",
        side="Buy",
        price=Decimal(_TEST_PRICE),
        qty=Decimal("0.001"),
        user_id=recorder._user_id,
        account_id=recorder._account_id,
        run_id=recorder._run_id,
    )


class TestPrivateStreamCoverage:
    async def test_session_opened_on_start(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A confirmed private start writes one session row; checkpoint = start."""
        before = datetime.now(UTC)
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                row = _session_row(db)
                assert row.run_id == str(recorder._run_id)
                assert row.account_id == str(recorder._account_id)
                assert row.connected_at.replace(tzinfo=UTC) >= before
                assert row.last_checkpoint_ts == row.connected_at
                assert recorder.get_stats()["private_ws"] == {"degraded": False}
            finally:
                await recorder.stop()

    async def test_gap_row_open_before_reset_then_closed(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """The gap row exists (open) when reset() runs; it is closed after."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                seen_at_reset = []
                recorder._private_collector._ws_client.on_reset = (
                    lambda: seen_at_reset.append(
                        [(g.symbol, g.gap_end) for g in _gap_rows(db)]
                    )
                )
                await _dead_socket_probe(recorder, gap_start)
                assert seen_at_reset == [[(_TEST_SYMBOL, None)]]
                (gap,) = _gap_rows(db)
                assert gap.gap_start.replace(tzinfo=UTC) == gap_start
                assert gap.gap_end is not None
                assert recorder._open_gap_ids == {}
            finally:
                await recorder.stop()

    async def test_reset_timeout_leaves_gap_open(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A reset that never returns leaves the gap row open, no recovery."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        gate = threading.Event()
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                collector = recorder._private_collector
                collector._ws_reset_timeout = 0.05
                collector._ws_client.on_reset = lambda: gate.wait(_WAIT_TIMEOUT)
                await _dead_socket_probe(recorder, gap_start)
                (gap,) = _gap_rows(db)
                assert gap.gap_end is None
                assert gap.recovery_status == RecoveryStatus.PENDING
                assert recorder._gap_count == 0
            finally:
                gate.set()
                await recorder.stop()

    async def test_second_outage_opens_new_gap(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """Disconnect → close committed → a second disconnect opens a new row."""
        first = datetime.now(UTC) - timedelta(seconds=60)
        second = datetime.now(UTC) - timedelta(seconds=20)
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                await _dead_socket_probe(recorder, first)
                await _dead_socket_probe(recorder, second)
                gaps = sorted(_gap_rows(db), key=lambda g: g.gap_start)
                assert [g.gap_start.replace(tzinfo=UTC) for g in gaps] == [
                    first,
                    second,
                ]
                assert all(g.gap_end is not None for g in gaps)
            finally:
                await recorder.stop()

    async def test_checkpoint_commits_pending_writes_then_advances(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """An execution submitted before the barrier is committed, then the
        checkpoint moves to barrier − liveness margin."""
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                _backdate_session(db)
                recorder._handle_execution(_execution_event(recorder, "cp-1"))
                before = datetime.now(UTC)
                await recorder._private_checkpoint()
                after = datetime.now(UTC)

                assert _count(db, PrivateExecution) == 1
                checkpoint = _session_row(db).last_checkpoint_ts.replace(
                    tzinfo=UTC
                )
                assert before - _LIVENESS_MARGIN <= checkpoint
                assert checkpoint <= after - _LIVENESS_MARGIN
            finally:
                await recorder.stop()

    @pytest.mark.parametrize(
        "writer_attr",
        [
            "_execution_writer",
            "_order_writer",
            "_position_writer",
            "_wallet_writer",
        ],
    )
    async def test_flush_error_blocks_checkpoint(
        self, config_with_account, db, db_with_gridbot_seed, writer_attr
    ):
        """A flush DB error in any private writer leaves the checkpoint."""
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                start = _backdate_session(db)
                with patch.object(
                    getattr(recorder, writer_attr),
                    "flush",
                    AsyncMock(return_value=False),
                ):
                    await recorder._private_checkpoint()
                assert _session_row(db).last_checkpoint_ts == start
            finally:
                await recorder.stop()

    async def test_failed_close_is_retried_before_checkpoint(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A failed close-gap write is retried by the next checkpoint, which
        does not advance until the retry succeeds."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        real_close = PrivateStreamGapRepository.close_gap
        failing = {"on": True}

        def _close(self, *args, **kwargs):
            if failing["on"]:
                raise RuntimeError("db down")
            return real_close(self, *args, **kwargs)

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                start = _backdate_session(db)
                with patch.object(PrivateStreamGapRepository, "close_gap", _close):
                    await _dead_socket_probe(recorder, gap_start)
                    assert _gap_rows(db)[0].gap_end is None

                    await recorder._private_checkpoint()  # retry still fails
                    assert _session_row(db).last_checkpoint_ts == start
                    assert _gap_rows(db)[0].gap_end is None

                    failing["on"] = False
                    await recorder._private_checkpoint()
                assert _gap_rows(db)[0].gap_end is not None
                assert recorder._open_gap_ids == {}
                assert _session_row(db).last_checkpoint_ts > start
            finally:
                await recorder.stop()

    async def test_lost_private_write_stops_checkpoint(
        self, config_with_account, db, db_with_gridbot_seed, caplog
    ):
        """A private write that fails never reached a writer and has no gap
        row, so the checkpoint stops advancing for the run."""

        async def _failing_write():
            raise RuntimeError("writer gone")

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                start = _backdate_session(db)
                fut = recorder._submit_private(_failing_write(), "execution write")
                with pytest.raises(RuntimeError):
                    await asyncio.wrap_future(fut)
                await recorder._private_checkpoint()
                assert _session_row(db).last_checkpoint_ts == start
                assert "checkpoint stops advancing" in caplog.text
            finally:
                await recorder.stop()

    async def test_close_of_missing_row_forgets_open_gap(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A close that finds no row is dropped and the open id is cleared, so
        the next outage opens a new row instead of never recording again."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)

        def _missing(self, gap_id, **kwargs):
            raise ValueError(f"private_stream_gaps row {gap_id} not found")

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                with patch.object(PrivateStreamGapRepository, "close_gap", _missing):
                    await _dead_socket_probe(recorder, gap_start)
                assert recorder._open_gap_ids == {}
                assert recorder._pending_gap_writes == []
            finally:
                await recorder.stop()

    async def test_stale_close_retry_does_not_touch_later_outage(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A close that failed and is still queued when a second outage
        arrives must not be replayed onto the second outage: each outage has
        its own row and the first keeps its own (earlier) end."""
        first = datetime.now(UTC) - timedelta(seconds=60)
        second = datetime.now(UTC) - timedelta(seconds=20)
        real_close = PrivateStreamGapRepository.close_gap
        failing = {"on": True}

        def _close(self, *args, **kwargs):
            if failing["on"]:
                raise RuntimeError("db down")
            return real_close(self, *args, **kwargs)

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                _backdate_session(db)
                with patch.object(PrivateStreamGapRepository, "close_gap", _close):
                    await _dead_socket_probe(recorder, first)  # close queued
                    failing["on"] = False
                    await _dead_socket_probe(recorder, second)
                    await recorder._private_checkpoint()  # replays the close
                gaps = sorted(_gap_rows(db), key=lambda g: g.gap_start)
                assert [g.gap_start.replace(tzinfo=UTC) for g in gaps] == [
                    first,
                    second,
                ]
                assert all(g.gap_end is not None for g in gaps)
                assert gaps[0].gap_end <= gaps[1].gap_end
                assert recorder._pending_gap_writes == []
            finally:
                await recorder.stop()

    async def test_healthy_probe_advances_checkpoint(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """The collector's healthy probe is wired to the checkpoint."""
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                start = _backdate_session(db)
                await recorder._private_collector._ws_health_check_once()
                assert _session_row(db).last_checkpoint_ts > start
            finally:
                await recorder.stop()

    async def test_no_checkpoint_while_a_gap_is_open(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """An open gap row (disconnect seen, reconnect not confirmed) blocks
        the checkpoint."""
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                start = _backdate_session(db)
                recorder._handle_private_disconnect(
                    datetime.now(UTC) - timedelta(seconds=30)
                )
                await recorder._private_checkpoint()
                assert _session_row(db).last_checkpoint_ts == start
            finally:
                await recorder.stop()

    async def test_disconnect_hook_is_idempotent(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """Repeated probes of one outage open exactly one row per symbol."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                recorder._handle_private_disconnect(gap_start)
                recorder._handle_private_disconnect(gap_start)
                assert len(_gap_rows(db)) == 1
            finally:
                await recorder.stop()

    async def test_gap_open_is_all_or_nothing_across_symbols(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """One symbol's open-gap insert failing rolls back the others and
        keeps no ids (the collector then skips the reset and retries)."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        real_add = PrivateStreamGapRepository.add_gap

        def _add(self, *args, **kwargs):
            if kwargs["symbol"] == "ETHUSDT":
                raise RuntimeError("db down")
            return real_add(self, *args, **kwargs)

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                recorder._config = recorder._config.model_copy(
                    update={"symbols": [_TEST_SYMBOL, "ETHUSDT"]}
                )
                with patch.object(PrivateStreamGapRepository, "add_gap", _add):
                    with pytest.raises(RuntimeError):
                        recorder._handle_private_disconnect(gap_start)
                assert _gap_rows(db) == []
                assert recorder._open_gap_ids == {}

                recorder._handle_private_disconnect(gap_start)
                assert sorted(g.symbol for g in _gap_rows(db)) == [
                    _TEST_SYMBOL,
                    "ETHUSDT",
                ]
                assert set(recorder._open_gap_ids) == {_TEST_SYMBOL, "ETHUSDT"}
            finally:
                await recorder.stop()

    async def test_failed_outcome_write_is_retried_before_checkpoint(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A failed outcome write is queued, blocks the checkpoint, and is
        stored by the retry."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        real_set = PrivateStreamGapRepository.set_outcome
        failing = {"on": True}

        def _set(self, *args, **kwargs):
            if failing["on"]:
                raise RuntimeError("db down")
            return real_set(self, *args, **kwargs)

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                start = _backdate_session(db)
                with patch.object(PrivateStreamGapRepository, "set_outcome", _set):
                    await _dead_socket_probe(recorder, gap_start)
                    await _wait_until(
                        lambda: recorder._pending_gap_writes,
                        desc="outcome write queued",
                    )
                    await recorder._private_checkpoint()
                    assert _session_row(db).last_checkpoint_ts == start
                    failing["on"] = False
                    await recorder._private_checkpoint()
                (gap,) = _gap_rows(db)
                assert gap.recovery_status == RecoveryStatus.RECOVERED
                assert _session_row(db).last_checkpoint_ts > start
            finally:
                await recorder.stop()

    async def test_fallback_gap_row_failure_is_retried_before_checkpoint(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """Late readiness has no open row; if recording the closed row fails
        it is queued and the checkpoint waits for it."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        real_add = PrivateStreamGapRepository.add_gap
        failing = {"on": True}

        def _add(self, *args, **kwargs):
            if failing["on"]:
                raise RuntimeError("db down")
            return real_add(self, *args, **kwargs)

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                start = _backdate_session(db)
                with patch.object(PrivateStreamGapRepository, "add_gap", _add):
                    recorder._handle_private_gap(gap_start, datetime.now(UTC))
                    await recorder._private_checkpoint()
                    assert _gap_rows(db) == []
                    assert _session_row(db).last_checkpoint_ts == start
                    failing["on"] = False
                    await recorder._private_checkpoint()
                (gap,) = _gap_rows(db)
                assert gap.gap_end is not None
                assert _session_row(db).last_checkpoint_ts > start
            finally:
                await recorder.stop()

    async def test_session_open_failure_disables_checkpoint_only(
        self, config_with_account, db, db_with_gridbot_seed, caplog
    ):
        """No session row: start() still succeeds and the checkpoint is a
        no-op (coverage is simply never certified)."""
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            with patch.object(
                PrivateStreamSessionRepository,
                "open_session",
                side_effect=RuntimeError("db down"),
            ):
                await recorder.start()
            try:
                assert recorder._private_session_id is None
                assert "Failed to open private stream session" in caplog.text
                await recorder._private_checkpoint()
                assert _count(db, PrivateStreamSession) == 0
            finally:
                await recorder.stop()

    async def test_barrier_is_taken_before_waiting_for_writes(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """The checkpoint time is fixed before the pending writes are awaited,
        so a slow write cannot push the certified time past its own start."""
        gate = asyncio.Event()

        async def _slow_write():
            await gate.wait()

        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                _backdate_session(db)
                recorder._submit_private(_slow_write(), "execution write")
                asyncio.get_running_loop().call_later(0.3, gate.set)
                await recorder._private_checkpoint()
                done = datetime.now(UTC)
                checkpoint = _session_row(db).last_checkpoint_ts.replace(
                    tzinfo=UTC
                )
                assert checkpoint + _LIVENESS_MARGIN <= done - timedelta(
                    seconds=0.2
                )
                assert recorder._pending_futures == set()
            finally:
                gate.set()
                await recorder.stop()

    async def test_stats_report_degraded_socket(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """The Health stats carry the collector's liveness-only state."""
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                recorder._private_collector._liveness_only = True
                assert recorder.get_stats()["private_ws"] == {"degraded": True}
            finally:
                await recorder.stop()

    async def test_duplicate_config_symbols_open_one_row(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """A symbol listed twice in the config still gets one gap row, and
        the reconnect closes it without leaving an orphan."""
        gap_start = datetime.now(UTC) - timedelta(seconds=30)
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            try:
                recorder._config = recorder._config.model_copy(
                    update={"symbols": [_TEST_SYMBOL, _TEST_SYMBOL]}
                )
                await _dead_socket_probe(recorder, gap_start)
                (gap,) = _gap_rows(db)
                assert gap.gap_end is not None
                assert recorder._open_gap_ids == {}
            finally:
                await recorder.stop()

    async def test_lost_write_latch_is_set_before_the_future_is_forgotten(
        self, config_with_account, db
    ):
        """The write-lost latch is set under the registry lock, so a
        checkpoint barrier can never see neither the future nor the latch."""
        recorder = Recorder(config=config_with_account, db=db)
        fut: Future = Future()
        fut.set_exception(RuntimeError("writer gone"))
        recorder._pending_futures.add(fut)
        seen = []

        class _Lock:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                seen.append(
                    (fut in recorder._pending_futures, recorder._private_write_lost)
                )

        recorder._pending_lock = _Lock()
        recorder._forget_pending(fut)
        assert seen == [(False, True)]

    async def test_restart_resets_coverage_state(
        self, config_with_account, db, db_with_gridbot_seed
    ):
        """stop() then start() is a new run: a write-lost latch, open gap ids
        and queued gap writes of the old run must not carry over."""
        with _patched_network(_make_fake_rest()):
            recorder = Recorder(config=config_with_account, db=db)
            await recorder.start()
            await recorder.stop()
            recorder._private_write_lost = True
            recorder._open_gap_ids = {_TEST_SYMBOL: 123}
            recorder._pending_gap_writes = [(lambda: None, "stale")]
            await recorder.start()
            try:
                assert recorder._private_write_lost is False
                assert recorder._open_gap_ids == {}
                assert recorder._pending_gap_writes == []
                assert recorder._private_session_id is not None
            finally:
                await recorder.stop()
