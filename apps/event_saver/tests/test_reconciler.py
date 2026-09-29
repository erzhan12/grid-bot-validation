"""Tests for GapReconciler."""

import pytest
import unittest.mock
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from concurrent.futures import Future
from unittest.mock import MagicMock
from uuid import uuid4

from bybit_adapter.rest_client import BybitRestClient
from grid_db import (
    BybitAccount,
    RecoveryStatus,
    DatabaseFactory,
    DatabaseSettings,
    PrivateExecution,
    Run,
    Strategy,
    User,
)

from event_saver.reconciler import (
    ExecutionRecoveryResult,
    GapReconciler,
    recovery_result_from_future,
    _PRIVATE_EXECUTION_RECONCILE_MAX_PAGES,
    _RECOVERY_WINDOW_MARGIN,
)


@pytest.fixture
def mock_db():
    """Create mock DatabaseFactory."""
    db = MagicMock(spec=DatabaseFactory)
    session = MagicMock()
    db.get_session.return_value.__enter__ = MagicMock(return_value=session)
    db.get_session.return_value.__exit__ = MagicMock(return_value=False)
    return db


@pytest.fixture
def mock_rest_client():
    """Create mock BybitRestClient."""
    client = MagicMock(spec=BybitRestClient)
    client.get_recent_trades = MagicMock(return_value=[])
    client.get_executions = MagicMock(return_value=([], None))
    return client


class TestGapReconcilerInit:
    """Test GapReconciler initialization."""

    def test_initialization(self, mock_db, mock_rest_client):
        """Test GapReconciler initialization with default values."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        assert reconciler._gap_threshold == 5.0
        assert reconciler._trades_reconciled == 0
        assert reconciler._executions_reconciled == 0

    def test_custom_threshold(self, mock_db, mock_rest_client):
        """Test GapReconciler with custom threshold."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=10.0,
        )

        assert reconciler._gap_threshold == 10.0


