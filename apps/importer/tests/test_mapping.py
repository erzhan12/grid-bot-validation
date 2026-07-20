"""Tests for source-row -> TickerSnapshot mapping (feature 0093)."""

from datetime import datetime
from decimal import Decimal

from importer.mapping import FallbackCounters, map_row

_TS = datetime(2026, 7, 1, 12, 0, 0)


def _row(**overrides) -> dict:
    row = {
        "id": 1,
        "symbol": "BTCUSDT",
        "timestamp": _TS,
        "last_price": 65000.1,
        "mark_price": 65000.2,
        "bid1_price": 65000.0,
        "ask1_price": 65000.3,
        "funding_rate": 0.0001,
    }
    row.update(overrides)
    return row


class TestMapping:
    def test_float_to_decimal_8dp(self):
        """Floats cross the boundary as Decimal(str(x)) quantized to 8dp."""
        counters = FallbackCounters()
        snap = map_row(_row(last_price=0.1), counters)
        assert snap.last_price == Decimal("0.10000000")
        assert snap.funding_rate == Decimal("0.00010000")
        assert counters.as_dict() == {
            "skipped_null_last_price": 0,
            "mark_price_fallback": 0,
            "bid1_price_fallback": 0,
            "ask1_price_fallback": 0,
            "funding_rate_fallback": 0,
        }

    def test_local_ts_mirrors_exchange_ts(self):
        """No recv-ts on source: local_ts equals exchange_ts."""
        snap = map_row(_row(), FallbackCounters())
        assert snap.exchange_ts == _TS
        assert snap.local_ts == _TS

    def test_null_mark_falls_back_to_last(self):
        """NULL mark_price -> last_price fallback + counter increment."""
        counters = FallbackCounters()
        snap = map_row(_row(mark_price=None), counters)
        assert snap.mark_price == snap.last_price
        assert counters.mark_price_fallback == 1

    def test_null_bid_ask_fall_back_to_last(self):
        """NULL bid1/ask1 -> last_price fallback + counter increments."""
        counters = FallbackCounters()
        snap = map_row(_row(bid1_price=None, ask1_price=None), counters)
        assert snap.bid1_price == snap.last_price
        assert snap.ask1_price == snap.last_price
        assert counters.bid1_price_fallback == 1
        assert counters.ask1_price_fallback == 1

    def test_null_funding_falls_back_to_zero(self):
        """NULL funding_rate -> 0 + counter increment."""
        counters = FallbackCounters()
        snap = map_row(_row(funding_rate=None), counters)
        assert snap.funding_rate == Decimal("0")
        assert counters.funding_rate_fallback == 1

    def test_null_last_price_row_skipped(self):
        """NULL last_price row is skipped (never inserted) and counted."""
        counters = FallbackCounters()
        assert map_row(_row(last_price=None), counters) is None
        assert counters.skipped_null_last_price == 1

    def test_raw_json_is_null(self):
        """No audit payload on source: raw_json stays NULL."""
        assert map_row(_row(), FallbackCounters()).raw_json is None
