"""Post-gap REST snapshot (feature 0110 B3).

After a private-stream gap closes, the recorder re-snapshots positions and
wallet over REST so replay's seed rows — and live-check's coverage interval,
which starts at them — move past the gap.
"""

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from event_saver.reconciler import ExecutionRecoveryResult
from grid_db import PositionSnapshot, RecoveryStatus, WalletSnapshot
from recorder.recorder import Recorder

_WALLET = {
    "list": [{
        "accountType": "UNIFIED",
        "coin": [{"coin": "USDT", "walletBalance": "1000"}],
    }]
}


def _position(side, size="0.5"):
    return {
        "symbol": "BTCUSDT", "side": side, "size": size,
        "entryPrice": "50000", "unrealisedPnl": "1.0",
        "updatedTime": "1700000000000",
    }


def _rest_stub():
    stub = MagicMock()
    stub.get_wallet_balance.return_value = _WALLET
    stub.get_positions.return_value = [_position("Buy"), _position("Sell")]
    stub.get_open_orders.return_value = []
    return stub


def _collectors(mock_pub_cls, mock_priv_cls):
    for cls in (mock_pub_cls, mock_priv_cls):
        mock = MagicMock()
        mock.start = AsyncMock()
        mock.stop = AsyncMock()
        mock.get_connection_state.return_value = None
        cls.return_value = mock


def _reconciler(mock_reconciler_cls):
    reconciler = MagicMock()
    reconciler.reconcile_executions = AsyncMock(
        return_value=ExecutionRecoveryResult(RecoveryStatus.RECOVERED)
    )
    reconciler.get_stats.return_value = {}
    mock_reconciler_cls.return_value = reconciler


def _rows(db, recorder, model):
    with db.get_session() as session:
        return [
            (r.local_ts, getattr(r, "side", None), r.raw_json)
            for r in session.query(model)
            .filter(model.run_id == str(recorder._run_id))
            .order_by(model.id)
            .all()
        ]


async def _close_gap(recorder):
    """Close a 30 s gap ending now; wait for the post-gap snapshot."""
    gap_end = datetime.now(UTC)
    recorder._handle_private_gap(gap_end - timedelta(seconds=30), gap_end)
    await asyncio.wrap_future(recorder._post_gap_snapshot_future)
    return gap_end.replace(tzinfo=None)


@pytest.fixture
def started(config_with_account, db, db_with_gridbot_seed):
    """Factory: start a Recorder with mocked collectors / reconciler and a
    REST stub; returns (recorder, stub). Stops it after the test."""
    recorders = []
    patches = [
        patch("recorder.recorder.GapReconciler"),
        patch("recorder.recorder.PrivateCollector"),
        patch("recorder.recorder.PublicCollector"),
        patch("recorder.recorder.BybitRestClient"),
    ]

    async def _start():
        mock_reconciler_cls, mock_priv_cls, mock_pub_cls, mock_rest_cls = (
            p.start() for p in patches
        )
        _collectors(mock_pub_cls, mock_priv_cls)
        _reconciler(mock_reconciler_cls)
        stub = _rest_stub()
        mock_rest_cls.return_value = stub
        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()
        recorders.append(recorder)
        return recorder, stub

    yield _start
    for p in patches:
        p.stop()


