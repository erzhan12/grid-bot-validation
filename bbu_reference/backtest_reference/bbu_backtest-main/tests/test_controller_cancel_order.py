"""Unit tests for Controller.cancel_order method."""

from datetime import datetime
from unittest.mock import Mock

from src.controller import Controller


def test_controller_cancel_order_success():
    """Test that cancel_order successfully delegates to BacktestOrderManager"""
    # Create a mock market maker
    mock_bm = Mock()
    mock_bm.strat = 1
    mock_bm.current_timestamp = datetime(2025, 1, 1, 12, 0, 0)

    # Create a mock backtest order manager
    mock_order_manager = Mock()
    mock_order_manager.cancel_order.return_value = True
    mock_bm.backtest_order_manager = mock_order_manager

    # Create controller instance and inject mock bm
    controller = Mock(spec=Controller)
    controller.bms = [mock_bm]

    # Use the real cancel_order method
    result = Controller.cancel_order(controller, strat_id=1, symbol='LTCUSDT', order_id='ORDER_000001')

    # Verify the method was called correctly
    mock_order_manager.cancel_order.assert_called_once_with('ORDER_000001', mock_bm.current_timestamp)
    assert result is True


def test_controller_cancel_order_no_backtest_manager():
    """Test that cancel_order returns False when backtest manager is not initialized"""
    # Create a mock market maker without backtest_order_manager
    mock_bm = Mock()
    mock_bm.strat = 1
    mock_bm.backtest_order_manager = None

    # Create controller instance and inject mock bm
    controller = Mock(spec=Controller)
    controller.bms = [mock_bm]

    # Use the real cancel_order method
    result = Controller.cancel_order(controller, strat_id=1, symbol='LTCUSDT', order_id='ORDER_000001')

    # Should return False when backtest_order_manager is None
    assert result is False


def test_controller_cancel_order_invalid_strat_id():
    """Test that cancel_order returns False for non-existent strategy"""
    # Create a mock market maker with strat_id 1
    mock_bm = Mock()
    mock_bm.strat = 1

    # Create controller instance and inject mock bm
    controller = Mock(spec=Controller)
    controller.bms = [mock_bm]

    # Use the real cancel_order method with different strat_id
    result = Controller.cancel_order(controller, strat_id=99999, symbol='LTCUSDT', order_id='ORDER_000001')

    # Should return False for non-matching strat_id
    assert result is False


def test_controller_cancel_order_delegates_to_manager():
    """Test that cancel_order correctly delegates with timestamp"""
    # Create mock market maker with specific strat_id
    mock_bm = Mock()
    mock_bm.strat = 42
    test_timestamp = datetime(2025, 10, 10, 15, 30, 45)
    mock_bm.current_timestamp = test_timestamp

    # Create mock backtest order manager
    mock_order_manager = Mock()
    mock_order_manager.cancel_order.return_value = False  # Order not found
    mock_bm.backtest_order_manager = mock_order_manager

    # Create controller with mock
    controller = Mock(spec=Controller)
    controller.bms = [mock_bm]

    # Call cancel_order
    result = Controller.cancel_order(controller, strat_id=42, symbol='BTCUSDT', order_id='ORDER_123456')

    # Verify it passed the correct timestamp and order_id
    mock_order_manager.cancel_order.assert_called_once_with('ORDER_123456', test_timestamp)
    assert result is False


def test_controller_cancel_order_multiple_bms():
    """Test that cancel_order finds the correct bm among multiple market makers"""
    # Create multiple mock market makers
    mock_bm1 = Mock()
    mock_bm1.strat = 1
    mock_bm1.backtest_order_manager = Mock()

    mock_bm2 = Mock()
    mock_bm2.strat = 2
    mock_bm2.current_timestamp = datetime(2025, 1, 1)
    mock_order_manager2 = Mock()
    mock_order_manager2.cancel_order.return_value = True
    mock_bm2.backtest_order_manager = mock_order_manager2

    mock_bm3 = Mock()
    mock_bm3.strat = 3
    mock_bm3.backtest_order_manager = Mock()

    # Create controller with multiple bms
    controller = Mock(spec=Controller)
    controller.bms = [mock_bm1, mock_bm2, mock_bm3]

    # Call cancel_order for strat_id=2 (should use mock_bm2)
    result = Controller.cancel_order(controller, strat_id=2, symbol='ETHUSDT', order_id='ORDER_999')

    # Verify only mock_bm2's order manager was called
    mock_order_manager2.cancel_order.assert_called_once()
    mock_bm1.backtest_order_manager.cancel_order.assert_not_called()
    mock_bm3.backtest_order_manager.cancel_order.assert_not_called()
    assert result is True