class TestShouldReconcile:
    """Test gap detection logic."""

    def test_gap_above_threshold(self, mock_db, mock_rest_client):
        """Test that gaps above threshold should reconcile."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=10)

        assert reconciler.should_reconcile(gap_start, gap_end) is True

    def test_gap_below_threshold(self, mock_db, mock_rest_client):
        """Test that gaps below threshold should not reconcile."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=3)

        assert reconciler.should_reconcile(gap_start, gap_end) is False

    def test_gap_at_threshold(self, mock_db, mock_rest_client):
        """Test that gaps exactly at threshold should reconcile."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=5)

        assert reconciler.should_reconcile(gap_start, gap_end) is True


class TestReconcilePublicTrades:
    """Test public trade reconciliation."""

    @pytest.mark.asyncio
    async def test_skips_small_gap(self, mock_db, mock_rest_client):
        """Test that small gaps are skipped."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=2)

        count = await reconciler.reconcile_public_trades(
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
        )

        assert count == 0
        mock_rest_client.get_recent_trades.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_rest_api(self, mock_db, mock_rest_client):
        """Test that REST API is called for valid gaps."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=10)

        mock_rest_client.get_recent_trades.return_value = []

        mock_repo = MagicMock()
        mock_repo.get_last_trade_ts.return_value = None
        with unittest.mock.patch(
            "event_saver.reconciler.PublicTradeRepository",
            return_value=mock_repo,
        ):
            await reconciler.reconcile_public_trades(
                symbol="BTCUSDT",
                gap_start=gap_start,
                gap_end=gap_end,
            )

        mock_rest_client.get_recent_trades.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_empty_response(self, mock_db, mock_rest_client):
        """Test handling of empty REST API response."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=10)

        mock_rest_client.get_recent_trades.return_value = []

        mock_repo = MagicMock()
        mock_repo.get_last_trade_ts.return_value = None
        with unittest.mock.patch(
            "event_saver.reconciler.PublicTradeRepository",
            return_value=mock_repo,
        ):
            count = await reconciler.reconcile_public_trades(
                symbol="BTCUSDT",
                gap_start=gap_start,
                gap_end=gap_end,
            )

        assert count == 0
        mock_rest_client.get_recent_trades.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconcile_start_is_capped_at_gap_start(
        self, mock_db, mock_rest_client
    ):
        """Post-gap live writes must not cause public-trade backfill to skip the gap."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=10)
        trade_ts_ms = int((gap_start + timedelta(seconds=5)).timestamp() * 1000)
        # A trade well before the gap: must be excluded by the gap_start-based
        # lower bound. If the filter window started earlier it would leak in.
        pre_gap_ts_ms = int((gap_start - timedelta(seconds=30)).timestamp() * 1000)

        mock_session = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_last_trade_ts.return_value = gap_end + timedelta(seconds=30)
        mock_repo.bulk_insert.return_value = 1
        mock_db.get_session.return_value.__enter__.return_value = mock_session
        mock_db.get_session.return_value.__exit__.return_value = None

        mock_rest_client.get_recent_trades.return_value = [
            {
                "execId": "pre_gap_trade",
                "time": pre_gap_ts_ms,
                "side": "Buy",
                "price": "50000.00",
                "size": "0.001",
            },
            {
                "execId": "gap_trade",
                "time": trade_ts_ms,
                "side": "Buy",
                "price": "50000.00",
                "size": "0.001",
            },
        ]

        with unittest.mock.patch(
            "event_saver.reconciler.PublicTradeRepository",
            return_value=mock_repo,
        ):
            count = await reconciler.reconcile_public_trades(
                symbol="BTCUSDT",
                gap_start=gap_start,
                gap_end=gap_end,
            )

        assert count == 1
        mock_repo.bulk_insert.assert_called_once()
        inserted = mock_repo.bulk_insert.call_args.args[0]
        # Only the in-gap trade survives: window floored at gap_start (capturing
        # the outage) while excluding the unrelated pre-gap trade. Proves the
        # lower bound is gap_start, not last_persisted_ts (gap_end + 30s).
        assert len(inserted) == 1
        assert inserted[0].trade_id == "gap_trade"

    @pytest.mark.asyncio
    async def test_reconcile_start_when_last_persisted_equals_gap_start(
        self, mock_db, mock_rest_client
    ):
        """last_persisted_ts == gap_start falls through to gap_start (strict <)."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=10)
        trade_ts_ms = int((gap_start + timedelta(seconds=3)).timestamp() * 1000)

        mock_session = MagicMock()
        mock_repo = MagicMock()
        # Equality must NOT take the if-branch (comparison is strict <).
        mock_repo.get_last_trade_ts.return_value = gap_start
        mock_repo.bulk_insert.return_value = 1
        mock_db.get_session.return_value.__enter__.return_value = mock_session
        mock_db.get_session.return_value.__exit__.return_value = None

        mock_rest_client.get_recent_trades.return_value = [
            {
                "execId": "gap_trade",
                "time": trade_ts_ms,
                "side": "Buy",
                "price": "50000.00",
                "size": "0.001",
            }
        ]

        with unittest.mock.patch(
            "event_saver.reconciler.PublicTradeRepository",
            return_value=mock_repo,
        ):
            count = await reconciler.reconcile_public_trades(
                symbol="BTCUSDT",
                gap_start=gap_start,
                gap_end=gap_end,
            )

        assert count == 1
        inserted = mock_repo.bulk_insert.call_args.args[0]
        assert inserted[0].trade_id == "gap_trade"

    @pytest.mark.asyncio
    async def test_handles_api_error(self, mock_db, mock_rest_client):
        """Test handling of REST API errors."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=10)

        mock_rest_client.get_recent_trades.side_effect = Exception("API Error")

        mock_repo = MagicMock()
        mock_repo.get_last_trade_ts.return_value = None
        with unittest.mock.patch(
            "event_saver.reconciler.PublicTradeRepository",
            return_value=mock_repo,
        ):
            count = await reconciler.reconcile_public_trades(
                symbol="BTCUSDT",
                gap_start=gap_start,
                gap_end=gap_end,
            )

        assert count == 0


async def _run_exec_recovery(
    reconciler,
    *,
    gap_start,
    gap_end,
    run_id="default",
    rest_result=([], False),
    rest_exc=None,
    bulk_count=None,
    bulk_exc=None,
):
    """Run reconcile_executions with the REST client and DB repo mocked.

    Returns ``(result, rest_client_mock, repo_mock)``.
    """
    repo = MagicMock()
    if bulk_exc is not None:
        repo.bulk_insert.side_effect = bulk_exc
    else:
        repo.bulk_insert.side_effect = lambda models: (
            len(models) if bulk_count is None else bulk_count
        )
    client = MagicMock()
    if rest_exc is not None:
        client.get_executions_all.side_effect = rest_exc
    else:
        client.get_executions_all.return_value = rest_result

    async def _to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with unittest.mock.patch(
        "event_saver.reconciler.PrivateExecutionRepository", return_value=repo
    ), unittest.mock.patch(
        "event_saver.reconciler.asyncio.to_thread", side_effect=_to_thread
    ), unittest.mock.patch(
        "event_saver.reconciler.BybitRestClient", return_value=client
    ):
        result = await reconciler.reconcile_executions(
            user_id=uuid4(),
            account_id=uuid4(),
            run_id=uuid4() if run_id == "default" else run_id,
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
            api_key="key",
            api_secret="secret",
            testnet=True,
        )
    return result, client, repo


def _rest_row(exec_id, price="50000"):
    return {
        "execType": "Trade", "execId": exec_id, "orderId": f"o-{exec_id}",
        "orderLinkId": f"l-{exec_id}", "symbol": "BTCUSDT", "side": "Buy",
        "execPrice": price, "execQty": "0.001", "execFee": "0.01",
        "execTime": 1700000000000, "closedSize": "0",
    }


class TestRecoveryResultFromFuture:
    """Finished recovery futures map to an outcome, never an exception."""

    def test_result_passes_through(self):
        """A completed recovery returns its own result."""
        fut = Future()
        expected = ExecutionRecoveryResult(RecoveryStatus.RECOVERED, 2, 1)
        fut.set_result(expected)
        assert recovery_result_from_future(fut) is expected

    def test_crashed_recovery_is_failed(self):
        """An exception becomes FAILED with the error in the reason."""
        fut = Future()
        fut.set_exception(RuntimeError("boom"))
        result = recovery_result_from_future(fut)
        assert result.status == RecoveryStatus.FAILED
        assert "recovery crashed: boom" == result.reason

    def test_cancelled_recovery_is_failed(self):
        """A cancelled recovery (e.g. shutdown) becomes FAILED."""
        fut = Future()
        fut.cancel()
        result = recovery_result_from_future(fut)
        assert result.status == RecoveryStatus.FAILED
        assert result.reason == "recovery cancelled"


class TestReconcileExecutions:
    """Execution recovery returns a structured, persistable outcome (0110 B1a)."""

    _GAP_START = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    _GAP_END = _GAP_START + timedelta(seconds=40)

    def _reconciler(self, mock_db, mock_rest_client):
        return GapReconciler(
            db=mock_db, rest_client=mock_rest_client, gap_threshold_seconds=5.0
        )

    async def test_small_gap_is_skipped(self, mock_db, mock_rest_client):
        """A gap below the threshold is SKIPPED, never labelled recovered."""
        result, client, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_START + timedelta(seconds=2),
        )
        assert result.status == RecoveryStatus.SKIPPED
        client.get_executions_all.assert_not_called()

    async def test_query_window_brackets_the_gap(self, mock_db, mock_rest_client):
        """REST window = [gap_start - margin, gap_end + margin], paginated."""
        _, client, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
        )
        kwargs = client.get_executions_all.call_args.kwargs
        margin = _RECOVERY_WINDOW_MARGIN
        assert kwargs["start_time"] == int(
            (self._GAP_START - margin).timestamp() * 1000
        )
        assert kwargs["end_time"] == int((self._GAP_END + margin).timestamp() * 1000)
        assert kwargs["max_pages"] == _PRIVATE_EXECUTION_RECONCILE_MAX_PAGES
        assert kwargs["return_truncated"] is True

    async def test_empty_history_is_recovered(self, mock_db, mock_rest_client):
        """A complete query with no executions is a successful recovery."""
        result, _, repo = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
        )
        assert result.status == RecoveryStatus.RECOVERED
        assert (result.inserted, result.duplicates) == (0, 0)
        repo.bulk_insert.assert_not_called()

    async def test_inserted_and_duplicates_are_counted(
        self, mock_db, mock_rest_client
    ):
        """Rows the DB already had count as duplicates, not failures."""
        rows = [_rest_row("e1"), _rest_row("e2"), _rest_row("e3")]
        result, _, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
            rest_result=(rows, False),
            bulk_count=2,
        )
        assert result.status == RecoveryStatus.RECOVERED
        assert (result.inserted, result.duplicates) == (2, 1)

    async def test_truncated_backfill_is_not_persisted(
        self, mock_db, mock_rest_client, caplog
    ):
        """max_pages reached with a cursor outstanding → TRUNCATED, no writes."""
        with caplog.at_level("ERROR", logger="event_saver.reconciler"):
            result, _, repo = await _run_exec_recovery(
                self._reconciler(mock_db, mock_rest_client),
                gap_start=self._GAP_START,
                gap_end=self._GAP_END,
                rest_result=([_rest_row("partial")], True),
            )
        assert result.status == RecoveryStatus.TRUNCATED
        repo.bulk_insert.assert_not_called()
        assert any("truncated" in r.message for r in caplog.records)

    async def test_rest_error_is_failed(self, mock_db, mock_rest_client):
        """A REST error (e.g. bad envelope category) is FAILED, not empty."""
        result, _, repo = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
            rest_exc=ValueError("expected result.category='linear'"),
        )
        assert result.status == RecoveryStatus.FAILED
        assert "result.category" in result.reason
        repo.bulk_insert.assert_not_called()

    async def test_missing_run_id_is_failed(self, mock_db, mock_rest_client):
        """Without a run_id nothing can be persisted: FAILED, no REST call."""
        result, client, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
            run_id=None,
        )
        assert result.status == RecoveryStatus.FAILED
        assert "run_id" in result.reason
        client.get_executions_all.assert_not_called()

    async def test_window_over_seven_days_recovers_clamped_tail(
        self, mock_db, mock_rest_client
    ):
        """Over Bybit's 7-day cap: query the latest 7 days, keep those rows,
        and still mark the gap FAILED because its head is unrecovered."""
        gap_end = self._GAP_START + timedelta(days=9)
        result, client, repo = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=gap_end,
            rest_result=([_rest_row("tail")], False),
        )
        kwargs = client.get_executions_all.call_args.kwargs
        query_end = gap_end + _RECOVERY_WINDOW_MARGIN
        assert kwargs["end_time"] == int(query_end.timestamp() * 1000)
        assert kwargs["start_time"] == int(
            (query_end - timedelta(days=7)).timestamp() * 1000
        )
        (models,), _ = repo.bulk_insert.call_args
        assert [m.exec_id for m in models] == ["tail"]
        assert result.status == RecoveryStatus.FAILED
        assert result.inserted == 1
        assert "7 days" in result.reason and "unrecovered" in result.reason

    async def test_duplicates_count_distinct_exec_ids(
        self, mock_db, mock_rest_client
    ):
        """A repeated exec_id across REST pages is not a DB duplicate."""
        rows = [_rest_row("e1"), _rest_row("e1"), _rest_row("e2")]
        result, _, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
            rest_result=(rows, False),
            bulk_count=1,
        )
        assert result.status == RecoveryStatus.RECOVERED
        assert (result.inserted, result.duplicates) == (1, 1)

    async def test_row_conversion_error_is_failed_but_good_rows_kept(
        self, mock_db, mock_rest_client, caplog
    ):
        """A dropped Trade row makes the recovery FAILED; valid rows persist."""
        rows = [_rest_row("good"), _rest_row("bad", price="not-a-number")]
        with caplog.at_level("WARNING", logger="event_saver.reconciler"):
            result, _, repo = await _run_exec_recovery(
                self._reconciler(mock_db, mock_rest_client),
                gap_start=self._GAP_START,
                gap_end=self._GAP_END,
                rest_result=(rows, False),
            )
        assert "execId='bad'" in caplog.text  # dropped row named
        assert result.status == RecoveryStatus.FAILED
        assert "1 of 2" in result.reason
        (models,), _ = repo.bulk_insert.call_args
        assert [m.exec_id for m in models] == ["good"]
        assert result.inserted == 1

    async def test_non_trade_rows_do_not_fail_recovery(
        self, mock_db, mock_rest_client
    ):
        """Funding/settlement rows are filtered, not counted as dropped."""
        rows = [_rest_row("e1"), {"execType": "Funding", "execId": "f1"}]
        result, _, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
            rest_result=(rows, False),
        )
        assert result.status == RecoveryStatus.RECOVERED
        assert result.inserted == 1

    async def test_window_of_exactly_seven_days_is_queried(
        self, mock_db, mock_rest_client
    ):
        """The 7-day cap is inclusive: a window of exactly 7 days is queried."""
        gap_end = (
            self._GAP_START + timedelta(days=7) - 2 * _RECOVERY_WINDOW_MARGIN
        )
        result, client, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=gap_end,
        )
        client.get_executions_all.assert_called_once()
        assert result.status == RecoveryStatus.RECOVERED

    async def test_malformed_row_is_failed_not_raised(
        self, mock_db, mock_rest_client, caplog
    ):
        """A non-dict REST row is a dropped row: FAILED, valid rows kept."""
        with caplog.at_level("WARNING", logger="event_saver.reconciler"):
            result, _, _ = await _run_exec_recovery(
                self._reconciler(mock_db, mock_rest_client),
                gap_start=self._GAP_START,
                gap_end=self._GAP_END,
                rest_result=([_rest_row("e1"), "garbage"], False),
            )
        assert "'garbage'" in caplog.text  # non-dict row named by repr
        assert result.status == RecoveryStatus.FAILED
        assert "1 of 2" in result.reason
        assert result.inserted == 1

    async def test_row_without_exec_type_is_dropped_not_filtered(
        self, mock_db, mock_rest_client, caplog
    ):
        """A dict with no execType is not provably non-Trade: FAILED, and the
        dropped row is named in a WARNING so it can be recovered by hand."""
        no_type = {k: v for k, v in _rest_row("e2").items() if k != "execType"}
        with caplog.at_level("WARNING", logger="event_saver.reconciler"):
            result, _, _ = await _run_exec_recovery(
                self._reconciler(mock_db, mock_rest_client),
                gap_start=self._GAP_START,
                gap_end=self._GAP_END,
                rest_result=([_rest_row("e1"), no_type], False),
            )
        assert "execId='e2'" in caplog.text
        assert result.status == RecoveryStatus.FAILED
        assert "1 of 2" in result.reason
        assert result.inserted == 1

    async def test_commit_error_is_failed(self, mock_db, mock_rest_client):
        """A failure at session commit (context exit) is FAILED, not zero."""
        mock_db.get_session.return_value.__exit__.side_effect = RuntimeError(
            "commit failed"
        )
        result, _, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
            rest_result=([_rest_row("e1")], False),
        )
        assert result.status == RecoveryStatus.FAILED
        assert "commit failed" in result.reason

    async def test_db_error_is_failed(self, mock_db, mock_rest_client):
        """A commit failure is FAILED, never a silent zero."""
        result, _, _ = await _run_exec_recovery(
            self._reconciler(mock_db, mock_rest_client),
            gap_start=self._GAP_START,
            gap_end=self._GAP_END,
            rest_result=([_rest_row("e1")], False),
            bulk_exc=RuntimeError("database is locked"),
        )
        assert result.status == RecoveryStatus.FAILED
        assert "database is locked" in result.reason


class TestTradesConversion:
    """Test trade data to model conversion."""

    def test_trades_to_models(self, mock_db, mock_rest_client):
        """Test conversion of trade data to models."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        trades = [
            {
                "execId": "trade_1",
                "time": 1700000000000,
                "side": "Buy",
                "price": "50000.00",
                "size": "0.001",
            },
            {
                "execId": "trade_2",
                "time": 1700000001000,
                "side": "Sell",
                "price": "50001.00",
                "size": "0.002",
            },
        ]

        models = reconciler._trades_to_models("BTCUSDT", trades)

        assert len(models) == 2
        assert models[0].symbol == "BTCUSDT"
        assert models[0].trade_id == "trade_1"
        assert models[0].side == "Buy"
        assert models[0].price == Decimal("50000.00")

    def test_trades_to_models_handles_bad_data(self, mock_db, mock_rest_client):
        """Test that bad trade data uses default values."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        trades = [
            {"invalid": "data"},  # Will use defaults
            {
                "execId": "trade_1",
                "time": 1700000000000,
                "side": "Buy",
                "price": "50000.00",
                "size": "0.001",
            },
        ]

        models = reconciler._trades_to_models("BTCUSDT", trades)

        # Both trades converted (first with defaults)
        assert len(models) == 2
        assert models[1].trade_id == "trade_1"


class TestExecutionsConversion:
    """Test execution data to model conversion."""

    def test_executions_to_models(self, mock_db, mock_rest_client):
        """Test conversion of execution data to models."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        user_id = uuid4()
        account_id = uuid4()
        run_id = uuid4()

        executions = [
            {
                "execType": "Trade",
                "execId": "exec_1",
                "orderId": "order_1",
                "orderLinkId": "link_1",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "execPrice": "50000.00",
                "execQty": "0.001",
                "execFee": "0.01",
                "feeCurrency": "USDT",
                "closedPnl": "0",
                "execTime": 1700000000000,
            },
        ]

        models = reconciler._executions_to_models(
            user_id=user_id,
            account_id=account_id,
            run_id=run_id,
            executions=executions,
        )

        assert len(models) == 1
        assert models[0].account_id == str(account_id)
        assert models[0].run_id == str(run_id)
        assert models[0].exec_id == "exec_1"

    def test_executions_to_models_requires_run_id(self, mock_db, mock_rest_client):
        """Test that None run_id returns empty list."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        executions = [
            {
                "execType": "Trade",
                "execId": "exec_1",
                "orderId": "order_1",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "execPrice": "50000.00",
                "execQty": "0.001",
                "execFee": "0.01",
                "closedPnl": "0",
                "execTime": 1700000000000,
            },
        ]

        models = reconciler._executions_to_models(
            user_id=uuid4(),
            account_id=uuid4(),
            run_id=None,  # No run_id
            executions=executions,
        )

        assert len(models) == 0

    def test_rest_backfill_accepts_documented_bybit_execution_shape(
        self, mock_db, mock_rest_client
    ):
        """REST rows carry no per-item category or closedPnl (audit #270 F4).

        Ported from cad63f4 ``test_rest_backfill_drops_documented_bybit_
        execution_shape`` with inverted assertions: the category-free row
        converts, and a closing row without PnL stays unknown (None).
        """
        row = {
            "symbol": "LTCUSDT", "execId": "rest-1", "orderId": "order-1",
            "orderLinkId": "open-1", "side": "Buy", "execPrice": "100",
            "execQty": "1", "execFee": "0.02", "execType": "Trade",
            "execTime": "1790481601000", "closedSize": "0",
        }
        reconciler = GapReconciler(db=mock_db, rest_client=mock_rest_client)
        ids = dict(user_id=uuid4(), account_id=uuid4(), run_id=uuid4())

        models = reconciler._executions_to_models(**ids, executions=[row])
        assert len(models) == 1
        assert models[0].exec_id == "rest-1"
        assert models[0].raw_json == row

        closing = reconciler._executions_to_models(
            **ids, executions=[row | {"side": "Sell", "closedSize": "1"}]
        )
        assert len(closing) == 1
        assert closing[0].closed_pnl is None

    @pytest.mark.parametrize(
        "extra, expected",
        [
            ({"closedSize": "0"}, Decimal("0")),  # opening fill: known zero
            ({"closedSize": "0.000"}, Decimal("0")),
            ({"closedSize": "1"}, None),  # closing fill: unknown
            ({"closedSize": ""}, None),  # empty (e.g. USDC-perp rows)
            ({"closedSize": "n/a"}, None),  # malformed: kept, PnL unknown
            ({"closedSize": "0", "execPnl": "n/a"}, None),  # malformed PnL
            ({}, None),  # absent
            ({"closedSize": "0", "execPnl": "0.4"}, Decimal("0.4")),
        ],
    )
    def test_rest_opening_fill_pnl_is_known_zero(
        self, mock_db, mock_rest_client, extra, expected
    ):
        """closedSize == 0 means nothing closed: realized PnL is exactly 0."""
        row = {
            "symbol": "LTCUSDT", "execId": "rest-1", "orderId": "order-1",
            "orderLinkId": "open-1", "side": "Buy", "execPrice": "100",
            "execQty": "1", "execFee": "0.02", "execType": "Trade",
            "execTime": "1790481601000", **extra,
        }
        reconciler = GapReconciler(db=mock_db, rest_client=mock_rest_client)
        models = reconciler._executions_to_models(
            user_id=uuid4(), account_id=uuid4(), run_id=uuid4(),
            executions=[row],
        )
        assert models[0].closed_pnl == expected

    def test_executions_filters_exec_type(self, mock_db, mock_rest_client):
        """Test that non-Trade executions are filtered out."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        executions = [
            {
                "execType": "Funding",  # Not Trade
                "execId": "exec_1",
                "orderId": "order_1",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "execPrice": "50000.00",
                "execQty": "0.001",
                "execFee": "0.01",
                "closedPnl": "0",
                "execTime": 1700000000000,
            },
        ]

        models = reconciler._executions_to_models(
            user_id=uuid4(),
            account_id=uuid4(),
            run_id=uuid4(),
            executions=executions,
        )

        assert len(models) == 0


