"""Shared helper functions for integration tests."""

import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from gridcore.events import TickerEvent, EventType


def make_ticker_event(symbol, price, ts):
    """Create a TickerEvent at given price and timestamp."""
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol=symbol,
        exchange_ts=ts,
        local_ts=ts,
        last_price=Decimal(str(price)),
        mark_price=Decimal(str(price)),
        bid1_price=Decimal(str(price)) - Decimal("0.5"),
        ask1_price=Decimal(str(price)) + Decimal("0.5"),
        funding_rate=Decimal("0.0001"),
    )


def generate_price_series(
    symbol="BTCUSDT",
    start_price=100000.0,
    amplitude=1000.0,
    num_ticks=200,
    start_time=None,
    interval_seconds=60,
):
    """Generate oscillating price series for testing.

    Produces a sine-wave-like oscillation around start_price.
    To guarantee grid fills, amplitude must exceed
    grid_step * grid_count / 2 (half the grid span).

    Returns:
        List of TickerEvent objects.
    """
    if start_time is None:
        start_time = datetime(2025, 1, 1, tzinfo=timezone.utc)

    events = []
    period = num_ticks / 4  # Complete 4 oscillations

    for i in range(num_ticks):
        # Oscillate: start_price + amplitude * sin(2*pi*i/period)
        offset = amplitude * math.sin(2 * math.pi * i / period)
        price = round(start_price + offset, 1)  # BTCUSDT has 0.1 tick
        ts = start_time + timedelta(seconds=i * interval_seconds)
        events.append(make_ticker_event(symbol, price, ts))

    return events
