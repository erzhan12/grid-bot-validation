"""Tests for gridbot reconciler module."""

from decimal import Decimal
from unittest.mock import Mock, MagicMock

import pytest

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult, CancelResult
from gridbot.reconciler import Reconciler, ReconciliationResult
from gridbot.runner import StrategyRunner


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


class TestReconcilerStartup:
    """Tests for startup reconciliation."""

    @pytest.mark.asyncio
    async def test_reconcile_startup_no_orders(self, reconciler, runner, mock_rest_client):
        """Test startup reconciliation with no open orders."""
        mock_rest_client.get_open_orders.return_value = []

        result = await reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 0
        assert result.orders_injected == 0
        assert result.untracked_orders_on_exchange == 0
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_reconcile_startup_with_orders(self, reconciler, runner, mock_rest_client):
        """Test startup reconciliation injects all open orders."""
        mock_rest_client.get_open_orders.return_value = [
            {"orderId": "ex_1", "orderLinkId": "abc123def456789a",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "ex_2",
             "price": "51000", "qty": "0.001", "side": "Sell"},
        ]

        result = await reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 2
        assert result.orders_injected == 2

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 2

    @pytest.mark.asyncio
    async def test_reconcile_startup_no_longer_filters_by_order_link_id(
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

        result = await reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 3
        assert result.orders_injected == 3

    @pytest.mark.asyncio
    async def test_reconcile_startup_api_error(self, reconciler, runner, mock_rest_client):
        """Test startup reconciliation with API error."""
        mock_rest_client.get_open_orders.side_effect = Exception("API error")

        result = await reconciler.reconcile_startup(runner)

        assert result.orders_fetched == 0
        assert len(result.errors) == 1
        assert "API error" in result.errors[0]


class TestReconcilerReconnect:
    """Tests for reconnect reconciliation."""

    @pytest.mark.asyncio
    async def test_reconcile_reconnect_in_sync(self, reconciler, runner, mock_rest_client):
        """Test reconnect when state is in sync."""
        runner.inject_open_orders([
            {"orderId": "ex_1", "price": "49000", "qty": "0.001", "side": "Buy"},
        ])

        mock_rest_client.get_open_orders.return_value = [
            {"orderId": "ex_1"},
        ]

        result = await reconciler.reconcile_reconnect(runner)

        assert result.orders_fetched == 1
        assert result.untracked_orders_on_exchange == 0

    @pytest.mark.asyncio
    async def test_reconcile_reconnect_missing_on_exchange(self, reconciler, runner, mock_rest_client):
        """Test reconnect when order is in memory but not on exchange."""
        runner.inject_open_orders([
            {"orderId": "ex_1", "price": "49000", "qty": "0.001", "side": "Buy"},
        ])

        mock_rest_client.get_open_orders.return_value = []

        result = await reconciler.reconcile_reconnect(runner)

        assert result.orders_fetched == 0
        # Tracked by orderId
        assert runner._tracked_orders["ex_1"].status == "cancelled"

    @pytest.mark.asyncio
    async def test_reconcile_reconnect_missing_in_memory(self, reconciler, runner, mock_rest_client):
        """Test reconnect when order is on exchange but not in memory."""
        mock_rest_client.get_open_orders.return_value = [
            {"orderId": "ex_new", "price": "50000", "qty": "0.001", "side": "Sell"},
        ]

        result = await reconciler.reconcile_reconnect(runner)

        assert result.orders_fetched == 1
        assert result.untracked_orders_on_exchange == 1
        assert result.orders_injected == 1


class TestBuildLimitOrdersDict:
    """Tests for build_limit_orders_dict method."""

    def test_empty_list(self, reconciler):
        """Test with empty order list."""
        result = reconciler.build_limit_orders_dict([])
        assert result == {"long": [], "short": []}

    def test_buy_orders(self, reconciler):
        """Test buy order direction determination."""
        orders = [
            {"side": "Buy", "reduceOnly": False},  # Opening long
            {"side": "Buy", "reduceOnly": True},   # Closing short
        ]

        result = reconciler.build_limit_orders_dict(orders)

        assert len(result["long"]) == 1
        assert len(result["short"]) == 1

    def test_sell_orders(self, reconciler):
        """Test sell order direction determination."""
        orders = [
            {"side": "Sell", "reduceOnly": False},  # Opening short
            {"side": "Sell", "reduceOnly": True},   # Closing long
        ]

        result = reconciler.build_limit_orders_dict(orders)

        assert len(result["long"]) == 1
        assert len(result["short"]) == 1


