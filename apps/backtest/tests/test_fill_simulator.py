"""Tests for fill simulator."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gridcore import EventType, TickerEvent

from backtest.fill_simulator import FillMode, TradeThroughFillSimulator
from backtest.order_manager import SimulatedOrder


def _order(
    side: str,
    price: Decimal = Decimal("58.60"),
    *,
    symbol: str = "LTCUSDT",
    order_id: str = "1",
    client_order_id: str = "c1",
) -> SimulatedOrder:
    return SimulatedOrder(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
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
    *,
    tick_index: int = 0,
) -> TickerEvent:
    # Per Feature 0051: every snapshot in a multi-tick test must carry
    # monotonically distinct (exchange_ts, local_ts) values, otherwise the
    # idempotency guard in advance_market keys on the same token and the
    # second call silently no-ops. tick_index offsets both timestamps by N
    # milliseconds so callers can pass tick_index=0 for prev and 1 for curr.
    base = datetime(2026, 5, 11, 4, 49, 58, tzinfo=timezone.utc)
    ts = base + timedelta(milliseconds=tick_index)
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


def _ticker_for(
    symbol: str,
    last: Decimal,
    *,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    tick_index: int = 0,
) -> TickerEvent:
    """Symbol-aware ticker helper for LAST_CROSS per-symbol isolation tests."""
    base = datetime(2026, 5, 11, 4, 49, 58, tzinfo=timezone.utc)
    ts = base + timedelta(milliseconds=tick_index)
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol=symbol,
        exchange_ts=ts,
        local_ts=ts,
        last_price=last,
        mark_price=last,
        bid1_price=bid if bid is not None else Decimal("0"),
        ask1_price=ask if ask is not None else Decimal("0"),
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
                    FillMode.LAST_CROSS: False,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: False,
                    FillMode.BOOK_TOUCH: False,
                    FillMode.LAST_CROSS: False,
                },
            ),
            (
                _ticker(Decimal("58.61"), Decimal("58.61"), Decimal("58.62")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: False,
                    FillMode.BOOK_TOUCH: False,
                    FillMode.LAST_CROSS: False,
                },
                {
                    FillMode.STRICT_CROSS: True,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                    FillMode.LAST_CROSS: False,
                },
            ),
            (
                _ticker(Decimal("58.60"), Decimal("58.59"), Decimal("58.61")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: False,
                    FillMode.LAST_CROSS: False,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: False,
                    FillMode.LAST_CROSS: False,
                },
            ),
            (
                _ticker(Decimal("58.60"), Decimal("58.60"), Decimal("58.60")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                    FillMode.LAST_CROSS: False,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                    FillMode.LAST_CROSS: False,
                },
            ),
            (
                _ticker(Decimal("58.61"), Decimal("58.60"), Decimal("58.60")),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: False,
                    FillMode.BOOK_TOUCH: True,
                    FillMode.LAST_CROSS: False,
                },
                {
                    FillMode.STRICT_CROSS: True,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                    FillMode.LAST_CROSS: False,
                },
            ),
            (
                Decimal("58.60"),
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                    FillMode.LAST_CROSS: False,
                },
                {
                    FillMode.STRICT_CROSS: False,
                    FillMode.TRADE_THROUGH_AT_LIMIT: True,
                    FillMode.BOOK_TOUCH: True,
                    FillMode.LAST_CROSS: False,
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


def _last_cross_simulator() -> TradeThroughFillSimulator:
    return TradeThroughFillSimulator(mode=FillMode.LAST_CROSS)


def _drive_two_ticks(
    simulator: TradeThroughFillSimulator,
    order: SimulatedOrder,
    prev_last: Decimal,
    curr_last: Decimal,
    *,
    symbol: str = "LTCUSDT",
):
    ticker_prev = _ticker_for(symbol, prev_last, tick_index=0)
    ticker_curr = _ticker_for(symbol, curr_last, tick_index=1)
    simulator.advance_market(ticker_prev)
    simulator.advance_market(ticker_curr)
    return simulator.check_fill(order, ticker_curr)


class TestLastCrossFillMode:
    """Transition-based fill checks for FillMode.LAST_CROSS (feature 0051)."""

    def test_no_fill_without_prior_tick(self):
        """First check on a symbol returns False even when curr is past limit."""
        # Test #1: single-snapshot driver — _tick_prev_last stashes None.
        simulator = _last_cross_simulator()
        order = _order("Buy", price=Decimal("54.20"))
        ticker = _ticker_for("LTCUSDT", Decimal("54.10"), tick_index=0)

        simulator.advance_market(ticker)
        result = simulator.check_fill(order, ticker)

        assert result.should_fill is False
        assert result.fill_price == Decimal("0")

    @pytest.mark.parametrize("side", ["Buy", "Sell"])
    @pytest.mark.parametrize(
        "level", [Decimal("54.10"), Decimal("54.20"), Decimal("54.30")]
    )
    def test_sticky_last_price_does_not_trigger_fill(self, side, level):
        """Sticky last_price (prev == curr) never fires in either direction."""
        # Test #2.
        simulator = _last_cross_simulator()
        order = _order(side, price=Decimal("54.20"))

        result = _drive_two_ticks(simulator, order, level, level)

        assert result.should_fill is False

    def test_single_tick_buy_cross(self):
        """prev=54.30 -> curr=54.20 fires BUY @ 54.20 on the second tick."""
        # Test #3.
        simulator = _last_cross_simulator()
        order = _order("Buy", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.30"), Decimal("54.20")
        )

        assert result.should_fill is True
        assert result.fill_price == Decimal("54.20")

    def test_single_tick_sell_cross(self):
        """prev=54.10 -> curr=54.20 fires SELL @ 54.20 on the second tick."""
        # Test #4.
        simulator = _last_cross_simulator()
        order = _order("Sell", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.10"), Decimal("54.20")
        )

        assert result.should_fill is True
        assert result.fill_price == Decimal("54.20")

    def test_multi_tick_buy_cross_gap_down(self):
        """Gap-down past BUY limit fires on the first post-cross tick."""
        # Test #5: prev=54.30 -> curr=54.10, BUY @ 54.20.
        simulator = _last_cross_simulator()
        order = _order("Buy", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.30"), Decimal("54.10")
        )

        assert result.should_fill is True

    def test_multi_tick_sell_cross_gap_up(self):
        """Gap-up past SELL limit fires on the first post-cross tick."""
        # Test #6: prev=54.10 -> curr=54.30, SELL @ 54.20.
        simulator = _last_cross_simulator()
        order = _order("Sell", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.10"), Decimal("54.30")
        )

        assert result.should_fill is True

    def test_wrong_direction_transition_does_not_fill_buy(self):
        """Price moving away from a BUY limit does not fire."""
        # Test #7 (BUY): prev=54.10 -> curr=54.30, BUY @ 54.20 -> no fill.
        simulator = _last_cross_simulator()
        order = _order("Buy", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.10"), Decimal("54.30")
        )

        assert result.should_fill is False

    def test_wrong_direction_transition_does_not_fill_sell(self):
        """Price moving away from a SELL limit does not fire."""
        # Test #7 (SELL): prev=54.30 -> curr=54.10, SELL @ 54.20 -> no fill.
        simulator = _last_cross_simulator()
        order = _order("Sell", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.30"), Decimal("54.10")
        )

        assert result.should_fill is False

    def test_per_symbol_isolation(self):
        """First-ever tick for a fresh symbol must not fill regardless of price."""
        # Test #9.
        simulator = _last_cross_simulator()
        ltc_prev = _ticker_for("LTCUSDT", Decimal("54.30"), tick_index=0)
        ltc_curr = _ticker_for("LTCUSDT", Decimal("54.10"), tick_index=1)
        btc_first = _ticker_for("BTCUSDT", Decimal("54.10"), tick_index=2)
        simulator.advance_market(ltc_prev)
        simulator.advance_market(ltc_curr)
        simulator.advance_market(btc_first)

        ltc_order = _order(
            "Buy",
            price=Decimal("54.20"),
            symbol="LTCUSDT",
        )
        btc_order = _order(
            "Buy",
            price=Decimal("54.20"),
            symbol="BTCUSDT",
            order_id="2",
            client_order_id="c2",
        )

        ltc_result = simulator.check_fill(ltc_order, ltc_curr)
        btc_result = simulator.check_fill(btc_order, btc_first)

        assert ltc_result.should_fill is True
        assert btc_result.should_fill is False

    def test_invalid_last_price_zero_does_not_advance_state(self):
        """curr=0 must not overwrite the committed prior valid prev_last."""
        # Test #10.
        simulator = _last_cross_simulator()
        ticker_t0 = _ticker_for("LTCUSDT", Decimal("54.30"), tick_index=0)
        ticker_t1_bad = _ticker_for("LTCUSDT", Decimal("0"), tick_index=1)
        ticker_t2_good = _ticker_for("LTCUSDT", Decimal("54.10"), tick_index=2)
        simulator.advance_market(ticker_t0)
        simulator.advance_market(ticker_t1_bad)
        simulator.advance_market(ticker_t2_good)

        # State must reflect the original prev=54.30, not poisoned by the
        # zero tick. With committed slot=54.30 at end of t0, t1_bad is a
        # no-op, and t2_good stashes the still-committed 54.30 into the
        # read slot.
        order = _order("Buy", price=Decimal("54.20"))
        result = simulator.check_fill(order, ticker_t2_good)

        assert result.should_fill is True
        assert simulator._prev_last_price["LTCUSDT"] == Decimal("54.10")
        assert simulator._tick_prev_last["LTCUSDT"] == Decimal("54.30")

    def test_legacy_bare_decimal_returns_false(self):
        """Bare Decimal input never fills under LAST_CROSS (no symbol)."""
        # Test #11.
        simulator = _last_cross_simulator()
        order = _order("Buy", price=Decimal("54.20"))

        result = simulator.check_fill(order, Decimal("54.10"))

        assert result.should_fill is False
        # State must remain untouched on the bare-Decimal path.
        assert simulator._prev_last_price == {}
        assert simulator._tick_prev_last == {}
        assert simulator._tick_token == {}

    def test_bare_decimal_does_not_mutate_warm_state(self):
        """Warm per-symbol state survives a subsequent bare-Decimal call."""
        # Pins the "no-state-mutation contract" for mixed input sequences:
        # TickerEvent history must not be corrupted by a later bare-Decimal
        # call. Covers the warm-state path that test #11 leaves open.
        simulator = _last_cross_simulator()
        ticker_prev = _ticker_for("LTCUSDT", Decimal("54.30"), tick_index=0)
        ticker_curr = _ticker_for("LTCUSDT", Decimal("54.10"), tick_index=1)
        simulator.advance_market(ticker_prev)
        simulator.advance_market(ticker_curr)
        snapshot_prev = dict(simulator._prev_last_price)
        snapshot_tick = dict(simulator._tick_prev_last)
        snapshot_token = dict(simulator._tick_token)

        order = _order("Buy", price=Decimal("54.20"))
        result = simulator.check_fill(order, Decimal("54.10"))

        assert result.should_fill is False
        assert simulator._prev_last_price == snapshot_prev
        assert simulator._tick_prev_last == snapshot_tick
        assert simulator._tick_token == snapshot_token

    def test_invalid_side_raises(self):
        """Side outside SideType raises ValueError in the warm-state path."""
        # Test #12.
        simulator = _last_cross_simulator()
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="LTCUSDT",
            side="Invalid",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
        )
        ticker_prev = _ticker_for("LTCUSDT", Decimal("54.30"), tick_index=0)
        ticker_curr = _ticker_for("LTCUSDT", Decimal("54.10"), tick_index=1)
        simulator.advance_market(ticker_prev)
        simulator.advance_market(ticker_curr)

        with pytest.raises(ValueError, match="Invalid order side"):
            simulator.check_fill(order, ticker_curr)

    def test_invalid_side_raises_in_cold_state(self):
        """Side validation runs before the prev_last=None short-circuit."""
        # Pins the fix for the P3 review finding: an invalid side on a
        # first-ever tick (no warmed prev_last) must still raise rather
        # than silently return no-fill.
        simulator = _last_cross_simulator()
        order = SimulatedOrder(
            order_id="1",
            client_order_id="c1",
            symbol="LTCUSDT",
            side="Invalid",
            price=Decimal("54.20"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
        )
        ticker = _ticker_for("LTCUSDT", Decimal("54.10"), tick_index=0)
        simulator.advance_market(ticker)

        with pytest.raises(ValueError, match="Invalid order side"):
            simulator.check_fill(order, ticker)

    def test_buy_strict_prev_at_limit_does_not_fill(self):
        """Pins strict inequality on prev_last for BUY."""
        # Test #13: BUY @ 54.20, prev=54.20, curr=54.10 -> no fill.
        simulator = _last_cross_simulator()
        order = _order("Buy", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.20"), Decimal("54.10")
        )
        assert result.should_fill is False

        # Follow-up tick: prev=54.10 (committed), curr=54.30 -> still no fill.
        ticker_t2 = _ticker_for("LTCUSDT", Decimal("54.30"), tick_index=2)
        simulator.advance_market(ticker_t2)
        result2 = simulator.check_fill(order, ticker_t2)
        assert result2.should_fill is False

    def test_sell_strict_prev_at_limit_does_not_fill(self):
        """Pins strict inequality on prev_last for SELL."""
        # Test #14: SELL @ 54.20, prev=54.20, curr=54.30 -> no fill.
        simulator = _last_cross_simulator()
        order = _order("Sell", price=Decimal("54.20"))

        result = _drive_two_ticks(
            simulator, order, Decimal("54.20"), Decimal("54.30")
        )
        assert result.should_fill is False

        # Follow-up tick: prev=54.30 (committed), curr=54.10 -> still no fill.
        ticker_t2 = _ticker_for("LTCUSDT", Decimal("54.10"), tick_index=2)
        simulator.advance_market(ticker_t2)
        result2 = simulator.check_fill(order, ticker_t2)
        assert result2.should_fill is False
