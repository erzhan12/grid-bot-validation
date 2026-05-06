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


class TestMinNotionalFloor:
    """Tests for the $5 USDT min-notional floor (bbu2 parity)."""

    def test_fraction_below_floor_bumps_up(self, instrument_info):
        calc = create_qty_calculator("x0.001", instrument_info)
        intent = _make_intent("50000")
        # 100 * 0.001 / 50000 = 0.000002 → notional 0.10 USDT (well below $5)
        # Floor: 5 / 50000 = 0.0001 → rounded up to qty_step 0.001
        qty = calc(intent, Decimal("100"))
        assert qty == Decimal("0.001")

    def test_usdt_below_floor_bumps_up(self, instrument_info):
        # Fixed 1 USDT request → 1/50000 = 0.00002, notional $1 < $5 floor.
        # Floor: 5 / 50000 = 0.0001 → rounded up to qty_step 0.001
        calc = create_qty_calculator("1", instrument_info)
        intent = _make_intent("50000")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("0.001")

    # Note: a true strict-`<` vs `<=` boundary discriminator at exactly $5
    # cannot be written numerically — at the boundary `_MIN_NOTIONAL / price`
    # equals the raw qty, so both implementations yield identical output.
    # We rely on `test_floor_just_below_5_fires` (firing path) and
    # `test_floor_just_above_5_passes_through` (non-firing path) to cover
    # both sides of the boundary instead.

    def test_floor_just_above_5_passes_through(self):
        # Notional slightly above $5 must NOT be bumped (passthrough).
        # amount "5.01" at price 40000 → 0.00012525 raw.
        calc = create_qty_calculator("5.01", None)
        intent = _make_intent("40000")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("5.01") / Decimal("40000")  # passthrough

    def test_floor_just_below_5_fires(self):
        # Notional $4.99 (strictly below $5) MUST be bumped to floor.
        # Discriminator: without instrument_info, raw output reveals whether
        # floor fired. amount "4.99" at price 40000 → 0.00012475 raw.
        # Floor fires: replaces with 5/40000 = 0.000125.
        calc = create_qty_calculator("4.99", None)
        intent = _make_intent("40000")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("5") / Decimal("40000")  # bumped to floor
        assert qty != Decimal("4.99") / Decimal("40000")  # not passthrough

    def test_above_floor_passthrough(self, instrument_info):
        # 100 / 50000 = 0.002, notional 100 USDT >> 5; passthrough.
        calc = create_qty_calculator("100", instrument_info)
        intent = _make_intent("50000")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("0.002")


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

    def test_invalid_usdt_raises(self):
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("notanumber")

    def test_bare_x_raises(self):
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("x")

    def test_legacy_b_mode_now_rejected(self):
        # "b..." mode (BTC-equivalent for inverse contracts) is removed.
        # Strings starting with "b" route through the numeric branch and fail.
        with pytest.raises(ValueError, match="invalid amount string"):
            create_qty_calculator("b0.005")

    def test_negative_price(self, instrument_info):
        calc = create_qty_calculator("100", instrument_info)
        intent = _make_intent("-1")
        qty = calc(intent, Decimal("10000"))
        assert qty == Decimal("0")