class TestPostGapSnapshot:
    async def test_gap_close_writes_fresh_rows_after_the_gap(
        self, started, db, caplog
    ):
        """Both legs + the wallet are re-snapshotted after gap_end; no
        startup sentinel is logged."""
        recorder, _ = await started()
        try:
            before = len(_rows(db, recorder, PositionSnapshot))
            with caplog.at_level(logging.INFO, logger="recorder.recorder"):
                gap_end = await _close_gap(recorder)
            positions = _rows(db, recorder, PositionSnapshot)[before:]
            assert sorted(side for _, side, _ in positions) == ["Buy", "Sell"]
            assert all(ts > gap_end for ts, _, _ in positions)
            wallets = [ts for ts, _, _ in _rows(db, recorder, WalletSnapshot)]
            assert max(wallets) > gap_end
            messages = [r.message for r in caplog.records]
            assert not any("RECORDER_SNAPSHOT" in m for m in messages)
            assert any(m.startswith("Post-gap snapshot") for m in messages)
        finally:
            await recorder.stop()

    @pytest.mark.parametrize("positions_effect,reason", [
        (RuntimeError("timeout"), "rest_failure"),
        ([], "empty_response"),
        ([{"symbol": "BTCUSDT", "side": "Buy", "size": "x"}], "malformed"),
    ])
    async def test_no_placeholder_rows_after_a_gap(
        self, started, db, caplog, positions_effect, reason
    ):
        """A failed, empty or malformed fetch writes nothing for the symbol:
        a placeholder would be read as a flat seed / unfit end anchor."""
        recorder, stub = await started()
        try:
            before = len(_rows(db, recorder, PositionSnapshot))
            if isinstance(positions_effect, Exception):
                stub.get_positions.side_effect = positions_effect
            else:
                stub.get_positions.return_value = positions_effect
            with caplog.at_level(logging.WARNING, logger="recorder.recorder"):
                await _close_gap(recorder)
            assert _rows(db, recorder, PositionSnapshot)[before:] == []
            assert any(
                "Post-gap snapshot" in r.message and reason in r.message
                for r in caplog.records
            )
        finally:
            await recorder.stop()

    async def test_absent_side_is_still_written(self, started, db):
        """A successful fetch that omits a leg proves it flat: written."""
        recorder, stub = await started()
        try:
            before = len(_rows(db, recorder, PositionSnapshot))
            stub.get_positions.return_value = [_position("Buy")]
            await _close_gap(recorder)
            new = _rows(db, recorder, PositionSnapshot)[before:]
            sell = [raw for _, side, raw in new if side == "Sell"]
            assert sell == [{"synthetic": "absent_side"}]
        finally:
            await recorder.stop()

    async def test_gaps_during_a_snapshot_rerun_it_once(self, started):
        """Gaps closing while a snapshot runs coalesce into ONE more run."""
        recorder, stub = await started()
        release = threading.Event()
        calls = []

        def _slow_wallet(*args):
            calls.append(1)
            release.wait(5)
            return _WALLET

        stub.get_wallet_balance.side_effect = _slow_wallet
        try:
            gap_end = datetime.now(UTC)
            for i in range(3):
                recorder._handle_private_gap(
                    gap_end - timedelta(seconds=30), gap_end + timedelta(seconds=i)
                )
                await asyncio.sleep(0.05)
            future = recorder._post_gap_snapshot_future
            release.set()
            await asyncio.wrap_future(future)
            assert len(calls) == 2
        finally:
            release.set()
            await recorder.stop()

    async def test_gap_right_after_the_last_run_is_not_lost(self, started):
        """A gap handled after the snapshot coroutine returned but before its
        future is marked done (a later loop callback) still gets a run."""
        recorder, _ = await started()
        loop = asyncio.get_running_loop()
        runs = []

        async def _fake_write():
            runs.append(1)
            if len(runs) == 1:
                # Runs after the coroutine returns, before the future's
                # done-callback: the window where a gap used to be lost.
                loop.call_soon(recorder._schedule_post_gap_snapshot)

        recorder._write_post_gap_snapshot = _fake_write
        try:
            recorder._schedule_post_gap_snapshot()
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert len(runs) == 2
        finally:
            await recorder.stop()

    async def test_rest_client_failure_writes_nothing(
        self, started, db, caplog
    ):
        """A REST client that cannot be built is logged; no rows."""
        recorder, _ = await started()
        try:
            before = len(_rows(db, recorder, PositionSnapshot))
            with patch("recorder.recorder.BybitRestClient",
                       side_effect=ValueError("bad key")):
                with caplog.at_level(logging.ERROR, logger="recorder.recorder"):
                    await _close_gap(recorder)
            assert len(_rows(db, recorder, PositionSnapshot)) == before
            assert any("REST client failed" in r.message for r in caplog.records)
        finally:
            await recorder.stop()

    async def test_stop_cancels_an_in_flight_snapshot(self, started):
        """stop() does not wait for a post-gap snapshot."""
        recorder, stub = await started()
        release = threading.Event()
        stub.get_wallet_balance.side_effect = lambda *a: release.wait(5)
        try:
            gap_end = datetime.now(UTC)
            recorder._handle_private_gap(gap_end - timedelta(seconds=30), gap_end)
            await asyncio.sleep(0.05)
            future = recorder._post_gap_snapshot_future
            started_at = time.monotonic()
            await recorder.stop()
            # REST is still blocked (up to 5 s): stop() must not wait for it.
            assert time.monotonic() - started_at < 2
            assert future.cancelled() or future.done()
            assert recorder._post_gap_snapshot_running is False
        finally:
            release.set()

    async def test_lost_write_gap_also_resnapshots(self, started, db):
        """Every gap path re-snapshots, including a lost-write gap."""
        recorder, _ = await started()
        try:
            before = len(_rows(db, recorder, PositionSnapshot))
            now = datetime.now(UTC)
            recorder._recover_private_gap(
                now - timedelta(seconds=30), now, close_open=False
            )
            await asyncio.wrap_future(recorder._post_gap_snapshot_future)
            assert len(_rows(db, recorder, PositionSnapshot)) == before + 2
        finally:
            await recorder.stop()
