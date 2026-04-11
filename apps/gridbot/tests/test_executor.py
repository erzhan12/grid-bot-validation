"""Tests for gridbot executor module."""

from decimal import Decimal
from unittest.mock import Mock, MagicMock

import pytest

from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridbot.executor import IntentExecutor, OrderResult, CancelResult, AUTH_ERROR_CODES


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


class TestAuthErrorDetection:
    """Tests for auth error detection and cooldown."""

    def test_is_auth_error_permission_denied(self):
        """Test detection of permission denied error."""
        error = "Bybit API error in place_order: [10005] Permission denied"
        assert IntentExecutor._is_auth_error(error) is True

    def test_is_auth_error_invalid_key(self):
        """Test detection of invalid API key error."""
        assert IntentExecutor._is_auth_error("[10003] Invalid API key") is True

    def test_is_auth_error_sign_error(self):
        """Test detection of signature error."""
        assert IntentExecutor._is_auth_error("[10004] Sign error") is True

    def test_is_auth_error_key_expired(self):
        """Test detection of API key expired error."""
        assert IntentExecutor._is_auth_error("[33004] API key expired") is True

    def test_is_auth_error_non_auth(self):
        """Test non-auth errors are not flagged."""
        assert IntentExecutor._is_auth_error("[110001] Order not found") is False

    def test_is_auth_error_no_code(self):
        """Test error strings without error codes."""
        assert IntentExecutor._is_auth_error("Connection timeout") is False

    def test_all_auth_codes_covered(self):
        """Verify all AUTH_ERROR_CODES are detected."""
        for code in AUTH_ERROR_CODES:
            assert IntentExecutor._is_auth_error(f"[{code}] some error") is True

    def test_is_auth_error_pybit_errcode_format(self):
        """Test detection of pybit (ErrCode: NNNNN) format."""
        error = (
            "Permission denied, please check your API key permissions. "
            "(ErrCode: 10005) (ErrTime: 14:13:32)."
        )
        assert IntentExecutor._is_auth_error(error) is True

    def test_is_auth_error_pybit_non_auth(self):
        """Test non-auth errors in pybit format are not flagged."""
        error = "Order not found. (ErrCode: 110001) (ErrTime: 14:13:32)."
        assert IntentExecutor._is_auth_error(error) is False

    def test_all_auth_codes_covered_pybit_format(self):
        """Verify all AUTH_ERROR_CODES are detected in pybit format."""
        for code in AUTH_ERROR_CODES:
            assert IntentExecutor._is_auth_error(f"Some error. (ErrCode: {code})") is True

    def test_cooldown_after_consecutive_auth_failures(self, mock_rest_client, place_intent):
        """Test cooldown activates after max_auth_failures consecutive auth errors."""
        mock_rest_client.place_order.side_effect = Exception(
            "Bybit API error in place_order: [10005] Permission denied"
        )
        executor = IntentExecutor(mock_rest_client, max_auth_failures=3)

        for i in range(2):
            result = executor.execute_place(place_intent)
            assert result.success is False
            assert executor.auth_cooldown is False
            assert executor.auth_failure_count == i + 1

        # Third failure triggers cooldown
        result = executor.execute_place(place_intent)
        assert result.success is False
        assert executor.auth_cooldown is True
        assert executor.auth_failure_count == 3

    def test_non_auth_error_resets_counter(self, mock_rest_client, place_intent):
        """Test non-auth error resets the consecutive auth failure counter."""
        executor = IntentExecutor(mock_rest_client, max_auth_failures=5)

        # Two auth errors
        mock_rest_client.place_order.side_effect = Exception("[10005] Permission denied")
        executor.execute_place(place_intent)
        executor.execute_place(place_intent)
        assert executor.auth_failure_count == 2

        # One non-auth error resets
        mock_rest_client.place_order.side_effect = Exception("[110001] Order not found")
        executor.execute_place(place_intent)
        assert executor.auth_failure_count == 0

    def test_success_resets_counter(self, mock_rest_client, place_intent):
        """Test successful call resets the auth failure counter."""
        executor = IntentExecutor(mock_rest_client, max_auth_failures=5)

        # Auth errors
        mock_rest_client.place_order.side_effect = Exception("[10005] Permission denied")
        executor.execute_place(place_intent)
        executor.execute_place(place_intent)
        assert executor.auth_failure_count == 2

        # Success resets
        mock_rest_client.place_order.side_effect = None
        mock_rest_client.place_order.return_value = {"orderId": "123"}
        executor.execute_place(place_intent)
        assert executor.auth_failure_count == 0

    def test_cancel_auth_error_counts(self, mock_rest_client, cancel_intent):
        """Test auth errors on cancel also count toward cooldown."""
        mock_rest_client.cancel_order.side_effect = Exception("[10005] Permission denied")
        executor = IntentExecutor(mock_rest_client, max_auth_failures=2)

        executor.execute_cancel(cancel_intent)
        assert executor.auth_failure_count == 1

        executor.execute_cancel(cancel_intent)
        assert executor.auth_cooldown is True

    def test_reset_auth_cooldown(self, mock_rest_client):
        """Test reset_auth_cooldown clears state."""
        executor = IntentExecutor(mock_rest_client, max_auth_failures=1)
        executor._auth_failure_count = 5
        executor._auth_cooldown = True

        executor.reset_auth_cooldown()

        assert executor.auth_failure_count == 0
        assert executor.auth_cooldown is False

    def test_on_cooldown_entered_callback(self, mock_rest_client, place_intent):
        """Test callback fires when cooldown activates."""
        callback = Mock()
        mock_rest_client.place_order.side_effect = Exception("[10005] Permission denied")
        executor = IntentExecutor(
            mock_rest_client, max_auth_failures=2, on_cooldown_entered=callback,
        )

        executor.execute_place(place_intent)
        callback.assert_not_called()

        executor.execute_place(place_intent)
        callback.assert_called_once()

    def test_callback_fires_only_once_per_cooldown(self, mock_rest_client, place_intent):
        """Test callback doesn't fire again on subsequent auth errors during same cooldown."""
        callback = Mock()
        mock_rest_client.place_order.side_effect = Exception("[10005] Permission denied")
        executor = IntentExecutor(
            mock_rest_client, max_auth_failures=2, on_cooldown_entered=callback,
        )

        # Trigger cooldown
        executor.execute_place(place_intent)
        executor.execute_place(place_intent)
        assert callback.call_count == 1

        # More failures during cooldown — callback should not fire again
        executor.execute_place(place_intent)
        assert callback.call_count == 1
