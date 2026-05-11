"""Tests for fill simulator."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gridcore import EventType, TickerEvent

from backtest.fill_simulator import FillMode, TradeThroughFillSimulator
from backtest.order_manager import SimulatedOrder


def _order(side: str, price: Decimal = Decimal("58.60")) -> SimulatedOrder:
    return SimulatedOrder(
        order_id="1",
        client_order_id="c1",
        symbol="LTCUSDT",
        side=side,
        price=price,
        qty=Decimal("0.1"),
        direction="long" if side == "Buy" else "short",
        grid_level=0,
    )


def _ticker(
    last: Decimal,
    bid: Decimal,
    ask: Decimal,
) -> TickerEvent:
    ts = datetime(2026, 5, 11, 4, 49, 58, tzinfo=timezone.utc)
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="LTCUSDT",
        exchange_ts=ts,
        local_ts=ts,
        last_price=last,
        mark_price=last,
        bid1_price=bid,
        ask1_price=ask,
        funding_rate=Decimal("0"),
    )


class TestTradeThroughFillSimulator:
    """Tests for TradeThroughFillSimulator."""

    def test_buy_does_not_fill_when_price_at_limit(self, fill_simulator):
        """Buy order does NOT fill when price exactly equals limit (conservative)."""
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.01"),
            direction="long",
            grid_level=0,
        )

        result = fill_simulator.check_fill(order, Decimal("100000"))

        # At limit price, no fill (queue position unknown)
        assert result.should_fill is False
        assert result.fill_price == Decimal("0")

    def test_buy_fills_when_price_below_limit(self, fill_simulator):
        """Buy order fills when price drops below limit."""
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.01"),
            direction="long",
            grid_level=0,
        )

        result = fill_simulator.check_fill(order, Decimal("99000"))

        assert result.should_fill is True
        assert result.fill_price == Decimal("100000")  # Fills at limit price

    def test_buy_does_not_fill_when_price_above_limit(self, fill_simulator):
        """Buy order does not fill when price above limit."""
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.01"),
            direction="long",
            grid_level=0,
        )

        result = fill_simulator.check_fill(order, Decimal("101000"))

        assert result.should_fill is False
        assert result.fill_price == Decimal("0")

    def test_sell_does_not_fill_when_price_at_limit(self, fill_simulator):
        """Sell order does NOT fill when price exactly equals limit (conservative)."""
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("100000"),
            qty=Decimal("0.01"),
            direction="short",
            grid_level=0,
        )

        result = fill_simulator.check_fill(order, Decimal("100000"))

        # At limit price, no fill (queue position unknown)
        assert result.should_fill is False
        assert result.fill_price == Decimal("0")

    def test_sell_fills_when_price_above_limit(self, fill_simulator):
        """Sell order fills when price rises above limit."""
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("100000"),
            qty=Decimal("0.01"),
            direction="short",
            grid_level=0,
        )

        result = fill_simulator.check_fill(order, Decimal("101000"))

        assert result.should_fill is True
        assert result.fill_price == Decimal("100000")  # Fills at limit price

    def test_sell_does_not_fill_when_price_below_limit(self, fill_simulator):
        """Sell order does not fill when price below limit."""
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("100000"),
            qty=Decimal("0.01"),
            direction="short",
            grid_level=0,
        )

        result = fill_simulator.check_fill(order, Decimal("99000"))

        assert result.should_fill is False
        assert result.fill_price == Decimal("0")

    def test_get_fill_price_returns_limit_price(self, fill_simulator):
        """Fill price is always the limit price."""
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("99500.50"),
            qty=Decimal("0.01"),
            direction="long",
            grid_level=0,
        )

        assert fill_simulator.get_fill_price(order) == Decimal("99500.50")


class TestFillModeMatrix:
    """Mode matrix for strict, last-price-at-limit, and L1 touch fills."""

    @pytest.mark.parametrize(
        (
            "market",
            "buy_expected",
            "sell_expected",
        ),
        [
            (
                _ticker(Decimal("58.59"), Decimal("58.58"), Decimal("58.59")),
                {
                    FillMode.STRICT_CROSS: True,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: False,
                    FillMode.BOOK_TOUCH: False,
                },
            ),
            (
                _ticker(Decimal("58.61"), Decimal("58.61"), Decimal("58.62")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: False,
                    FillMode.BOOK_TOUCH: False,
                },
                {
                    FillMode.STRICT_CROSS: True,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                },
            ),
            (
                _ticker(Decimal("58.60"), Decimal("58.59"), Decimal("58.61")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: False,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: False,
                },
            ),
            (
                _ticker(Decimal("58.60"), Decimal("58.60"), Decimal("58.60")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                },
            ),
            (
                _ticker(Decimal("58.61"), Decimal("58.60"), Decimal("58.60")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: False,
                    FillMode.BOOK_TOUCH: True,
                },
                {
                    FillMode.STRICT_CROSS: True,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                },
            ),
            (
                Decimal("58.60"),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                },
            ),
        ],
    )
    @pytest.mark.parametrize("mode", list(FillMode))
    def test_fill_mode_matrix(self, market, buy_expected, sell_expected, mode):
        simulator = TradeThroughFillSimulator(mode=mode)

        buy_result = simulator.check_fill(_order("Buy"), market)
        sell_result = simulator.check_fill(_order("Sell"), market)

        assert buy_result.should_fill is buy_expected[mode]
        assert sell_result.should_fill is sell_expected[mode]

    def test_book_touch_falls_back_to_trade_through_on_bare_decimal_buy(self):
        simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)

        result = simulator.check_fill(_order("Buy"), Decimal("58.60"))

        assert result.should_fill is True
        assert result.fill_price == Decimal("58.60")

    def test_book_touch_falls_back_to_trade_through_on_bare_decimal_sell(self):
        simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)

        result = simulator.check_fill(_order("Sell"), Decimal("58.60"))

        assert result.should_fill is True
        assert result.fill_price == Decimal("58.60")

    def test_book_touch_treats_default_zero_bid_ask_as_missing_l1(self):
        """TickerEvent's default zero bid/ask must not trigger phantom fills."""
        simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)
        ts = datetime(2026, 5, 11, 4, 49, 58, tzinfo=timezone.utc)
        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="LTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            last_price=Decimal("58.61"),
            mark_price=Decimal("58.61"),
        )

        buy_result = simulator.check_fill(_order("Buy"), ticker)
        sell_result = simulator.check_fill(_order("Sell"), ticker)

        assert buy_result.should_fill is False
        assert sell_result.should_fill is True

    @pytest.mark.parametrize("mode", list(FillMode))
    def test_non_positive_last_price_never_fills(self, mode):
        """Default zero last_price is invalid market data, not a cross."""
        simulator = TradeThroughFillSimulator(mode=mode)
        ts = datetime(2026, 5, 11, 4, 49, 58, tzinfo=timezone.utc)
        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="LTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
        )

        buy_result = simulator.check_fill(_order("Buy"), ticker)
        sell_result = simulator.check_fill(_order("Sell"), ticker)

        assert buy_result.should_fill is False
        assert sell_result.should_fill is False

    def test_invalid_fill_mode_string_raises_with_valid_options(self):
        """Construction with an unknown mode lists the valid options."""
        with pytest.raises(ValueError, match="Valid modes: strict_cross"):
            TradeThroughFillSimulator(mode="not_a_mode")

    def test_book_touch_rejects_invalid_side(self):
        """book_touch raises on side values outside SideType."""
        simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="LTCUSDT",
            side="Invalid",
            price=Decimal("58.60"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
        )
        ticker = _ticker(Decimal("58.60"), Decimal("58.60"), Decimal("58.60"))

        with pytest.raises(ValueError, match="Invalid order side"):
            simulator.check_fill(order, ticker)