class TestGetStats:
    """Test statistics retrieval."""

    def test_get_stats(self, mock_db, mock_rest_client):
        """Test get_stats returns correct values."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        reconciler._trades_reconciled = 100
        reconciler._executions_reconciled = 50
        reconciler._reconciliation_count = 10

        stats = reconciler.get_stats()

        assert stats["trades_reconciled"] == 100
        assert stats["executions_reconciled"] == 50
        assert stats["reconciliation_count"] == 10


class TestDocumentedRestPayloadPersistence:
    """Contract: documented Bybit envelope → adapter → reconciler → SQLite."""

    @pytest.mark.asyncio
    async def test_documented_rest_payload_is_persisted_with_unknown_pnl(self):
        """Mock only the HTTP transport; NULL PnL survives to the DB row."""
        db = DatabaseFactory(
            DatabaseSettings(db_type="sqlite", db_name=":memory:")
        )
        db.create_tables()
        try:
            with db.get_session() as session:
                user = User(username="u", email="u@example.invalid")
                session.add(user)
                session.flush()
                account = BybitAccount(
                    user_id=user.user_id, account_name="a",
                    environment="testnet",
                )
                session.add(account)
                session.flush()
                strategy = Strategy(
                    account_id=account.account_id,
                    strategy_type="GridStrategy",
                    symbol="LTCUSDT",
                    config_json={},
                )
                session.add(strategy)
                session.flush()
                run = Run(
                    user_id=user.user_id,
                    account_id=account.account_id,
                    strategy_id=strategy.strategy_id,
                    run_type="recording",
                    start_ts=datetime(2026, 9, 1, tzinfo=UTC),
                )
                session.add(run)
                session.flush()
                user_id, account_id, run_id = (
                    user.user_id, account.account_id, run.run_id
                )

            gap_start = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
            gap_end = gap_start + timedelta(minutes=2)
            exec_ms = int(
                (gap_start + timedelta(seconds=30)).timestamp() * 1000
            )
            http = MagicMock()
            http.get_executions.return_value = {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "category": "linear",
                    "nextPageCursor": "",
                    "list": [{
                        "symbol": "LTCUSDT", "execId": "rest-1",
                        "orderId": "order-1", "orderLinkId": "link-1",
                        "side": "Sell", "execPrice": "100.5",
                        "execQty": "0.2", "execFee": "0.011",
                        "execType": "Trade", "execTime": str(exec_ms),
                        "closedSize": "0.2",
                    }],
                },
            }
            reconciler = GapReconciler(
                db=db,
                rest_client=MagicMock(spec=BybitRestClient),
                gap_threshold_seconds=5.0,
            )
            with unittest.mock.patch(
                "bybit_adapter.rest_client.HTTP", return_value=http
            ):
                result = await reconciler.reconcile_executions(
                    user_id=user_id,
                    account_id=account_id,
                    run_id=run_id,
                    symbol="LTCUSDT",
                    gap_start=gap_start,
                    gap_end=gap_end,
                    api_key="k",
                    api_secret="s",
                    testnet=True,
                )

            assert result.status == RecoveryStatus.RECOVERED
            assert result.inserted == 1
            with db.get_session() as session:
                row = session.query(PrivateExecution).one()
                assert row.exec_id == "rest-1"
                assert row.order_id == "order-1"
                assert row.order_link_id == "link-1"
                assert row.symbol == "LTCUSDT"
                assert row.raw_json["closedSize"] == "0.2"
                assert row.run_id == str(run_id)
                assert row.side == "Sell"
                assert row.exec_price == Decimal("100.5")
                assert row.exec_qty == Decimal("0.2")
                assert row.exec_fee == Decimal("0.011")
                assert row.closed_pnl is None
                assert row.exchange_ts.replace(tzinfo=UTC) == (
                    datetime.fromtimestamp(exec_ms / 1000, tz=UTC)
                )
        finally:
            db.drop_tables()
