"""Tests for fill simulator."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest.fill_simulator import TradeThroughFillSimulator, FillResult
from backtest.order_manager import SimulatedOrder


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
