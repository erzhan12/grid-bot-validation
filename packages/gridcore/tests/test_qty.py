"""Unit tests for qty calculator factory."""

import pytest
from decimal import Decimal

from gridcore.qty import create_qty_calculator
from gridcore.instrument_info import InstrumentInfo
from gridcore.intents import PlaceLimitIntent


@pytest.fixture
def instrument_info():
    return InstrumentInfo(
        symbol="BTCUSDT",
        qty_step=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
    )


def _make_intent(price: str = "50000", side: str = "Buy") -> PlaceLimitIntent:
    return PlaceLimitIntent(
        symbol="BTCUSDT",
        side=side,
        price=Decimal(price),
        qty=Decimal("0"),
        reduce_only=False,
        client_order_id="test-001",
        grid_level=0,
        direction="long",
    )


class TestFractionMode:
    """Tests for 'x...' wallet-fraction amount strings."""

    def test_basic_fraction(self, instrument_info):
        calc = create_qty_calculator("x0.01", instrument_info)
        intent = _make_intent("50000")
        qty = calc(intent, Decimal("10000"))
        # 10000 * 0.01 / 50000 = 0.002
        assert qty == Decimal("0.002")

    def test_fraction_rounds_up(self, instrument_info):
        calc = create_qty_calculator("x0.01", instrument_info)
        intent = _make_intent("30000")
        qty = calc(intent, Decimal("10000"))
        # 10000 * 0.01 / 30000 = 0.003333... -> rounds up to 0.004
        assert qty == Decimal("0.004")

    def test_fraction_zero_price(self, instrument_info):
        calc = create_qty_calculator("x0.01", instrument_info)
        intent = _make_intent("0")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("0")

    def test_fraction_zero_balance(self, instrument_info):
        calc = create_qty_calculator("x0.01", instrument_info)
        intent = _make_intent("50000")
        qty = calc(intent, Decimal("0"))
        assert qty == Decimal("0")


class TestFixedUSDT:
    """Tests for plain numeric (fixed USDT) amount strings."""

    def test_basic_usdt(self, instrument_info):
        calc = create_qty_calculator("100", instrument_info)
        intent = _make_intent("50000")
        qty = calc(intent, Decimal("10000"))
        # 100 / 50000 = 0.002
        assert qty == Decimal("0.002")

    def test_usdt_rounds_up(self, instrument_info):
        calc = create_qty_calculator("100", instrument_info)
        intent = _make_intent("30000")
        qty = calc(intent, Decimal("10000"))
        # 100 / 30000 = 0.003333... -> rounds up to 0.004
        assert qty == Decimal("0.004")

    def test_usdt_zero_price(self, instrument_info):
        calc = create_qty_calculator("100", instrument_info)
        intent = _make_intent("0")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("0")


class TestFixedBase:
    """Tests for 'b...' fixed base currency amount strings."""

    def test_basic_base(self, instrument_info):
        calc = create_qty_calculator("b0.005", instrument_info)
        intent = _make_intent("50000")
        qty = calc(intent, Decimal("10000"))
        # Fixed base = 0.005, rounds up to 0.005
        assert qty == Decimal("0.005")

    def test_base_rounds_up(self, instrument_info):
        calc = create_qty_calculator("b0.0015", instrument_info)
        intent = _make_intent("50000")
        qty = calc(intent, Decimal("10000"))
        # 0.0015 rounds up to 0.002
        assert qty == Decimal("0.002")

    def test_base_ignores_price_and_balance(self, instrument_info):
        calc = create_qty_calculator("b0.01", instrument_info)
        intent = _make_intent("0")
        qty = calc(intent, Decimal("0"))
        # Fixed base always returns the amount, regardless of price/balance
        assert qty == Decimal("0.01")


class TestNoInstrumentInfo:
    """Tests without instrument_info (no rounding)."""

    def test_fraction_no_rounding(self):
        calc = create_qty_calculator("x0.01")
        intent = _make_intent("30000")
        qty = calc(intent, Decimal("10000"))
        # 10000 * 0.01 / 30000 = 0.000333...  (no rounding)
        expected = Decimal("10000") * Decimal("0.01") / Decimal("30000")
        assert qty == expected

    def test_usdt_no_rounding(self):
        calc = create_qty_calculator("100")
        intent = _make_intent("30000")
        qty = calc(intent, Decimal("10000"))
        expected = Decimal("100") / Decimal("30000")
        assert qty == expected


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            create_qty_calculator("")

    def test_invalid_fraction_raises(self):
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("xnotanumber")

    def test_invalid_base_raises(self):
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("bnotanumber")

    def test_invalid_usdt_raises(self):
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("notanumber")

    def test_bare_x_raises(self):
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("x")

    def test_bare_b_raises(self):
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("b")

    def test_negative_price(self, instrument_info):
        calc = create_qty_calculator("100", instrument_info)
        intent = _make_intent("-1")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("0")
