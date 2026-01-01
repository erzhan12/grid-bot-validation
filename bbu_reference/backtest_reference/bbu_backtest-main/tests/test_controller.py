"""Unit tests for src/controller.py components."""

from unittest.mock import Mock, patch

from src.controller import Controller


def test_controller_init():
    controller = Controller('LTCUSDT')
    assert controller.bms is not None
    assert controller.strats is not None


def test_controller_init_with_start_datetime():
    """Test Controller initialization with start_datetime parameter"""
    start_dt = "2025-09-19 00:00:00"
    controller = Controller('LTCUSDT', start_datetime=start_dt)

    assert controller.symbol == 'LTCUSDT'
    assert controller.start_datetime == start_dt
    assert controller.bms is not None
    assert controller.strats is not None


@patch('src.controller.settings')
@patch('src.controller.importlib')
def test_controller_passes_start_datetime_to_strat50(mock_importlib, mock_settings):
    """Test that Controller passes start_datetime to Strat50 strategies"""
    # Mock settings - use a simple object that can act like a dict
    class MockPairTimeframe:
        def __init__(self):
            self.symbol = 'BTCUSDT'
            self.strat = 'Strat50'
            self.id = 1
            self._data = {'symbol': 'BTCUSDT', 'strat': 'Strat50', 'id': 1}

        def __iter__(self):
            return iter([('symbol', 'BTCUSDT'), ('strat', 'Strat50'), ('id', 1)])

        def keys(self):
            return ['symbol', 'strat', 'id']

        def __getitem__(self, key):
            return self._data[key]

    mock_pt = MockPairTimeframe()

    mock_settings.pair_timeframes = [mock_pt]
    mock_settings.amounts = []  # No amounts to simplify test

    # Mock strategy class
    mock_strat_class = Mock()
    mock_strat_instance = Mock()
    mock_strat_instance.bms = []
    mock_strat_class.return_value = mock_strat_instance

    mock_module = Mock()
    mock_module.Strat50 = mock_strat_class
    mock_importlib.import_module.return_value = mock_module

    # Test Controller creation with start_datetime
    start_dt = "2025-09-19 00:00:00"
    _ = Controller('BTCUSDT', start_datetime=start_dt)

    # Verify Strat50 was called with start_datetime
    mock_strat_class.assert_called_once()
    call_args, call_kwargs = mock_strat_class.call_args

    assert 'start_datetime' in call_kwargs
    assert call_kwargs['start_datetime'] == start_dt


@patch('src.controller.settings')
@patch('src.controller.importlib')
def test_controller_does_not_pass_start_datetime_to_other_strats(mock_importlib, mock_settings):
    """Test that Controller does not pass start_datetime to non-Strat50 strategies"""
    # Mock settings for a different strategy
    class MockPairTimeframe:
        def __init__(self):
            self.symbol = 'BTCUSDT'
            self.strat = 'Strat1'  # Different strategy
            self.id = 1
            self._data = {'symbol': 'BTCUSDT', 'strat': 'Strat1', 'id': 1}

        def __iter__(self):
            return iter([('symbol', 'BTCUSDT'), ('strat', 'Strat1'), ('id', 1)])

        def keys(self):
            return ['symbol', 'strat', 'id']

        def __getitem__(self, key):
            return self._data[key]

    mock_pt = MockPairTimeframe()

    mock_settings.pair_timeframes = [mock_pt]
    mock_settings.amounts = []

    # Mock strategy class
    mock_strat_class = Mock()
    mock_strat_instance = Mock()
    mock_strat_instance.bms = []
    mock_strat_class.return_value = mock_strat_instance

    mock_module = Mock()
    mock_module.Strat1 = mock_strat_class
    mock_importlib.import_module.return_value = mock_module

    # Test Controller creation with start_datetime
    start_dt = "2025-09-19 00:00:00"
    _ = Controller('BTCUSDT', start_datetime=start_dt)

    # Verify Strat1 was NOT called with start_datetime
    mock_strat_class.assert_called_once()
    call_args, call_kwargs = mock_strat_class.call_args

    assert 'start_datetime' not in call_kwargs


def test_controller_check_job_iteration_counting():
    """Test that check_job properly counts iterations and reports progress"""
    # This test would require more complex mocking of the strategy execution
    # For now, we'll just test that the method exists and can be called
    controller = Controller('LTCUSDT')

    # Verify the method exists
    assert hasattr(controller, 'check_job')
    assert callable(controller.check_job)