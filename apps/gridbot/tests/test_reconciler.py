"""Tests for gridbot reconciler module."""

from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch

import pytest

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult, CancelResult
from gridbot.reconciler import Reconciler, ReconciliationResult
from gridbot.runner import StrategyRunner
from bybit_adapter.rest_client import BybitRestClient


@pytest.fixture
def mock_rest_client():
    """Create mock REST client."""
    client = Mock()
    client.get_open_orders = MagicMock(return_value=[])
    client.cancel_order = MagicMock(return_value=True)
    return client


@pytest.fixture
def reconciler(mock_rest_client):
    """Create reconciler with mock client."""
    return Reconciler(mock_rest_client)


@pytest.fixture
def strategy_config():
    """Sample strategy configuration."""
    return StrategyConfig(
        strat_id="btcusdt_test",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.2,
        shadow_mode=False,
    )


@pytest.fixture
def mock_executor():
    """Create mock executor."""
    executor = Mock(spec=IntentExecutor)
    executor.shadow_mode = False
    executor.execute_place = MagicMock(
        return_value=OrderResult(success=True, order_id="order_123")
    )
    executor.execute_cancel = MagicMock(return_value=CancelResult(success=True))
    return executor


@pytest.fixture
def runner(strategy_config, mock_executor):
    """Create strategy runner."""
    return StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
    )


class TestReconciliationResult:
    """Tests for ReconciliationResult dataclass."""

    def test_default_values(self):
        """Test default values."""
        result = ReconciliationResult()
        assert result.orders_fetched == 0
        assert result.orders_injected == 0
        assert result.untracked_orders_on_exchange == 0
        assert result.truncated is False
        assert result.errors == []

    def test_custom_values(self):
        """Test custom values."""
        result = ReconciliationResult(
            orders_fetched=10,
            orders_injected=8,
            untracked_orders_on_exchange=2,
        )
        assert result.orders_fetched == 10
        assert result.orders_injected == 8
        assert result.untracked_orders_on_exchange == 2

    def test_accepts_truncated(self):
        result = ReconciliationResult(truncated=True)
        assert result.truncated is True


class TestReconcilerStartup:
    """Tests for startup reconciliation."""

    def test_reconcile_startup_no_orders(self, reconciler, runner, mock_rest_client):
        """Test startup reconciliation with no open orders."""
        mock_rest_client.get_open_orders.return_value = []

        result = reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 0
        assert result.orders_injected == 0
        assert result.untracked_orders_on_exchange == 0
        assert len(result.errors) == 0

    def test_reconcile_startup_with_orders(self, reconciler, runner, mock_rest_client):
        """Test startup reconciliation injects all open orders (with orderLinkId)."""
        mock_rest_client.get_open_orders.return_value = [
            {"orderId": "ex_1", "orderLinkId": "abc123def456789a",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "ex_2", "orderLinkId": "def456abc789012b",
             "price": "51000", "qty": "0.001", "side": "Sell"},
        ]

        result = reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 2
        assert result.orders_injected == 2

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 2

    def test_reconcile_startup_no_longer_filters_by_order_link_id(
        self, reconciler, runner, mock_rest_client
    ):
        """All open orders are injected regardless of orderLinkId pattern."""
        mock_rest_client.get_open_orders.return_value = [
            {"orderId": "ex_1", "orderLinkId": "abc123def456789a",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "ex_2", "orderLinkId": "manual_order",
             "price": "50000", "qty": "0.001", "side": "Sell"},
            {"orderId": "ex_3",
             "price": "51000", "qty": "0.001", "side": "Buy", "reduceOnly": True},
        ]

        result = reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 3
        assert result.orders_injected == 3

    def test_reconcile_startup_api_error(self, reconciler, runner, mock_rest_client):
        """Test startup reconciliation with API error."""
        mock_rest_client.get_open_orders.side_effect = Exception("API error")

        result = reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 0
        assert len(result.errors) == 1
        assert "API error" in result.errors[0]


