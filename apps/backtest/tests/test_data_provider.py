"""Tests for HistoricalDataProvider."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from gridcore import EventType
from grid_db import TickerSnapshot, PublicTrade

from backtest.data_provider import HistoricalDataProvider, DataRangeInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# SQLite strips timezone info, so use naive datetimes for assertions.
_BASE_TS = datetime(2025, 1, 15, 12, 0, 0)


def _make_ticker(
    symbol="BTCUSDT",
    exchange_ts=None,
    local_ts=None,
    last_price=Decimal("100000"),
    mark_price=Decimal("100000"),
    bid1_price=Decimal("99999"),
    ask1_price=Decimal("100001"),
    funding_rate=Decimal("0.0001"),
):
    ts = exchange_ts or _BASE_TS
    return TickerSnapshot(
        symbol=symbol,
        exchange_ts=ts,
        local_ts=local_ts or ts,
        last_price=last_price,
        mark_price=mark_price,
        bid1_price=bid1_price,
        ask1_price=ask1_price,
        funding_rate=funding_rate,
    )


def _make_trade(
    symbol="BTCUSDT",
    exchange_ts=None,
    local_ts=None,
    trade_id="t1",
    side="Buy",
    price=Decimal("100000"),
    size=Decimal("0.01"),
):
    ts = exchange_ts or _BASE_TS
    return PublicTrade(
        symbol=symbol,
        exchange_ts=ts,
        local_ts=local_ts or ts,
        trade_id=trade_id,
        side=side,
        price=price,
        size=size,
    )


def _seed(db, records):
    """Insert ORM records into the database."""
    with db.get_session() as session:
        for rec in records:
            session.add(rec)


# ---------------------------------------------------------------------------
# TestIterateTickers
# ---------------------------------------------------------------------------

class TestIterateTickers:
    """Tests for _iterate_tickers() via __iter__ with use_trades=False."""

    def test_basic_iteration(self, db):
        """3 tickers yield 3 TickerEvents with correct event_type."""
        tickers = [
            _make_ticker(exchange_ts=_BASE_TS + timedelta(seconds=i))
            for i in range(3)
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
        )
        events = list(provider)

        assert len(events) == 3
        for ev in events:
            assert ev.event_type == EventType.TICKER

    def test_field_mapping(self, db):
        """All TickerSnapshot fields map correctly to TickerEvent fields."""
        ts = _BASE_TS
        local = _BASE_TS + timedelta(milliseconds=50)
        ticker = _make_ticker(
            symbol="ETHUSDT",
            exchange_ts=ts,
            local_ts=local,
            last_price=Decimal("3500.50"),
            mark_price=Decimal("3501.00"),
            bid1_price=Decimal("3500.00"),
            ask1_price=Decimal("3501.50"),
            funding_rate=Decimal("0.00015"),
        )
        _seed(db, [ticker])

        provider = HistoricalDataProvider(
            db=db,
            symbol="ETHUSDT",
            start_ts=ts - timedelta(seconds=1),
            end_ts=ts + timedelta(seconds=1),
        )
        events = list(provider)
        assert len(events) == 1
        ev = events[0]

        assert ev.symbol == "ETHUSDT"
        assert ev.exchange_ts == ts
        assert ev.local_ts == local
        assert ev.last_price == Decimal("3500.50")
        assert ev.mark_price == Decimal("3501.00")
        assert ev.bid1_price == Decimal("3500.00")
        assert ev.ask1_price == Decimal("3501.50")
        assert ev.funding_rate == Decimal("0.00015")

    def test_date_range_filtering(self, db):
        """Records outside [start_ts, end_ts] are excluded."""
        tickers = [
            _make_ticker(exchange_ts=_BASE_TS + timedelta(seconds=i))
            for i in range(5)
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS + timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=3),
        )
        events = list(provider)
        assert len(events) == 3  # seconds 1, 2, 3

    def test_inclusive_boundaries(self, db):
        """Records at exact start_ts and end_ts are included."""
        start = _BASE_TS
        end = _BASE_TS + timedelta(seconds=2)
        tickers = [
            _make_ticker(exchange_ts=start),
            _make_ticker(exchange_ts=start + timedelta(seconds=1)),
            _make_ticker(exchange_ts=end),
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db, symbol="BTCUSDT", start_ts=start, end_ts=end,
        )
        events = list(provider)
        assert len(events) == 3

    def test_chronological_ordering(self, db):
        """Results are sorted by exchange_ts ascending."""
        # Insert in reverse order
        tickers = [
            _make_ticker(
                exchange_ts=_BASE_TS + timedelta(seconds=i),
                last_price=Decimal(str(100000 + i)),
            )
            for i in [4, 2, 0, 3, 1]
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
        )
        events = list(provider)
        timestamps = [ev.exchange_ts for ev in events]
        assert timestamps == sorted(timestamps)
        assert len(events) == 5

    def test_symbol_filtering(self, db):
        """Only records for requested symbol are returned."""
        tickers = [
            _make_ticker(symbol="BTCUSDT", exchange_ts=_BASE_TS),
            _make_ticker(symbol="ETHUSDT", exchange_ts=_BASE_TS + timedelta(seconds=1)),
            _make_ticker(symbol="BTCUSDT", exchange_ts=_BASE_TS + timedelta(seconds=2)),
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
        )
        events = list(provider)
        assert len(events) == 2
        assert all(ev.symbol == "BTCUSDT" for ev in events)

    def test_pagination_across_batches(self, db):
        """5 records with batch_size=2 still yields all 5 in order."""
        tickers = [
            _make_ticker(
                exchange_ts=_BASE_TS + timedelta(seconds=i),
                last_price=Decimal(str(100000 + i)),
            )
            for i in range(5)
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            batch_size=2,
        )
        events = list(provider)
        assert len(events) == 5
        prices = [ev.last_price for ev in events]
        assert prices == [Decimal(str(100000 + i)) for i in range(5)]

    def test_batch_size_one(self, db):
        """batch_size=1 works correctly."""
        tickers = [
            _make_ticker(exchange_ts=_BASE_TS + timedelta(seconds=i))
            for i in range(3)
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            batch_size=1,
        )
        events = list(provider)
        assert len(events) == 3

    def test_empty_database(self, db):
        """Empty DB yields zero events."""
        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
        )
        events = list(provider)
        assert len(events) == 0

    def test_single_record(self, db):
        """Single record yields one event."""
        _seed(db, [_make_ticker(exchange_ts=_BASE_TS)])

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=1),
        )
        events = list(provider)
        assert len(events) == 1


# ---------------------------------------------------------------------------
# TestIterateTrades
# ---------------------------------------------------------------------------

class TestIterateTrades:
    """Tests for _iterate_trades() via __iter__ with use_trades=True."""

    def test_basic_iteration(self, db):
        """3 trades yield 3 TickerEvents."""
        trades = [
            _make_trade(
                exchange_ts=_BASE_TS + timedelta(seconds=i),
                trade_id=f"t{i}",
            )
            for i in range(3)
        ]
        _seed(db, trades)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            use_trades=True,
        )
        events = list(provider)
        assert len(events) == 3

    def test_price_mapped_to_all_fields(self, db):
        """Trade price maps to last_price, mark_price, bid1_price, ask1_price."""
        price = Decimal("42000.50")
        _seed(db, [_make_trade(price=price)])

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=1),
            use_trades=True,
        )
        ev = list(provider)[0]
        assert ev.last_price == price
        assert ev.mark_price == price
        assert ev.bid1_price == price
        assert ev.ask1_price == price

    def test_funding_rate_is_zero(self, db):
        """funding_rate is always Decimal('0') for trades."""
        _seed(db, [_make_trade()])

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=1),
            use_trades=True,
        )
        ev = list(provider)[0]
        assert ev.funding_rate == Decimal("0")

    def test_timestamp_mapping(self, db):
        """exchange_ts and local_ts from trade mapped correctly."""
        exchange = _BASE_TS
        local = _BASE_TS + timedelta(milliseconds=100)
        _seed(db, [_make_trade(exchange_ts=exchange, local_ts=local)])

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=1),
            use_trades=True,
        )
        ev = list(provider)[0]
        assert ev.exchange_ts == exchange
        assert ev.local_ts == local

    def test_event_type_is_ticker(self, db):
        """event_type is EventType.TICKER even from trade source."""
        _seed(db, [_make_trade()])

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=1),
            use_trades=True,
        )
        ev = list(provider)[0]
        assert ev.event_type == EventType.TICKER

    def test_date_range_filtering(self, db):
        """Date range filtering works for trades."""
        trades = [
            _make_trade(
                exchange_ts=_BASE_TS + timedelta(seconds=i),
                trade_id=f"t{i}",
            )
            for i in range(5)
        ]
        _seed(db, trades)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS + timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=3),
            use_trades=True,
        )
        events = list(provider)
        assert len(events) == 3

    def test_symbol_filtering(self, db):
        """Symbol filtering works for trades."""
        trades = [
            _make_trade(symbol="BTCUSDT", trade_id="t1"),
            _make_trade(symbol="ETHUSDT", trade_id="t2",
                        exchange_ts=_BASE_TS + timedelta(seconds=1)),
            _make_trade(symbol="BTCUSDT", trade_id="t3",
                        exchange_ts=_BASE_TS + timedelta(seconds=2)),
        ]
        _seed(db, trades)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            use_trades=True,
        )
        events = list(provider)
        assert len(events) == 2
        assert all(ev.symbol == "BTCUSDT" for ev in events)

    def test_pagination_across_batches(self, db):
        """Pagination works for trades."""
        trades = [
            _make_trade(
                exchange_ts=_BASE_TS + timedelta(seconds=i),
                trade_id=f"t{i}",
                price=Decimal(str(50000 + i)),
            )
            for i in range(5)
        ]
        _seed(db, trades)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            batch_size=2,
            use_trades=True,
        )
        events = list(provider)
        assert len(events) == 5
        prices = [ev.last_price for ev in events]
        assert prices == [Decimal(str(50000 + i)) for i in range(5)]


# ---------------------------------------------------------------------------
# TestGetDataRangeInfo
# ---------------------------------------------------------------------------

class TestGetDataRangeInfo:
    """Tests for get_data_range_info()."""

    def test_ticker_range_info(self, db):
        """Returns correct count, min/max timestamps for tickers."""
        tickers = [
            _make_ticker(exchange_ts=_BASE_TS + timedelta(seconds=i))
            for i in range(5)
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
        )
        info = provider.get_data_range_info()

        assert info.symbol == "BTCUSDT"
        assert info.total_records == 5
        assert info.start_ts == _BASE_TS
        assert info.end_ts == _BASE_TS + timedelta(seconds=4)

    def test_ticker_range_info_with_date_filter(self, db):
        """Filtered range returns subset."""
        tickers = [
            _make_ticker(exchange_ts=_BASE_TS + timedelta(seconds=i))
            for i in range(10)
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS + timedelta(seconds=2),
            end_ts=_BASE_TS + timedelta(seconds=5),
        )
        info = provider.get_data_range_info()

        assert info.total_records == 4  # seconds 2, 3, 4, 5
        assert info.start_ts == _BASE_TS + timedelta(seconds=2)
        assert info.end_ts == _BASE_TS + timedelta(seconds=5)

    def test_ticker_range_info_empty(self, db):
        """Empty DB returns total_records=0, timestamps None."""
        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS,
            end_ts=_BASE_TS + timedelta(seconds=10),
        )
        info = provider.get_data_range_info()

        assert info.total_records == 0
        assert info.start_ts is None
        assert info.end_ts is None

    def test_ticker_range_info_symbol_filter(self, db):
        """Only counts requested symbol."""
        tickers = [
            _make_ticker(symbol="BTCUSDT", exchange_ts=_BASE_TS),
            _make_ticker(symbol="ETHUSDT", exchange_ts=_BASE_TS + timedelta(seconds=1)),
            _make_ticker(symbol="BTCUSDT", exchange_ts=_BASE_TS + timedelta(seconds=2)),
        ]
        _seed(db, tickers)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
        )
        info = provider.get_data_range_info()

        assert info.total_records == 2
        assert info.symbol == "BTCUSDT"

    def test_trade_range_info(self, db):
        """Correct stats with use_trades=True."""
        trades = [
            _make_trade(
                exchange_ts=_BASE_TS + timedelta(seconds=i),
                trade_id=f"t{i}",
            )
            for i in range(3)
        ]
        _seed(db, trades)

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            use_trades=True,
        )
        info = provider.get_data_range_info()

        assert info.total_records == 3
        assert info.start_ts == _BASE_TS
        assert info.end_ts == _BASE_TS + timedelta(seconds=2)

    def test_trade_range_info_empty(self, db):
        """Empty trade table returns total_records=0."""
        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS,
            end_ts=_BASE_TS + timedelta(seconds=10),
            use_trades=True,
        )
        info = provider.get_data_range_info()

        assert info.total_records == 0
        assert info.start_ts is None
        assert info.end_ts is None


# ---------------------------------------------------------------------------
# TestIterDispatch
# ---------------------------------------------------------------------------

class TestIterDispatch:
    """Tests for __iter__() dispatch between tickers and trades."""

    def test_default_uses_tickers(self, db):
        """use_trades=False (default) iterates tickers, not trades."""
        # Seed both tickers and trades
        _seed(db, [_make_ticker(exchange_ts=_BASE_TS)])
        _seed(db, [_make_trade(exchange_ts=_BASE_TS + timedelta(seconds=1))])

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            use_trades=False,
        )
        events = list(provider)
        # Should only get the 1 ticker, not the trade
        assert len(events) == 1
        assert events[0].exchange_ts == _BASE_TS

    def test_flag_switches_to_trades(self, db):
        """use_trades=True iterates trades, not tickers."""
        # Seed both tickers and trades
        _seed(db, [_make_ticker(exchange_ts=_BASE_TS)])
        _seed(db, [_make_trade(exchange_ts=_BASE_TS + timedelta(seconds=1))])

        provider = HistoricalDataProvider(
            db=db,
            symbol="BTCUSDT",
            start_ts=_BASE_TS - timedelta(seconds=1),
            end_ts=_BASE_TS + timedelta(seconds=10),
            use_trades=True,
        )
        events = list(provider)
        # Should only get the 1 trade, not the ticker
        assert len(events) == 1
        assert events[0].exchange_ts == _BASE_TS + timedelta(seconds=1)
