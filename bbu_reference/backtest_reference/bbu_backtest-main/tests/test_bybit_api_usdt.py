import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from src.bybit_api_usdt import BybitApiUsdt


# Create a proper mock HTTP class that accepts arguments
class MockHTTP:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_instruments_info(self, **kwargs):
        # Return a default structure that matches Bybit API response
        symbol = kwargs.get('symbol', 'BTCUSDT')
        return {
            'result': {
                'list': [
                    {
                        'symbol': symbol,
                        'priceFilter': {'tickSize': '0.5'},
                        'lotSizeFilter': {'qtyStep': '0.01'},
                    }
                ]
            }
        }


# Create a proper mock WebSocket class
class MockWebSocket:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def ticker_stream(self, symbol, callback):
        # Mock method for ticker streaming
        pass


pybit_module = types.ModuleType("pybit")
unified = types.ModuleType("unified_trading")
setattr(unified, "HTTP", MockHTTP)
setattr(unified, "WebSocket", MockWebSocket)
pybit_module.unified_trading = unified
sys.modules.setdefault("pybit", pybit_module)
sys.modules.setdefault("pybit.unified_trading", unified)


class TestBybitApiUsdt:

    @patch('src.bybit_api_usdt.HTTP')
    def test_read_ticksize_success(self, mock_http):
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            'result': {
                'list': [
                    {
                        'symbol': 'BTCUSDT',
                        'priceFilter': {'tickSize': '0.5'},
                        'lotSizeFilter': {'qtyStep': '0.01'},
                    }
                ]
            }
        }
        mock_http.return_value = mock_session

        BybitApiUsdt.ticksizes = {}
        api = BybitApiUsdt('key', 'secret', 1, None, 'name', None)
        api.read_ticksize('BTCUSDT')

        assert BybitApiUsdt.ticksizes['BTCUSDT'] == 0.5
        assert api.min_amount == 0.01

    @patch('src.bybit_api_usdt.HTTP')
    def test_read_ticksize_static_success(self, mock_http):
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            'result': {
                'list': [
                    {
                        'symbol': 'ETHUSDT',
                        'priceFilter': {'tickSize': '0.05'},
                    }
                ]
            }
        }
        mock_http.return_value = mock_session

        BybitApiUsdt.ticksizes = {}
        BybitApiUsdt.read_ticksize_static('ETHUSDT')

        assert BybitApiUsdt.ticksizes['ETHUSDT'] == 0.05

    @patch('src.bybit_api_usdt.HTTP')
    def test_read_ticksize_static_missing_symbol(self, mock_http):
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            'result': {'list': []}
        }
        mock_http.return_value = mock_session

        BybitApiUsdt.ticksizes = {}

        with pytest.raises(ValueError, match="Symbol 'DOGEUSDT' not found"):
            BybitApiUsdt.read_ticksize_static('DOGEUSDT')

    import sys
    import types

    pybit_module = types.ModuleType("pybit")
    unified = types.ModuleType("unified_trading")
    setattr(unified, "HTTP", object)
    pybit_module.unified_trading = unified
    sys.modules.setdefault("pybit", pybit_module)
    sys.modules.setdefault("pybit.unified_trading", unified)

    from src.bybit_api_usdt import BybitApiUsdt

    @patch('src.bybit_api_usdt.HTTP')
    def test_read_ticksize_missing_symbol(self, mock_http):
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            'result': {'list': []}
        }
        mock_http.return_value = mock_session

        BybitApiUsdt.ticksizes = {}
        api = BybitApiUsdt('key', 'secret', 1, None, 'name', None)

        with pytest.raises(ValueError, match="Symbol 'ABCUSDT' not found"):
            api.read_ticksize('ABCUSDT')

    @patch('src.bybit_api_usdt.HTTP')
    def test_read_ticksize_stat_missing_symbol(self, mock_http):
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            'result': {'list': []}
        }
        mock_http.return_value = mock_session

        BybitApiUsdt.ticksizes = {}

        with pytest.raises(ValueError, match="Symbol 'DOGEUSDT' not found"):
            BybitApiUsdt.read_ticksize_static('DOGEUSDT')
