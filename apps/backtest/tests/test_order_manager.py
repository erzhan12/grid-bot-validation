"""Tests for order manager."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest.order_manager import BacktestOrderManager, SimulatedOrder
from backtest.fill_simulator import TradeThroughFillSimulator


class TestBacktestOrderManager:
    """Tests for BacktestOrderManager."""

    def test_place_order_creates_order(self, order_manager, sample_timestamp):
        """Place order creates and tracks order."""
        order = order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        assert order is not None
        assert order.client_order_id == "c1"
        assert order.symbol == "BTCUSDT"
        assert order.side == "Buy"
        assert order.price == Decimal("100000")
        assert order.status == "pending"
        assert order_manager.total_active_orders == 1

    def test_place_order_rejects_duplicate(self, order_manager, sample_timestamp):
        """Duplicate client_order_id is rejected."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # Try to place duplicate
        duplicate = order_manager.place_order(
            client_order_id="c1",  # Same client_order_id
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("101000"),
            qty=Decimal("0.1"),
            direction="short",
            grid_level=1,
            timestamp=sample_timestamp,
        )

        assert duplicate is None
        assert order_manager.total_active_orders == 1  # Still just one order

    def test_cancel_order(self, order_manager, sample_timestamp):
        """Cancel removes order from active."""
        order = order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        success = order_manager.cancel_order(order.order_id, sample_timestamp)

        assert success is True
        assert order_manager.total_active_orders == 0
        assert len(order_manager.cancelled_orders) == 1
        assert order_manager.cancelled_orders[0].status == "cancelled"

    def test_cancel_nonexistent_order(self, order_manager, sample_timestamp):
        """Cancel nonexistent order returns False."""
        success = order_manager.cancel_order("nonexistent", sample_timestamp)

        assert success is False

    def test_check_fills_buy_order(self, order_manager, sample_timestamp):
        """Buy order fills when price drops to limit."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # Price drops to 99000 (below limit)
        fills = order_manager.check_fills(
            current_price=Decimal("99000"),
            timestamp=sample_timestamp,
        )

        assert len(fills) == 1
        assert fills[0].side == "Buy"
        assert fills[0].price == Decimal("100000")  # Fills at limit
        assert fills[0].qty == Decimal("0.1")
        assert order_manager.total_active_orders == 0
        assert order_manager.total_filled_orders == 1

    def test_check_fills_sell_order(self, order_manager, sample_timestamp):
        """Sell order fills when price rises to limit."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("101000"),
            qty=Decimal("0.1"),
            direction="short",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # Price rises to 102000 (above limit)
        fills = order_manager.check_fills(
            current_price=Decimal("102000"),
            timestamp=sample_timestamp,
        )

        assert len(fills) == 1
        assert fills[0].side == "Sell"
        assert fills[0].price == Decimal("101000")

    def test_check_fills_no_fill(self, order_manager, sample_timestamp):
        """Order doesn't fill when price doesn't cross."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # Price at 101000 (above buy limit)
        fills = order_manager.check_fills(
            current_price=Decimal("101000"),
            timestamp=sample_timestamp,
        )

        assert len(fills) == 0
        assert order_manager.total_active_orders == 1

    def test_check_fills_multiple_orders(self, order_manager, sample_timestamp):
        """Multiple orders can fill simultaneously."""
        # Place two buy orders at different prices
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        order_manager.place_order(
            client_order_id="c2",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("99500"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=1,
            timestamp=sample_timestamp,
        )

        # Price drops to 99000 (both should fill)
        fills = order_manager.check_fills(
            current_price=Decimal("99000"),
            timestamp=sample_timestamp,
        )

        assert len(fills) == 2
        assert order_manager.total_active_orders == 0
        assert order_manager.total_filled_orders == 2

    def test_get_limit_orders(self, order_manager, sample_timestamp):
        """Get orders in GridEngine format."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        order_manager.place_order(
            client_order_id="c2",
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("101000"),
            qty=Decimal("0.1"),
            direction="short",
            grid_level=1,
            timestamp=sample_timestamp,
        )

        limit_orders = order_manager.get_limit_orders()

        assert len(limit_orders["long"]) == 1
        assert len(limit_orders["short"]) == 1
        # Keys use camelCase to match GridEngine expectations
        assert limit_orders["long"][0]["price"] == "100000"  # String like Bybit API
        assert limit_orders["short"][0]["price"] == "101000"
        assert "orderId" in limit_orders["long"][0]
        assert "orderLinkId" in limit_orders["long"][0]

    def test_check_fills_symbol_filter(self, order_manager, sample_timestamp):
        """Symbol filter only checks matching orders."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        order_manager.place_order(
            client_order_id="c2",
            symbol="ETHUSDT",
            side="Buy",
            price=Decimal("3000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # Price check for BTCUSDT only
        fills = order_manager.check_fills(
            current_price=Decimal("99000"),
            timestamp=sample_timestamp,
            symbol="BTCUSDT",
        )

        assert len(fills) == 1
        assert fills[0].symbol == "BTCUSDT"
        assert order_manager.total_active_orders == 1  # ETHUSDT still active

    def test_fill_includes_commission(self, order_manager, sample_timestamp):
        """Execution event includes calculated commission."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        fills = order_manager.check_fills(
            current_price=Decimal("99000"),
            timestamp=sample_timestamp,
        )

        # Commission = 0.1 * 100000 * 0.0002 = 2
        assert fills[0].fee == Decimal("2")

    def test_cancel_by_client_order_id_success(self, order_manager, sample_timestamp):
        """Cancel by client_order_id removes order from active."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        success = order_manager.cancel_by_client_order_id("c1", sample_timestamp)

        assert success is True
        assert order_manager.total_active_orders == 0
        assert len(order_manager.cancelled_orders) == 1
        assert order_manager.cancelled_orders[0].status == "cancelled"
        assert order_manager.cancelled_orders[0].client_order_id == "c1"

    def test_cancel_by_client_order_id_not_found(self, order_manager, sample_timestamp):
        """Cancel with non-existent client_order_id returns False."""
        success = order_manager.cancel_by_client_order_id("nonexistent", sample_timestamp)

        assert success is False

    def test_cancel_by_client_order_id_allows_reuse(self, order_manager, sample_timestamp):
        """Canceled client_order_id can be reused for a new order."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        order_manager.cancel_by_client_order_id("c1", sample_timestamp)

        # Reuse the same client_order_id
        order = order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("101000"),
            qty=Decimal("0.2"),
            direction="short",
            grid_level=1,
            timestamp=sample_timestamp,
        )

        assert order is not None
        assert order.client_order_id == "c1"
        assert order_manager.total_active_orders == 1

    def test_get_order_by_id_found(self, order_manager, sample_timestamp):
        """Get active order by order_id."""
        order = order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        found = order_manager.get_order_by_id(order.order_id)

        assert found is not None
        assert found.order_id == order.order_id
        assert found.client_order_id == "c1"

    def test_get_order_by_id_not_found(self, order_manager):
        """Get non-existent order_id returns None."""
        assert order_manager.get_order_by_id("nonexistent") is None

    def test_get_order_by_client_id_active(self, order_manager, sample_timestamp):
        """Get active order by client_order_id."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        found = order_manager.get_order_by_client_id("c1")

        assert found is not None
        assert found.client_order_id == "c1"
        assert found.status == "pending"

    def test_get_order_by_client_id_filled(self, order_manager, sample_timestamp):
        """Get filled order by client_order_id."""
        order_manager.place_order(
            client_order_id="c1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # Fill the order
        order_manager.check_fills(
            current_price=Decimal("99000"),
            timestamp=sample_timestamp,
        )

        found = order_manager.get_order_by_client_id("c1")

        assert found is not None
        assert found.client_order_id == "c1"
        assert found.status == "filled"

    def test_get_order_by_client_id_not_found(self, order_manager):
        """Get non-existent client_order_id returns None."""
        assert order_manager.get_order_by_client_id("nonexistent") is None
