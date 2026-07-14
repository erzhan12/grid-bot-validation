"""Unit tests for InstrumentInfo.from_bybit_response."""

from decimal import Decimal

import pytest

from gridcore.instrument_info import InstrumentInfo


def _make_instrument(qty_step="0.001", tick_size="0.1", min_qty="0.001", max_qty="1000"):
    """Build a minimal Bybit instrument dict."""
    return {
        "lotSizeFilter": {
            "qtyStep": qty_step,
            "minOrderQty": min_qty,
            "maxOrderQty": max_qty,
        },
        "priceFilter": {
            "tickSize": tick_size,
        },
    }


class TestFromBybitResponse:
    """Tests for InstrumentInfo.from_bybit_response."""

    def test_fully_populated_payload_parses_correctly(self):
        """Fully-populated response yields correct InstrumentInfo."""
        instrument = _make_instrument(qty_step="0.01", tick_size="0.05")
        result = InstrumentInfo.from_bybit_response("LTCUSDT", instrument)

        assert result is not None
        assert result.symbol == "LTCUSDT"
        assert result.qty_step == Decimal("0.01")
        assert result.tick_size == Decimal("0.05")
        assert result.min_qty == Decimal("0.001")
        assert result.max_qty == Decimal("1000")

    def test_missing_qty_step_returns_none(self):
        """Missing lotSizeFilter.qtyStep key returns None (not synthetic default)."""
        instrument = {
            "lotSizeFilter": {
                # qtyStep deliberately absent
                "minOrderQty": "0.001",
                "maxOrderQty": "1000",
            },
            "priceFilter": {"tickSize": "0.1"},
        }
        result = InstrumentInfo.from_bybit_response("BTCUSDT", instrument)
        assert result is None

    def test_missing_tick_size_returns_none(self):
        """Missing priceFilter.tickSize key returns None (not synthetic default)."""
        instrument = {
            "lotSizeFilter": {
                "qtyStep": "0.001",
                "minOrderQty": "0.001",
                "maxOrderQty": "1000",
            },
            "priceFilter": {
                # tickSize deliberately absent
            },
        }
        result = InstrumentInfo.from_bybit_response("BTCUSDT", instrument)
        assert result is None

    def test_zero_qty_step_returns_none(self):
        """qtyStep=0 returns None."""
        instrument = _make_instrument(qty_step="0")
        result = InstrumentInfo.from_bybit_response("BTCUSDT", instrument)
        assert result is None

    def test_zero_tick_size_returns_none(self):
        """tickSize=0 returns None."""
        instrument = _make_instrument(tick_size="0")
        result = InstrumentInfo.from_bybit_response("BTCUSDT", instrument)
        assert result is None

    @pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN", "", "abc"])
    def test_nonfinite_or_unparseable_tick_size_returns_none(self, bad):
        """Infinity/NaN/empty/garbage tickSize returns None, never a poisoned tick.

        ``Decimal("Infinity") > 0`` is True, so a naive ``<= 0`` guard would let
        a non-finite tick through and corrupt every grid price. Unparseable
        strings must not raise, either.
        """
        instrument = _make_instrument(tick_size=bad)
        assert InstrumentInfo.from_bybit_response("BTCUSDT", instrument) is None

    @pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN", "", "abc"])
    def test_nonfinite_or_unparseable_qty_step_returns_none(self, bad):
        """Infinity/NaN/empty/garbage qtyStep returns None, never raises."""
        instrument = _make_instrument(qty_step=bad)
        assert InstrumentInfo.from_bybit_response("BTCUSDT", instrument) is None

    def test_null_filter_returns_none(self):
        """A present-but-null lotSizeFilter/priceFilter returns None, not raises."""
        assert (
            InstrumentInfo.from_bybit_response(
                "BTCUSDT",
                {"lotSizeFilter": None, "priceFilter": {"tickSize": "0.1"}},
            )
            is None
        )
        assert (
            InstrumentInfo.from_bybit_response(
                "BTCUSDT",
                {"lotSizeFilter": {"qtyStep": "0.01"}, "priceFilter": None},
            )
            is None
        )

    def test_min_max_qty_defaults_stay(self):
        """Missing minOrderQty/maxOrderQty still use their defaults."""
        instrument = {
            "lotSizeFilter": {
                "qtyStep": "0.01",
                # minOrderQty and maxOrderQty absent
            },
            "priceFilter": {"tickSize": "0.1"},
        }
        result = InstrumentInfo.from_bybit_response("BTCUSDT", instrument)
        assert result is not None
        assert result.min_qty == Decimal("0.001")
        assert result.max_qty == Decimal("1000")