class TestReconcilerReconnect:
    """Tests for reconnect reconciliation."""

    def test_reconcile_reconnect_in_sync(self, reconciler, runner, mock_rest_client):
        """Test reconnect when state is in sync."""
        runner.inject_open_orders([
            {"orderId": "ex_1", "price": "49000", "qty": "0.001", "side": "Buy"},
        ])

        mock_rest_client.get_open_orders.return_value = ([
            {"orderId": "ex_1"},
        ], False)

        result = reconciler.reconcile_reconnect(runner)

        assert result.orders_fetched == 1
        assert result.untracked_orders_on_exchange == 0

    def test_reconcile_reconnect_missing_on_exchange(self, reconciler, runner, mock_rest_client):
        """Test reconnect when order is in memory but not on exchange."""
        runner.inject_open_orders([
            {"orderId": "ex_1", "price": "49000", "qty": "0.001", "side": "Buy"},
        ])

        mock_rest_client.get_open_orders.return_value = ([], False)

        result = reconciler.reconcile_reconnect(runner)

        assert result.orders_fetched == 0
        # Tracked by orderId
        assert runner._tracked_orders["ex_1"].status == "cancelled"

    def test_reconcile_reconnect_missing_in_memory(self, reconciler, runner, mock_rest_client):
        """Test reconnect when order is on exchange but not in memory."""
        mock_rest_client.get_open_orders.return_value = ([
            {"orderId": "ex_new", "price": "50000", "qty": "0.001", "side": "Sell"},
        ], False)

        result = reconciler.reconcile_reconnect(runner)

        assert result.orders_fetched == 1
        assert result.untracked_orders_on_exchange == 1
        assert result.orders_injected == 1

    def test_reconnect_requests_truncation_status(
        self, reconciler, runner, mock_rest_client
    ):
        mock_rest_client.get_open_orders.return_value = ([], False)

        result = reconciler.reconcile_reconnect(runner)

        assert result.truncated is False
        mock_rest_client.get_open_orders.assert_called_once_with(
            symbol=runner.symbol,
            order_type="Limit",
            return_truncated=True,
        )

    def test_truncated_reconnect_skips_cancellation_and_injects_orphans(
        self, reconciler, runner, mock_rest_client
    ):
        runner.inject_open_orders([
            {"orderId": "tracked", "price": "49000", "qty": "0.001", "side": "Buy"},
        ])
        runner.mark_order_cancelled_by_order_id = MagicMock(
            wraps=runner.mark_order_cancelled_by_order_id
        )
        runner.inject_open_orders = MagicMock(wraps=runner.inject_open_orders)
        orphan = {"orderId": "orphan", "price": "50000", "qty": "0.001", "side": "Sell"}
        mock_rest_client.get_open_orders.return_value = ([orphan], True)

        result = reconciler.reconcile_reconnect(runner)

        assert result.orders_fetched == 1
        assert result.truncated is True
        runner.mark_order_cancelled_by_order_id.assert_not_called()
        runner.inject_open_orders.assert_called_once_with([orphan])
        assert result.untracked_orders_on_exchange == 1
        assert result.orders_injected == 1

    def test_truncated_reconnect_keeps_unfetched_tracked_order_and_adopts_fetched_orphan(
        self, runner
    ):
        """Real adapter pagination proves the issue #207 acceptance case."""
        runner.inject_open_orders([
            {"orderId": "unfetched", "price": "49000", "qty": "0.001", "side": "Buy"},
        ])
        session = MagicMock()
        fetched_orphan = {
            "orderId": "fetched_orphan",
            "orderType": "Limit",
            "price": "50000",
            "qty": "0.001",
            "side": "Sell",
        }
        session.get_open_orders.side_effect = [
            {"retCode": 0, "retMsg": "OK", "result": {
                "list": [fetched_orphan], "nextPageCursor": "unfetched-page",
            }},
            {"retCode": 0, "retMsg": "OK", "result": {
                "list": [{
                    "orderId": "another-page",
                    "orderType": "Limit",
                    "price": "51000",
                    "qty": "0.001",
                    "side": "Sell",
                }],
                "nextPageCursor": "still-more",
            }},
        ]
        with patch("bybit_adapter.rest_client.HTTP", return_value=session):
            client = BybitRestClient(api_key="key", api_secret="secret")
        reconciler = Reconciler(client)
        # Exercise the adapter's actual pagination with a tight test page limit.
        original_get_open_orders = client.get_open_orders
        client.get_open_orders = MagicMock(
            side_effect=lambda **kwargs: original_get_open_orders(max_pages=1, **kwargs)
        )

        result = reconciler.reconcile_reconnect(runner)

        assert result.truncated is True
        assert runner._tracked_orders["unfetched"].status != "cancelled"
        assert "fetched_orphan" in runner._tracked_orders
        assert session.get_open_orders.call_count == 1

    def test_truncated_reconnect_preserves_flag_when_injection_fails(
        self, reconciler, runner, mock_rest_client
    ):
        mock_rest_client.get_open_orders.return_value = ([{"orderId": "orphan"}], True)
        runner.inject_open_orders = MagicMock(side_effect=Exception("inject failed"))

        result = reconciler.reconcile_reconnect(runner)

        assert result.truncated is True
        assert result.errors == ["inject failed"]
