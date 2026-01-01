from typing import Optional

import psycopg2
from psycopg2.extras import DictCursor

# from db.database import get_db, engine
from config.settings import DatabaseSettings

# Load settings once
_settings = DatabaseSettings()


class DataProvider:
    def __init__(self, page_size=100, start_datetime: Optional[str] = None):
        self.ticker_data_table = []
        self.page_size = page_size
        self.last_date = None
        self.last_id = None
        self.start_datetime = start_datetime
        self.conn = psycopg2.connect(_settings.database_url)

        # Track data availability per symbol
        self._data_exhausted = {}
        self._total_records_cache = {}
    
    def get_next_batch(self, symbol, start_id):
        """Get next batch using cursor-based pagination"""
        # Check if data is already exhausted for this symbol
        if self._data_exhausted.get(symbol, False):
            return []

        query = """
        SELECT * FROM ticker_data
        WHERE symbol = %(symbol)s
        AND id >= %(param_id)s
        """

        # Add start_datetime filter if specified
        if self.start_datetime:
            query += " AND timestamp >= %(start_datetime)s"

        query += """
        ORDER BY timestamp ASC
        LIMIT %(limit)s
        """

        # Get the next ID to query for
        param_id = self.last_id + 1 if self.last_id is not None else start_id
        params = {
            'symbol': symbol,
            'param_id': param_id,
            'limit': self.page_size
        }

        if self.start_datetime:
            params['start_datetime'] = self.start_datetime

        if self.conn is None:
            return []

        with self.conn.cursor(cursor_factory=DictCursor) as curs:
            curs.execute(query, params)
            self.ticker_data_table = curs.fetchall()

            # Mark as exhausted if we got fewer records than requested
            if len(self.ticker_data_table) < self.page_size:
                self._data_exhausted[symbol] = True

            if self.ticker_data_table:
                self.last_id = self.ticker_data_table[-1]['id']
            else:
                # No more data available
                self._data_exhausted[symbol] = True

            return self.ticker_data_table
  
    def iterate_all(self, symbol, start_id):
        """Generator using cursor-based pagination"""
        # Reset pagination state for each new iteration cycle
        self.last_id = None
        self._data_exhausted[symbol] = False

        while True:
            batch = self.get_next_batch(symbol, start_id)
            if not batch:
                break
            for item in batch:
                yield item

    def has_more_data(self, symbol):
        """Check if there's more data available for the given symbol"""
        return not self._data_exhausted.get(symbol, False)

    def get_data_range_info(self, symbol):
        """Get information about available data range for the symbol"""
        if self.conn is None:
            return None

        query = """
        SELECT
            MIN(timestamp) as start_time,
            MAX(timestamp) as end_time,
            COUNT(*) as total_records
        FROM ticker_data
        WHERE symbol = %(symbol)s
        """

        params = {'symbol': symbol}

        # Add start_datetime filter if specified
        if self.start_datetime:
            query += " AND timestamp >= %(start_datetime)s"
            params['start_datetime'] = self.start_datetime

        with self.conn.cursor(cursor_factory=DictCursor) as curs:
            curs.execute(query, params)
            result = curs.fetchone()
            if result:
                self._total_records_cache[symbol] = result['total_records']
                return {
                    'start_time': result['start_time'],
                    'end_time': result['end_time'],
                    'total_records': result['total_records']
                }
        return None

    def reset_for_symbol(self, symbol):
        """Reset pagination state for a specific symbol"""
        self._data_exhausted[symbol] = False
        self.last_id = None
        if symbol in self._total_records_cache:
            del self._total_records_cache[symbol]
    