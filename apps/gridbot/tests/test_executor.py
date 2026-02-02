"""Tests for gridbot executor module."""

from decimal import Decimal
from unittest.mock import Mock, MagicMock

import pytest

from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridbot.executor import IntentExecutor, OrderResult, CancelResult


@pytest.fixture
def mock_rest_client():
    """Create a mock REST client."""
    client = Mock()
    client.place_order = MagicMock(return_value={"orderId": "test_order_123"})
    client.cancel_order = MagicMock(return_value=True)
    return client


@pytest.fixture
def executor(mock_rest_client):
    """Create executor with mock client."""
    return IntentExecutor(mock_rest_client, shadow_mode=False)


@pytest.fixture
def shadow_executor(mock_rest_client):
    """Create executor in shadow mode."""
    return IntentExecutor(mock_rest_client, shadow_mode=True)


@pytest.fixture
def place_intent():
    """Sample PlaceLimitIntent."""
    return PlaceLimitIntent.create(
        symbol="BTCUSDT",
        side="Buy",
        price=Decimal("50000.0"),
        qty=Decimal("0.001"),
        grid_level=10,
        direction="long",
        reduce_only=False,
    )


@pytest.fixture
def cancel_intent():
    """Sample CancelIntent."""
    return CancelIntent(
        symbol="BTCUSDT",
        order_id="order_to_cancel_123",
        reason="side_mismatch",
    )


class TestOrderResult:
    """Tests for OrderResult dataclass."""

    def test_success_result(self):
        """Test successful order result."""
        result = OrderResult(success=True, order_id="123")
        assert result.success is True
        assert result.order_id == "123"
        assert result.error is None
        assert result.timestamp is not None

    def test_failure_result(self):
        """Test failed order result."""
        result = OrderResult(success=False, error="Rate limited")
        assert result.success is False
        assert result.order_id is None
        assert result.error == "Rate limited"


class TestCancelResult:
    """Tests for CancelResult dataclass."""

    def test_success_result(self):
        """Test successful cancel result."""
        result = CancelResult(success=True)
        assert result.success is True
        assert result.error is None
        assert result.timestamp is not None

    def test_failure_result(self):
        """Test failed cancel result."""
        result = CancelResult(success=False, error="Order not found")
        assert result.success is False
        assert result.error == "Order not found"


class TestExecutorPlaceOrder:
    """Tests for execute_place method."""

    def test_place_order_success(self, executor, mock_rest_client, place_intent):
        """Test successful order placement."""
        result = executor.execute_place(place_intent)

        assert result.success is True
        assert result.order_id == "test_order_123"

        mock_rest_client.place_order.assert_called_once_with(
            symbol="BTCUSDT",
            side="Buy",
            order_type="Limit",
            qty="0.001",
            price="50000.0",
            reduce_only=False,
            position_idx=1,  # long direction
            order_link_id=place_intent.client_order_id,
        )

    def test_place_order_short_direction(self, executor, mock_rest_client):
        """Test order placement with short direction uses correct position_idx."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("50000.0"),
            qty=Decimal("0.001"),
            grid_level=10,
            direction="short",
            reduce_only=False,
        )

        result = executor.execute_place(intent)

        assert result.success is True
        assert result.order_id == "test_order_123"

        mock_rest_client.place_order.assert_called_once_with(
            symbol="BTCUSDT",
            side="Sell",
            order_type="Limit",
            qty="0.001",
            price="50000.0",
            reduce_only=False,
            position_idx=2,  # short direction
            order_link_id=intent.client_order_id,
        )

    def test_place_order_failure(self, executor, mock_rest_client, place_intent):
        """Test order placement failure."""
        mock_rest_client.place_order.side_effect = Exception("API error")

        result = executor.execute_place(place_intent)

        assert result.success is False
        assert "API error" in result.error

    def test_place_order_shadow_mode(self, shadow_executor, mock_rest_client, place_intent):
        """Test order placement in shadow mode."""
        result = shadow_executor.execute_place(place_intent)

        assert result.success is True
        assert result.order_id.startswith("shadow_")
        mock_rest_client.place_order.assert_not_called()


class TestExecutorCancelOrder:
    """Tests for execute_cancel method."""

    def test_cancel_order_success(self, executor, mock_rest_client, cancel_intent):
        """Test successful order cancellation."""
        result = executor.execute_cancel(cancel_intent)

        assert result.success is True
        assert result.error is None

        mock_rest_client.cancel_order.assert_called_once_with(
            symbol="BTCUSDT",
            order_id="order_to_cancel_123",
        )

    def test_cancel_order_not_found(self, executor, mock_rest_client, cancel_intent):
        """Test cancellation when order not found (returns False)."""
        mock_rest_client.cancel_order.return_value = False

        result = executor.execute_cancel(cancel_intent)

        assert result.success is False

    def test_cancel_order_failure(self, executor, mock_rest_client, cancel_intent):
        """Test cancellation failure."""
        mock_rest_client.cancel_order.side_effect = Exception("Network error")

        result = executor.execute_cancel(cancel_intent)

        assert result.success is False
        assert "Network error" in result.error

    def test_cancel_order_shadow_mode(self, shadow_executor, mock_rest_client, cancel_intent):
        """Test order cancellation in shadow mode."""
        result = shadow_executor.execute_cancel(cancel_intent)

        assert result.success is True
        mock_rest_client.cancel_order.assert_not_called()


class TestExecutorBatch:
    """Tests for execute_batch method."""

    def test_batch_execution(self, executor, mock_rest_client, place_intent, cancel_intent):
        """Test batch execution of multiple intents."""
        intents = [place_intent, cancel_intent]

        results = executor.execute_batch(intents)

        assert len(results) == 2
        assert isinstance(results[0], OrderResult)
        assert isinstance(results[1], CancelResult)
        assert results[0].success is True
        assert results[1].success is True

    def test_batch_partial_failure(self, executor, mock_rest_client, place_intent, cancel_intent):
        """Test batch with partial failures."""
        mock_rest_client.place_order.side_effect = Exception("Rate limited")
        mock_rest_client.cancel_order.return_value = True

        intents = [place_intent, cancel_intent]
        results = executor.execute_batch(intents)

        assert len(results) == 2
        assert results[0].success is False
        assert results[1].success is True

    def test_batch_empty_list(self, executor):
        """Test batch with empty list."""
        results = executor.execute_batch([])
        assert results == []


class TestPositionIndex:
    """Tests for position index calculation."""

    def test_long_position_idx(self, executor):
        """Test position index for long direction."""
        assert executor._get_position_idx("long") == 1

    def test_short_position_idx(self, executor):
        """Test position index for short direction."""
        assert executor._get_position_idx("short") == 2

    def test_unknown_position_idx(self, executor):
        """Test position index for unknown direction."""
        assert executor._get_position_idx("unknown") == 0

    def test_custom_position_idx(self, mock_rest_client):
        """Test custom position indices."""
        executor = IntentExecutor(
            mock_rest_client,
            position_idx_long=10,
            position_idx_short=20,
        )
        assert executor._get_position_idx("long") == 10
        assert executor._get_position_idx("short") == 20
