from unittest.mock import Mock, patch

import pytest

from src.data_provider import DataProvider
from src.enums import DateConstants


def setup_db_mock(mock_data=None):
    """Helper function to setup database mock with nested context managers"""
    if mock_data is None:
        mock_data = []

    # Create mock cursor that supports DictCursor behavior and context manager protocol
    mock_cursor = Mock()
    mock_cursor.fetchall.return_value = mock_data

    # Make mock cursor work as a context manager
    mock_cursor.__enter__ = Mock(return_value=mock_cursor)
    mock_cursor.__exit__ = Mock(return_value=None)

    # Mock the DictCursor factory function behavior
    def mock_cursor_factory(cursor_factory=None, **kwargs):
        if cursor_factory and hasattr(cursor_factory, '__name__') and cursor_factory.__name__ == 'DictCursor':
            # Return cursor that behaves like DictCursor (returns dict-like rows)
            return mock_cursor
        return mock_cursor

    # Create mock connection
    mock_conn = Mock()
    mock_conn.cursor = mock_cursor_factory

    return mock_conn, mock_cursor


class TestDataProvider:
    """Test suite for DataProvider class"""

    def test_initialization(self):
        """Test DataProvider initialization with default and custom page_size"""
        # Test default initialization
        provider = DataProvider()
        assert provider.page_size == 100
        assert provider.ticker_data_table == []
        assert provider.last_date is None
        assert provider.last_id is None
        assert provider.start_datetime is None

        # Test custom page_size
        provider = DataProvider(page_size=500)
        assert provider.page_size == 500

        # Test initialization with start_datetime
        start_dt = "2025-09-19 00:00:00"
        provider = DataProvider(start_datetime=start_dt)
        assert provider.start_datetime == start_dt
        assert provider.page_size == 100

    @patch('psycopg2.connect')
    def test_get_next_batch_first_query(self, mock_connect):
        """Test get_next_batch for first query (last_id is None)"""
        # Setup mock
        mock_data = [
            {'id': 1, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:00:00', 'last_price': 50000.0},
            {'id': 2, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:01:00', 'last_price': 50100.0},
        ]
        mock_conn, mock_cursor = setup_db_mock(mock_data)
        mock_connect.return_value = mock_conn

        # Test
        provider = DataProvider(page_size=2)
        result = provider.get_next_batch('BTCUSDT', 1)

        # Assertions
        assert result == mock_data
        assert provider.last_id == 2
        assert provider.ticker_data_table == mock_data

        # Verify SQL query was called correctly
        mock_conn.cursor().execute.assert_called_once()
        call_args = mock_conn.cursor().execute.call_args
        assert 'SELECT * FROM ticker_data' in call_args[0][0]
        assert 'WHERE symbol = %(symbol)s' in call_args[0][0]
        assert 'AND id >= %(param_id)s' in call_args[0][0]
        assert 'ORDER BY timestamp ASC' in call_args[0][0]
        assert 'LIMIT %(limit)s' in call_args[0][0]

    @patch('psycopg2.connect')
    def test_get_next_batch_subsequent_query(self, mock_connect):
        """Test get_next_batch for subsequent queries (last_id is not None)"""
        # Setup mock
        mock_data = [
            {'id': 3, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:02:00', 'last_price': 50200.0},
            {'id': 4, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:03:00', 'last_price': 50300.0},
        ]
        mock_conn, mock_cursor = setup_db_mock(mock_data)
        mock_connect.return_value = mock_conn

        # Test
        provider = DataProvider(page_size=2)
        provider.last_id = 2  # Set last_id to simulate subsequent query
        result = provider.get_next_batch('BTCUSDT', 1)

        # Assertions
        assert result == mock_data
        assert provider.last_id == 4

        # Verify SQL query was called correctly
        mock_conn.cursor().execute.assert_called_once()
        call_args = mock_conn.cursor().execute.call_args
        assert 'AND id >= %(param_id)s' in call_args[0][0]

    @patch('psycopg2.connect')
    def test_get_next_batch_empty_result(self, mock_connect):
        """Test get_next_batch when no data is returned"""
        # Setup mock
        mock_conn, mock_cursor = setup_db_mock([])
        mock_connect.return_value = mock_conn

        # Test
        provider = DataProvider()
        result = provider.get_next_batch('BTCUSDT', 1)

        # Assertions
        assert result == []
        assert provider.last_id is None  # Should not be updated when no data

    @patch('psycopg2.connect')
    def test_get_next_batch_database_error(self, mock_connect):
        """Test get_next_batch when database raises an exception"""
        # Create a simple mock that raises exception when cursor() is called
        mock_conn = Mock()
        mock_conn.cursor.side_effect = Exception("Database connection failed")
        mock_connect.return_value = mock_conn

        # Test
        provider = DataProvider()

        with pytest.raises(Exception, match="Database connection failed"):
            provider.get_next_batch('BTCUSDT', 1)

    @patch.object(DataProvider, 'get_next_batch')
    def test_iterate_all_single_batch(self, mock_get_next_batch):
        """Test iterate_all with single batch of data"""
        # Mock data
        mock_data = [
            {'id': 1, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:00:00', 'last_price': 50000.0},
            {'id': 2, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:01:00', 'last_price': 50100.0},
        ]
        
        # Setup mock to return data once, then empty list
        mock_get_next_batch.side_effect = [mock_data, []]

        # Test
        provider = DataProvider()
        result = list(provider.iterate_all('BTCUSDT', 1))

        # Assertions
        assert result == mock_data
        assert mock_get_next_batch.call_count == 2

    @patch.object(DataProvider, 'get_next_batch')
    def test_iterate_all_multiple_batches(self, mock_get_next_batch):
        """Test iterate_all with multiple batches of data"""
        # Mock data for multiple batches
        batch1 = [
            {'id': 1, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:00:00', 'last_price': 50000.0},
            {'id': 2, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:01:00', 'last_price': 50100.0},
        ]
        batch2 = [
            {'id': 3, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:02:00', 'last_price': 50200.0},
            {'id': 4, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:03:00', 'last_price': 50300.0},
        ]
        
        # Setup mock to return data twice, then empty list
        mock_get_next_batch.side_effect = [batch1, batch2, []]

        # Test
        provider = DataProvider()
        result = list(provider.iterate_all('BTCUSDT', 1))

        # Assertions
        expected_result = batch1 + batch2
        assert result == expected_result
        assert mock_get_next_batch.call_count == 3

    @patch.object(DataProvider, 'get_next_batch')
    def test_iterate_all_empty_data(self, mock_get_next_batch):
        """Test iterate_all when no data is available"""
        # Setup mock to return empty list immediately
        mock_get_next_batch.return_value = []

        # Test
        provider = DataProvider()
        result = list(provider.iterate_all('BTCUSDT', 1))

        # Assertions
        assert result == []
        assert mock_get_next_batch.call_count == 1

    @patch.object(DataProvider, 'get_next_batch')
    def test_iterate_all_yields_individual_items(self, mock_get_next_batch):
        """Test that iterate_all yields individual items, not batches"""
        # Mock data
        mock_data = [
            {'id': 1, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:00:00', 'last_price': 50000.0},
            {'id': 2, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:01:00', 'last_price': 50100.0},
        ]
        
        mock_get_next_batch.side_effect = [mock_data, []]

        # Test
        provider = DataProvider()
        items = []
        for item in provider.iterate_all('BTCUSDT', 1):
            items.append(item)

        # Assertions
        assert len(items) == 2
        assert items[0]['id'] == 1
        assert items[1]['id'] == 2

    def test_date_constants(self):
        """Test DateConstants enum values"""
        assert DateConstants.MIN_DATE.value == '1970-01-01 00:00:00'
        assert DateConstants.MAX_DATE.value == '2100-01-01 00:00:00'

    @patch('psycopg2.connect')
    def test_get_next_batch_parameters_passed_correctly(self, mock_connect):
        """Test that parameters are passed correctly to database query"""
        # Setup mock
        mock_conn, mock_cursor = setup_db_mock([])
        mock_connect.return_value = mock_conn

        # Test
        provider = DataProvider(page_size=100)
        provider.get_next_batch('ETHUSDT', 50)

        # Verify parameters were passed correctly
        mock_conn.cursor().execute.assert_called_once()
        call_args = mock_conn.cursor().execute.call_args
        params = call_args[0][1]

        assert params['symbol'] == 'ETHUSDT'
        assert params['param_id'] == 50  # First query uses start_id
        assert params['limit'] == 100

    @patch('psycopg2.connect')
    def test_get_next_batch_subsequent_query_parameters(self, mock_connect):
        """Test parameters for subsequent queries"""
        # Setup mock
        mock_conn, mock_cursor = setup_db_mock([])
        mock_connect.return_value = mock_conn

        # Test
        provider = DataProvider(page_size=50)
        provider.last_id = 100
        provider.get_next_batch('BTCUSDT', 1)

        # Verify parameters were passed correctly
        mock_conn.cursor().execute.assert_called_once()
        call_args = mock_conn.cursor().execute.call_args
        params = call_args[0][1]

        assert params['symbol'] == 'BTCUSDT'
        assert params['param_id'] == 101  # last_id + 1 = 101
        assert params['limit'] == 50

    @patch('psycopg2.connect')
    def test_get_next_batch_with_start_datetime(self, mock_connect):
        """Test get_next_batch with start_datetime filter"""
        # Setup mock
        mock_conn, mock_cursor = setup_db_mock([])
        mock_connect.return_value = mock_conn

        # Test with start_datetime
        start_dt = "2025-09-19 00:00:00"
        provider = DataProvider(page_size=100, start_datetime=start_dt)
        provider.get_next_batch('BTCUSDT', 1)

        # Verify SQL query includes timestamp filter
        mock_conn.cursor().execute.assert_called_once()
        call_args = mock_conn.cursor().execute.call_args
        query = call_args[0][0]
        params = call_args[0][1]

        assert 'AND timestamp >= %(start_datetime)s' in query
        assert params['start_datetime'] == start_dt
        assert params['symbol'] == 'BTCUSDT'
        assert params['param_id'] == 1
        assert params['limit'] == 100

    @patch('psycopg2.connect')
    def test_get_next_batch_without_start_datetime(self, mock_connect):
        """Test get_next_batch without start_datetime filter"""
        # Setup mock
        mock_conn, mock_cursor = setup_db_mock([])
        mock_connect.return_value = mock_conn

        # Test without start_datetime
        provider = DataProvider(page_size=100)
        provider.get_next_batch('BTCUSDT', 1)

        # Verify SQL query does NOT include timestamp filter
        mock_conn.cursor().execute.assert_called_once()
        call_args = mock_conn.cursor().execute.call_args
        query = call_args[0][0]
        params = call_args[0][1]

        assert 'AND timestamp >= %(start_datetime)s' not in query
        assert 'start_datetime' not in params
        assert params['symbol'] == 'BTCUSDT'
        assert params['param_id'] == 1
        assert params['limit'] == 100

    @patch('psycopg2.connect')
    def test_has_more_data_functionality(self, mock_connect):
        """Test has_more_data method"""
        # Setup mock
        mock_conn, mock_cursor = setup_db_mock([])
        mock_connect.return_value = mock_conn

        provider = DataProvider()

        # Initial state - should have more data
        assert provider.has_more_data('BTCUSDT')

        # After getting empty batch, should be exhausted
        provider.get_next_batch('BTCUSDT', 1)
        assert not provider.has_more_data('BTCUSDT')

    @patch('psycopg2.connect')
    def test_data_exhaustion_tracking(self, mock_connect):
        """Test data exhaustion tracking with partial batches"""
        # Setup mock for partial batch (less than page_size)
        partial_data = [
            {'id': 1, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:00:00', 'last_price': 50000.0},
        ]
        mock_conn, mock_cursor = setup_db_mock(partial_data)
        mock_connect.return_value = mock_conn

        provider = DataProvider(page_size=10)  # Request 10, but only get 1

        # Should have data initially
        assert provider.has_more_data('BTCUSDT')

        # Get partial batch - should mark as exhausted
        result = provider.get_next_batch('BTCUSDT', 1)
        assert len(result) == 1
        assert not provider.has_more_data('BTCUSDT')  # Exhausted due to partial batch


class TestDataProviderIntegration:
    """Integration tests for DataProvider with actual database operations"""

    @pytest.fixture
    def sample_data(self):
        """Sample ticker data for testing"""
        return [
            {'id': 1, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:00:00', 'last_price': 50000.0},
            {'id': 2, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:01:00', 'last_price': 50100.0},
            {'id': 3, 'symbol': 'BTCUSDT', 'timestamp': '2023-01-01 00:02:00', 'last_price': 50200.0},
            {'id': 4, 'symbol': 'ETHUSDT', 'timestamp': '2023-01-01 00:00:00', 'last_price': 3000.0},
        ]

    @patch('psycopg2.connect')
    def test_full_iteration_cycle(self, mock_connect, sample_data):
        """Test complete iteration cycle with multiple batches"""
        # Setup mock to simulate pagination with side_effect
        mock_cursor = Mock()
        mock_cursor.fetchall.side_effect = [
            sample_data[:2],  # First 2 items
            sample_data[2:3],  # Next 1 item
            []  # No more data
        ]

        # Make mock cursor work as a context manager
        mock_cursor.__enter__ = Mock(return_value=mock_cursor)
        mock_cursor.__exit__ = Mock(return_value=None)

        # Mock the cursor factory function behavior
        def mock_cursor_factory(cursor_factory=None, **kwargs):
            return mock_cursor

        mock_conn = Mock()
        mock_conn.cursor = mock_cursor_factory
        mock_connect.return_value = mock_conn

        # Test
        provider = DataProvider(page_size=2)
        result = list(provider.iterate_all('BTCUSDT', 1))

        # Assertions
        expected_data = [item for item in sample_data if item['symbol'] == 'BTCUSDT']
        assert result == expected_data
        assert len(result) == 3
        # Only 2 execute calls needed: first batch (2 items), second batch (1 item, marks exhausted)
        assert mock_cursor.execute.call_count == 2
