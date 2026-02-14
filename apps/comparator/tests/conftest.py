"""Test fixtures for comparator package."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from grid_db import DatabaseFactory, DatabaseSettings
from gridcore.position import DirectionType, SideType

from comparator.loader import NormalizedTrade
from comparator.matcher import MatchedTrade, MatchResult


@pytest.fixture
def db_settings():
    """In-memory SQLite settings for testing."""
    return DatabaseSettings(
        db_type="sqlite",
        db_name=":memory:",
        echo_sql=False,
    )


@pytest.fixture
def db(db_settings):
    """Create fresh in-memory database for each test."""
    database = DatabaseFactory(db_settings)
    database.create_tables()
    yield database
    database.drop_tables()


@pytest.fixture
def ts():
    """Base timestamp for tests."""
    return datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def make_trade(ts):
    """Factory for creating NormalizedTrade instances."""
    def _make(
        client_order_id="order_abc",
        symbol="BTCUSDT",
        side="Buy",
        price=Decimal("100000"),
        qty=Decimal("0.001"),
        fee=Decimal("0.02"),
        realized_pnl=Decimal("0"),
        timestamp=None,
        source="live",
        direction=None,
    ):
        return NormalizedTrade(
            client_order_id=client_order_id,
            symbol=symbol,
            side=SideType(side),
            price=price,
            qty=qty,
            fee=fee,
            realized_pnl=realized_pnl,
            timestamp=timestamp or ts,
            source=source,
            direction=DirectionType(direction) if direction else None,
        )
    return _make


@pytest.fixture
def sample_live_trades(make_trade, ts):
    """Sample set of live trades."""
    return [
        make_trade(client_order_id="order_1", side="Buy", price=Decimal("100000"),
                    qty=Decimal("0.001"), fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                    timestamp=ts, source="live", direction="long"),
        make_trade(client_order_id="order_2", side="Sell", price=Decimal("100200"),
                    qty=Decimal("0.001"), fee=Decimal("0.02"), realized_pnl=Decimal("0.2"),
                    timestamp=ts + timedelta(hours=1), source="live", direction="long"),
        make_trade(client_order_id="order_3", side="Buy", price=Decimal("99800"),
                    qty=Decimal("0.002"), fee=Decimal("0.04"), realized_pnl=Decimal("0"),
                    timestamp=ts + timedelta(hours=2), source="live", direction="long"),
    ]


@pytest.fixture
def sample_backtest_trades(make_trade, ts):
    """Sample set of backtest trades matching live (except order_3 missing, order_4 phantom)."""
    return [
        make_trade(client_order_id="order_1", side="Buy", price=Decimal("100000"),
                    qty=Decimal("0.001"), fee=Decimal("0.02"), realized_pnl=Decimal("0"),
                    timestamp=ts, source="backtest", direction="long"),
        make_trade(client_order_id="order_2", side="Sell", price=Decimal("100200"),
                    qty=Decimal("0.0011"), fee=Decimal("0.022"), realized_pnl=Decimal("0.22"),
                    timestamp=ts + timedelta(hours=1, minutes=5), source="backtest", direction="long"),
        make_trade(client_order_id="order_4", side="Sell", price=Decimal("100400"),
                    qty=Decimal("0.001"), fee=Decimal("0.02"), realized_pnl=Decimal("0.4"),
                    timestamp=ts + timedelta(hours=3), source="backtest", direction="short"),
    ]


@pytest.fixture
def sample_match_result(sample_live_trades, sample_backtest_trades):
    """Pre-computed match result from sample trades."""
    matched = [
        MatchedTrade(live=sample_live_trades[0], backtest=sample_backtest_trades[0]),
        MatchedTrade(live=sample_live_trades[1], backtest=sample_backtest_trades[1]),
    ]
    live_only = [sample_live_trades[2]]  # order_3
    backtest_only = [sample_backtest_trades[2]]  # order_4
    return MatchResult(matched=matched, live_only=live_only, backtest_only=backtest_only)
