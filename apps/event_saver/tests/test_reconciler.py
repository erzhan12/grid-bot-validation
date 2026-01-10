"""Tests for GapReconciler."""

import pytest
import unittest.mock
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from bybit_adapter.rest_client import BybitRestClient
from grid_db import DatabaseFactory

from event_saver.reconciler import GapReconciler


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

        count = await reconciler.reconcile_public_trades(
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
        )

        assert count == 0

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

        count = await reconciler.reconcile_public_trades(
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
        )

        assert count == 0


class TestReconcileExecutions:
    """Test execution reconciliation."""

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

        count = await reconciler.reconcile_executions(
            user_id=uuid4(),
            account_id=uuid4(),
            run_id=uuid4(),
            symbol="BTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
            api_key="key",
            api_secret="secret",
            testnet=True,
        )

        assert count == 0
        mock_rest_client.get_executions.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_rest_api(self, mock_db, mock_rest_client, monkeypatch):
        """Test that REST API is called for valid gaps."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
            gap_threshold_seconds=5.0,
        )

        gap_start = datetime.now(UTC)
        gap_end = gap_start + timedelta(seconds=10)

        # Mock repo to return None for last_execution_ts
        mock_session = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_last_execution_ts.return_value = None
        mock_db.get_session.return_value.__enter__.return_value = mock_session
        mock_db.get_session.return_value.__exit__.return_value = None

        # Mock asyncio.to_thread to execute the function synchronously
        async def mock_to_thread(func, *args, **kwargs):
            # Just call the function directly (synchronously)
            return func(*args, **kwargs)

        # Mock the BybitRestClient constructor
        mock_client = MagicMock()
        mock_client.get_executions_all.return_value = []

        # Patch BybitRestClient constructor, PrivateExecutionRepository, and asyncio.to_thread
        with unittest.mock.patch('event_saver.reconciler.PrivateExecutionRepository', return_value=mock_repo), \
             unittest.mock.patch('event_saver.reconciler.asyncio.to_thread', side_effect=mock_to_thread), \
             unittest.mock.patch('event_saver.reconciler.BybitRestClient', return_value=mock_client):

            count = await reconciler.reconcile_executions(
                user_id=uuid4(),
                account_id=uuid4(),
                run_id=uuid4(),
                symbol="BTCUSDT",
                gap_start=gap_start,
                gap_end=gap_end,
                api_key="test_key",
                api_secret="test_secret",
                testnet=True,
            )

        # Verify reconciliation completed (would return 0 for empty list)
        assert count == 0


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
                "category": "linear",
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
                "category": "linear",
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

    def test_executions_filters_category(self, mock_db, mock_rest_client):
        """Test that non-linear executions are filtered out."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        executions = [
            {
                "category": "spot",  # Not linear
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
            run_id=uuid4(),
            executions=executions,
        )

        assert len(models) == 0

    def test_executions_filters_exec_type(self, mock_db, mock_rest_client):
        """Test that non-Trade executions are filtered out."""
        reconciler = GapReconciler(
            db=mock_db,
            rest_client=mock_rest_client,
        )

        executions = [
            {
                "category": "linear",
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
