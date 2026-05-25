"""Tests for order manager."""

from decimal import Decimal

import pytest

from gridcore import EventType, TickerEvent

from backtest.fill_simulator import FillMode, TradeThroughFillSimulator


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

    def test_check_fills_ticker_event_scopes_to_event_symbol(
        self,
        order_manager,
        sample_timestamp,
    ):
        """TickerEvent input only checks orders for the event symbol."""
        order_manager.place_order(
            client_order_id="btc",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        order_manager.place_order(
            client_order_id="eth",
            symbol="ETHUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=Decimal("99000"),
            mark_price=Decimal("99000"),
            bid1_price=Decimal("98999"),
            ask1_price=Decimal("99001"),
            funding_rate=Decimal("0"),
        )

        fills = order_manager.check_fills(ticker)

        assert len(fills) == 1
        assert fills[0].symbol == "BTCUSDT"
        assert order_manager.get_order_by_client_id("eth").status == "pending"

    def test_check_fills_ticker_event_ignores_symbol_override(
        self,
        order_manager,
        sample_timestamp,
    ):
        """TickerEvent input is always scoped to the event symbol."""
        order_manager.place_order(
            client_order_id="btc",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        order_manager.place_order(
            client_order_id="eth",
            symbol="ETHUSDT",
            side="Buy",
            price=Decimal("3000"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=Decimal("99000"),
            mark_price=Decimal("99000"),
            bid1_price=Decimal("98999"),
            ask1_price=Decimal("99001"),
            funding_rate=Decimal("0"),
        )

        fills = order_manager.check_fills(ticker, symbol="ETHUSDT")

        assert len(fills) == 1
        assert fills[0].symbol == "BTCUSDT"
        assert order_manager.get_order_by_client_id("eth").status == "pending"

    def test_check_fills_decimal_requires_timestamp(self, order_manager):
        """Legacy bare-Decimal fill checks need an explicit timestamp."""
        with pytest.raises(ValueError, match="timestamp is required"):
            order_manager.check_fills(Decimal("99000"))

    def test_book_touch_falls_back_through_order_manager_bare_decimal(
        self,
        order_manager,
        sample_timestamp,
    ):
        """BOOK_TOUCH degrades to at-limit semantics for bare Decimal input."""
        order_manager.fill_simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)
        order_manager.place_order(
            client_order_id="ltc",
            symbol="LTCUSDT",
            side="Buy",
            price=Decimal("58.60"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        fills = order_manager.check_fills(Decimal("58.60"), timestamp=sample_timestamp)

        assert len(fills) == 1
        assert fills[0].price == Decimal("58.60")
        assert fills[0].side == "Buy"

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


class TestLastCrossOrderManagerIntegration:
    """Feature 0051 integration: advance_market hook in check_fills."""

    @staticmethod
    def _ticker(
        symbol: str,
        last: Decimal,
        *,
        tick_index: int,
    ) -> TickerEvent:
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 5, 11, 4, 49, 58, tzinfo=timezone.utc)
        ts = base + timedelta(milliseconds=tick_index)
        return TickerEvent(
            event_type=EventType.TICKER,
            symbol=symbol,
            exchange_ts=ts,
            local_ts=ts,
            last_price=last,
            mark_price=last,
            bid1_price=Decimal("0"),
            ask1_price=Decimal("0"),
            funding_rate=Decimal("0"),
        )

    @pytest.fixture
    def last_cross_order_manager(self, order_manager):
        order_manager.fill_simulator = TradeThroughFillSimulator(
            mode=FillMode.LAST_CROSS
        )
        return order_manager

    def test_two_orders_same_symbol_tick_both_see_cross(
        self,
        last_cross_order_manager,
        sample_timestamp,
    ):
        """Test #8: advance once per tick; both orders read same prev/curr."""
        last_cross_order_manager.place_order(
            client_order_id="c1",
            symbol="LTCUSDT",
            side="Buy",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        last_cross_order_manager.place_order(
            client_order_id="c2",
            symbol="LTCUSDT",
            side="Buy",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=1,
            timestamp=sample_timestamp,
        )

        ticker_prev = self._ticker("LTCUSDT", Decimal("54.30"), tick_index=0)
        ticker_curr = self._ticker("LTCUSDT", Decimal("54.10"), tick_index=1)
        # T0: stash None, commit 54.30. No fills (prev_last is None).
        fills_prev = last_cross_order_manager.check_fills(ticker_prev)
        assert fills_prev == []
        # T1: stash 54.30, commit 54.10. Both orders observe the cross.
        fills_curr = last_cross_order_manager.check_fills(ticker_curr)

        assert len(fills_curr) == 2
        assert {f.order_link_id for f in fills_curr} == {"c1", "c2"}
        assert all(f.price == Decimal("54.20") for f in fills_curr)

    def test_advance_market_idempotent_for_same_tick(
        self,
        last_cross_order_manager,
        sample_timestamp,
    ):
        """Test #8b: re-stash on same token is a no-op; third order still fills."""
        last_cross_order_manager.place_order(
            client_order_id="c1",
            symbol="LTCUSDT",
            side="Buy",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        last_cross_order_manager.place_order(
            client_order_id="c2",
            symbol="LTCUSDT",
            side="Buy",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=1,
            timestamp=sample_timestamp,
        )
        simulator = last_cross_order_manager.fill_simulator
        ticker_prev = self._ticker("LTCUSDT", Decimal("54.30"), tick_index=0)
        ticker_curr = self._ticker("LTCUSDT", Decimal("54.10"), tick_index=1)
        last_cross_order_manager.check_fills(ticker_prev)
        last_cross_order_manager.check_fills(ticker_curr)

        # Redundant advance on the same TickerEvent must not re-stash.
        simulator.advance_market(ticker_curr)
        assert simulator._prev_last_price["LTCUSDT"] == Decimal("54.10")
        assert simulator._tick_prev_last["LTCUSDT"] == Decimal("54.30")

        # A late-arriving third order on the same tick still sees the cross.
        last_cross_order_manager.place_order(
            client_order_id="c3",
            symbol="LTCUSDT",
            side="Buy",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=2,
            timestamp=sample_timestamp,
        )
        fills = last_cross_order_manager.check_fills(ticker_curr)
        assert len(fills) == 1
        assert fills[0].order_link_id == "c3"

    def test_state_advances_on_ticks_with_no_active_orders(
        self,
        last_cross_order_manager,
        sample_timestamp,
    ):
        """Test #15: advance_market runs unconditionally on orderless ticks."""
        # Three orderless ticks: 54.30, 54.10, 54.10.
        t0 = self._ticker("LTCUSDT", Decimal("54.30"), tick_index=0)
        t1 = self._ticker("LTCUSDT", Decimal("54.10"), tick_index=1)
        t2 = self._ticker("LTCUSDT", Decimal("54.10"), tick_index=2)
        last_cross_order_manager.check_fills(t0)
        last_cross_order_manager.check_fills(t1)
        last_cross_order_manager.check_fills(t2)

        # Place a SELL @ 54.20 after the no-order ticks.
        last_cross_order_manager.place_order(
            client_order_id="c1",
            symbol="LTCUSDT",
            side="Sell",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="short",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # T3: prev=54.10 (committed from orderless t2), curr=54.30 -> SELL fires.
        t3 = self._ticker("LTCUSDT", Decimal("54.30"), tick_index=3)
        fills = last_cross_order_manager.check_fills(t3)

        assert len(fills) == 1
        assert fills[0].order_link_id == "c1"
        assert fills[0].price == Decimal("54.20")
