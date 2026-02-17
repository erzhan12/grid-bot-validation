"""Integration test: GridEngine → IntentExecutor pipeline.

Validates that GridEngine produces valid intents that IntentExecutor
can translate to Bybit API calls (mocked).
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from gridcore.engine import GridEngine
from gridcore.intents import PlaceLimitIntent, CancelIntent

from gridbot.executor import IntentExecutor

from integration_helpers import make_ticker_event


class TestEngineToExecutor:
    """Test GridEngine → Executor intent pipeline."""

    def test_first_ticker_produces_intents(self, grid_config, btcusdt_tick_size):
        """First ticker should build grid and produce PlaceLimitIntents."""
        engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=btcusdt_tick_size,
            config=grid_config,
            strat_id="test_strat",
        )

        event = make_ticker_event("BTCUSDT", 100000.0, datetime(2025, 1, 1, tzinfo=timezone.utc))

        intents = engine.on_event(event)

        # Should produce PlaceLimitIntents for grid levels
        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        assert len(place_intents) > 0

        # All intents should have valid fields
        for intent in place_intents:
            assert intent.symbol == "BTCUSDT"
            assert intent.side in ("Buy", "Sell")
            assert intent.price > 0
            assert intent.client_order_id  # Non-empty
            assert len(intent.client_order_id) == 16  # 16-char hex

    def test_intents_have_valid_client_order_ids(self, grid_config, btcusdt_tick_size):
        """All PlaceLimitIntents should have unique, deterministic client_order_ids."""
        engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=btcusdt_tick_size,
            config=grid_config,
            strat_id="test_strat",
        )

        event = make_ticker_event("BTCUSDT", 100000.0, datetime(2025, 1, 1, tzinfo=timezone.utc))
        intents = engine.on_event(event)

        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        ids = [i.client_order_id for i in place_intents]

        # All IDs unique
        assert len(ids) == len(set(ids))

        # All IDs are 16-char hex strings
        for cid in ids:
            assert len(cid) == 16
            int(cid, 16)  # Should not raise

    def test_buy_and_sell_intents_produced(self, grid_config, btcusdt_tick_size):
        """Grid should produce both Buy and Sell intents around current price."""
        engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=btcusdt_tick_size,
            config=grid_config,
            strat_id="test_strat",
        )

        event = make_ticker_event("BTCUSDT", 100000.0, datetime(2025, 1, 1, tzinfo=timezone.utc))
        intents = engine.on_event(event)

        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        sides = set(i.side for i in place_intents)

        assert "Buy" in sides
        assert "Sell" in sides

    def test_buy_prices_below_sell_prices(self, grid_config, btcusdt_tick_size):
        """All Buy prices should be below all Sell prices."""
        engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=btcusdt_tick_size,
            config=grid_config,
            strat_id="test_strat",
        )

        event = make_ticker_event("BTCUSDT", 100000.0, datetime(2025, 1, 1, tzinfo=timezone.utc))
        intents = engine.on_event(event)

        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        buy_prices = [i.price for i in place_intents if i.side == "Buy"]
        sell_prices = [i.price for i in place_intents if i.side == "Sell"]

        if buy_prices and sell_prices:
            assert max(buy_prices) < min(sell_prices)

    def test_executor_shadow_mode_processes_intents(self, grid_config, btcusdt_tick_size):
        """IntentExecutor in shadow mode should process intents without real API calls."""
        mock_rest = MagicMock()
        executor = IntentExecutor(rest_client=mock_rest, shadow_mode=True)

        engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=btcusdt_tick_size,
            config=grid_config,
            strat_id="test_strat",
        )

        event = make_ticker_event("BTCUSDT", 100000.0, datetime(2025, 1, 1, tzinfo=timezone.utc))
        intents = engine.on_event(event)

        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]

        for intent in place_intents[:3]:  # Test first 3
            result = executor.execute_place(intent)
            assert result.success
            assert result.order_id.startswith("shadow_")

        # No real API calls made
        mock_rest.place_order.assert_not_called()

    def test_second_ticker_no_duplicate_intents(self, grid_config, btcusdt_tick_size):
        """Second ticker at same price should not produce duplicate intents."""
        engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=btcusdt_tick_size,
            config=grid_config,
            strat_id="test_strat",
        )

        ts1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        ts2 = ts1 + timedelta(seconds=1)

        event1 = make_ticker_event("BTCUSDT", 100000.0, ts1)
        intents1 = engine.on_event(event1)

        # Simulate orders being placed (provide them as limit_orders)
        place_intents = [i for i in intents1 if isinstance(i, PlaceLimitIntent)]
        long_orders = []
        short_orders = []
        for i, intent in enumerate(place_intents):
            order = {
                "orderId": f"order_{i}",
                "orderLinkId": intent.client_order_id,
                "price": str(intent.price),
                "qty": "0.001",
                "side": intent.side,
            }
            if intent.direction == "long":
                long_orders.append(order)
            else:
                short_orders.append(order)

        limit_orders = {"long": long_orders, "short": short_orders}

        event2 = make_ticker_event("BTCUSDT", 100000.0, ts2)
        intents2 = engine.on_event(event2, limit_orders=limit_orders)

        # Should produce no new place intents (all orders already placed)
        new_place = [i for i in intents2 if isinstance(i, PlaceLimitIntent)]
        assert len(new_place) == 0


class TestExecutorRESTPayloadMapping:
    """Test IntentExecutor (non-shadow) maps intents to correct REST params."""

    def _build_intents(self, grid_config, btcusdt_tick_size):
        """Helper: build grid and return PlaceLimitIntents."""
        engine = GridEngine(
            symbol="BTCUSDT",
            tick_size=btcusdt_tick_size,
            config=grid_config,
            strat_id="test_strat",
        )
        event = make_ticker_event(
            "BTCUSDT", 100000.0, datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        intents = engine.on_event(event)
        return [i for i in intents if isinstance(i, PlaceLimitIntent)]

    def test_place_intent_maps_to_rest_params(self, grid_config, btcusdt_tick_size):
        """PlaceLimitIntent fields map correctly to place_order kwargs."""
        mock_rest = MagicMock()
        mock_rest.place_order.return_value = {"orderId": "real_order_1"}
        executor = IntentExecutor(rest_client=mock_rest, shadow_mode=False)

        place_intents = self._build_intents(grid_config, btcusdt_tick_size)
        intent = place_intents[0]

        result = executor.execute_place(intent)

        assert result.success
        assert result.order_id == "real_order_1"
        mock_rest.place_order.assert_called_once_with(
            symbol="BTCUSDT",
            side=intent.side,
            order_type="Limit",
            qty=str(intent.qty),
            price=str(intent.price),
            reduce_only=intent.reduce_only,
            position_idx=1 if intent.direction == "long" else 2,
            order_link_id=intent.client_order_id,
        )

    def test_cancel_intent_maps_to_rest_params(self, grid_config, btcusdt_tick_size):
        """CancelIntent fields map correctly to cancel_order kwargs."""
        mock_rest = MagicMock()
        mock_rest.cancel_order.return_value = True
        executor = IntentExecutor(rest_client=mock_rest, shadow_mode=False)

        cancel_intent = CancelIntent(
            symbol="BTCUSDT",
            order_id="order_to_cancel",
            reason="rebuild",
        )

        result = executor.execute_cancel(cancel_intent)

        assert result.success
        mock_rest.cancel_order.assert_called_once_with(
            symbol="BTCUSDT",
            order_id="order_to_cancel",
        )

    def test_reduce_only_flag_propagated(self, grid_config, btcusdt_tick_size):
        """reduce_only=True on intent reaches place_order."""
        mock_rest = MagicMock()
        mock_rest.place_order.return_value = {"orderId": "ro_order"}
        executor = IntentExecutor(rest_client=mock_rest, shadow_mode=False)

        place_intents = self._build_intents(grid_config, btcusdt_tick_size)
        base = place_intents[0]
        # Create a reduce_only variant of the first intent
        intent = PlaceLimitIntent(
            symbol=base.symbol,
            side=base.side,
            price=base.price,
            qty=base.qty,
            direction=base.direction,
            grid_level=base.grid_level,
            client_order_id=base.client_order_id,
            reduce_only=True,
        )

        executor.execute_place(intent)

        call_kwargs = mock_rest.place_order.call_args[1]
        assert call_kwargs["reduce_only"] is True

    def test_position_idx_long_vs_short(self, grid_config, btcusdt_tick_size):
        """Long intents get position_idx=1, short get position_idx=2."""
        mock_rest = MagicMock()
        mock_rest.place_order.return_value = {"orderId": "idx_order"}
        executor = IntentExecutor(rest_client=mock_rest, shadow_mode=False)

        place_intents = self._build_intents(grid_config, btcusdt_tick_size)

        long_intents = [i for i in place_intents if i.direction == "long"]
        short_intents = [i for i in place_intents if i.direction == "short"]

        if long_intents:
            executor.execute_place(long_intents[0])
            call_kwargs = mock_rest.place_order.call_args[1]
            assert call_kwargs["position_idx"] == 1

        mock_rest.reset_mock()

        if short_intents:
            executor.execute_place(short_intents[0])
            call_kwargs = mock_rest.place_order.call_args[1]
            assert call_kwargs["position_idx"] == 2
