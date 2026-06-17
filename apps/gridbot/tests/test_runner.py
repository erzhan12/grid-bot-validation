"""Tests for gridbot strategy runner module."""

from dataclasses import replace
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from unittest.mock import Mock, MagicMock

import pytest

from gridcore import TickerEvent, ExecutionEvent, OrderUpdateEvent, EventType, InstrumentInfo
from gridcore.intents import PlaceLimitIntent, CancelIntent

from gridbot.config import StrategyConfig, SafetyCapsConfig
from gridbot.executor import IntentExecutor, OrderResult, CancelResult
from gridbot.retry_queue import RetryQueue
from gridbot.runner import StrategyRunner, TrackedOrder
from gridbot.safety_caps import SafetyCaps

EMPTY_LIMITS: dict[str, list[dict]] = {'long': [], 'short': []}


def _make_limit_order(intent: PlaceLimitIntent, order_id: str = "test_order") -> dict:
    """Convert a PlaceLimitIntent to the order dict format returned by get_limit_orders()."""
    return {
        'orderId': order_id,
        'orderLinkId': intent.client_order_id,
        'price': str(intent.price),
        'qty': str(intent.qty),
        'side': intent.side,
        'reduceOnly': intent.reduce_only,
    }


@pytest.fixture
def strategy_config():
    """Sample strategy configuration."""
    return StrategyConfig(
        strat_id="btcusdt_test",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.2,
        shadow_mode=False,
    )


@pytest.fixture
def shadow_config():
    """Shadow mode strategy configuration."""
    return StrategyConfig(
        strat_id="btcusdt_shadow",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.2,
        shadow_mode=True,
    )


@pytest.fixture
def mock_executor():
    """Create mock executor."""
    executor = Mock(spec=IntentExecutor)
    executor.shadow_mode = False
    executor.auth_cooldown = False
    executor.execute_place = MagicMock(
        return_value=OrderResult(success=True, order_id="order_123")
    )
    executor.execute_cancel = MagicMock(return_value=CancelResult(success=True))
    return executor


@pytest.fixture
def instrument_info():
    """Sample instrument info for qty rounding."""
    return InstrumentInfo(
        symbol="BTCUSDT",
        qty_step=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
    )


@pytest.fixture
def runner(strategy_config, mock_executor, instrument_info):
    """Create strategy runner with wallet balance set for qty resolution."""
    r = StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
        instrument_info=instrument_info,
    )
    # Set wallet balance so qty_calculator can resolve (x0.001 = 0.1% of wallet)
    r._wallet_balance = Decimal("10000")
    return r


@pytest.fixture
def ticker_event():
    """Sample ticker event."""
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        exchange_ts=datetime.now(UTC),
        local_ts=datetime.now(UTC),
        last_price=Decimal("50000.0"),
        mark_price=Decimal("50000.0"),
        bid1_price=Decimal("49999.0"),
        ask1_price=Decimal("50001.0"),
        funding_rate=Decimal("0.0001"),
    )


class TestTrackedOrder:
    """Tests for TrackedOrder dataclass."""

    def test_create_order(self):
        """Test creating tracked order."""
        order = TrackedOrder(client_order_id="test_123")
        assert order.client_order_id == "test_123"
        assert order.order_id is None
        assert order.status == "pending"

    def test_mark_placed(self):
        """Test marking order as placed."""
        order = TrackedOrder(client_order_id="test_123")
        order.mark_placed("exchange_order_456")

        assert order.order_id == "exchange_order_456"
        assert order.status == "placed"

    def test_mark_filled(self):
        """Test marking order as filled."""
        order = TrackedOrder(client_order_id="test_123")
        order.mark_filled()
        assert order.status == "filled"

    def test_mark_cancelled(self):
        """Test marking order as cancelled."""
        order = TrackedOrder(client_order_id="test_123")
        order.mark_cancelled()
        assert order.status == "cancelled"


class TestStrategyRunnerProperties:
    """Tests for StrategyRunner properties."""

    def test_strat_id(self, runner, strategy_config):
        """Test strat_id property."""
        assert runner.strat_id == strategy_config.strat_id

    def test_symbol(self, runner, strategy_config):
        """Test symbol property."""
        assert runner.symbol == strategy_config.symbol

    def test_shadow_mode(self, runner):
        """Test shadow_mode property."""
        assert runner.shadow_mode is False

    def test_shadow_mode_enabled(self, shadow_config, mock_executor):
        """Test shadow_mode property when enabled."""
        runner = StrategyRunner(
            strategy_config=shadow_config,
            executor=mock_executor,
        )
        assert runner.shadow_mode is True

    def test_low_margin_equal_position_boost_config_wired_true(
        self, strategy_config, mock_executor, instrument_info
    ):
        """StrategyConfig flag is passed to both linked Position risk configs."""
        strategy_config = strategy_config.model_copy(
            update={"increase_same_position_on_low_margin": True}
        )
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )

        assert (
            runner._long_position.risk_config.increase_same_position_on_low_margin
            is True
        )
        assert (
            runner._short_position.risk_config.increase_same_position_on_low_margin
            is True
        )

    def test_low_margin_equal_position_boost_config_defaults_false(self, runner):
        """Default StrategyConfig flag keeps both linked Position risk configs off."""
        assert (
            runner._long_position.risk_config.increase_same_position_on_low_margin
            is False
        )
        assert (
            runner._short_position.risk_config.increase_same_position_on_low_margin
            is False
        )


class TestStrategyRunnerTicker:
    """Tests for ticker event processing."""
    def test_on_ticker_builds_grid(self, runner, ticker_event):
        """Test ticker event builds grid on first call."""
        runner.on_ticker(ticker_event)

        # Grid should be built, intents generated
        assert len(runner.engine.grid.grid) > 0
    def test_on_ticker_executes_intents(self, runner, mock_executor, ticker_event):
        """Test ticker event executes returned intents."""
        runner.on_ticker(ticker_event)

        # Executor should have been called
        assert mock_executor.execute_place.called or mock_executor.execute_cancel.called


class TestStrategyRunnerOrderTracking:
    """Tests for order tracking."""

    def test_get_limit_orders_empty(self, runner):
        """Test getting limit orders when none tracked."""
        orders = runner.get_limit_orders()
        assert orders == {"long": [], "short": []}

    def test_inject_open_orders(self, runner):
        """Test injecting open orders from exchange (keyed by client_order_id)."""
        orders = [
            {"orderId": "exchange_1", "orderLinkId": "link_1",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "exchange_2",
             "price": "51000", "qty": "0.001", "side": "Sell"},
        ]
        runner.inject_open_orders(orders)

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 2
        # Keyed by client_order_id (orderLinkId when present, orderId otherwise)
        assert "link_1" in runner._tracked_orders
        assert "exchange_2" in runner._tracked_orders
        # Findable by order_id via scan
        assert runner._find_tracked_order(None, "exchange_1").client_order_id == "link_1"

    def test_inject_open_orders_with_and_without_order_link_id(self, runner):
        """Orders from old sessions (with orderLinkId) and new sessions (without) coexist."""
        orders = [
            {"orderId": "ex_old", "orderLinkId": "old_link_abc",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "ex_new",
             "price": "51000", "qty": "0.001", "side": "Sell"},
        ]
        runner.inject_open_orders(orders)

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 2

        # Old order keyed by orderLinkId, findable by both paths
        assert runner._find_tracked_order("old_link_abc", None) is not None
        assert runner._find_tracked_order(None, "ex_old") is not None
        assert runner._find_tracked_order("old_link_abc", None).order_id == "ex_old"

        # New order keyed by orderId, findable by order_id index
        assert runner._find_tracked_order(None, "ex_new") is not None
        assert runner._find_tracked_order("ex_new", None) is not None  # client_id == order_id

    @pytest.mark.parametrize("side,reduce_only,expected_direction", [
        ("Buy", False, "long"),
        ("Buy", True, "short"),
        ("Sell", False, "short"),
        ("Sell", True, "long"),
    ])
    def test_inject_open_orders_derives_direction(
        self, runner, side, reduce_only, expected_direction,
    ):
        """Direction is correctly derived from side+reduceOnly for all 4 combinations."""
        orders = [
            {"orderId": "ex_1", "price": "50000", "qty": "0.001",
             "side": side, "reduceOnly": reduce_only},
        ]
        runner.inject_open_orders(orders)

        tracked = runner._find_tracked_order(None, "ex_1")
        assert tracked.intent is not None
        assert tracked.intent.direction == expected_direction

    def test_inject_open_orders_skips_without_order_id(self, runner):
        """Orders without orderId are skipped."""
        orders = [
            {"orderLinkId": "link_only"},
            {"orderId": "exchange_1", "price": "49000", "qty": "0.001", "side": "Buy"},
        ]
        runner.inject_open_orders(orders)

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 1

    def test_inject_open_orders_skips_key_collision(self, runner):
        """Duplicate client_id (e.g. re-injected order) is skipped."""
        orders = [
            {"orderId": "ex_1", "orderLinkId": "link_a",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "ex_2", "orderLinkId": "link_a",  # same orderLinkId
             "price": "50000", "qty": "0.002", "side": "Sell"},
        ]
        runner.inject_open_orders(orders)

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 1  # second skipped

    def test_inject_open_orders_upgrades_failed_order_and_cancels_retry(
        self, strategy_config, mock_executor, instrument_info
    ):
        """Reconcile upgrades ambiguous failures and cancels queued retries."""
        retry_queue = RetryQueue(executor_func=mock_executor.execute_place)
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
            on_intent_failed=lambda intent, error: retry_queue.add(intent, error),
            on_retry_cancel_for_prefix=lambda prefix: retry_queue.cancel_for_prefix(prefix),
        )
        runner._wallet_balance = Decimal("10000")
        mock_executor.execute_place.return_value = OrderResult(
            success=False,
            error="Connection timeout",
        )
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)
        tracked = runner._tracked_orders[intent.client_order_id]
        failed_wire_id = tracked.intent.order_link_id

        assert tracked.status == "failed"
        assert retry_queue.size == 1

        exchange_link_id = f"{intent.client_order_id}-1715170809999"
        runner.inject_open_orders([
            {
                "orderId": "exchange_1",
                "orderLinkId": exchange_link_id,
                "price": "49000.0",
                "qty": "0.001",
                "side": "Buy",
                "reduceOnly": False,
            },
        ])

        tracked = runner._tracked_orders[intent.client_order_id]
        assert tracked.status == "placed"
        assert tracked.order_id == "exchange_1"
        assert tracked.intent.order_link_id == exchange_link_id
        assert tracked.intent.qty == intent.qty
        assert tracked.intent.grid_level == intent.grid_level
        assert tracked.intent.direction == intent.direction
        assert tracked.intent.order_link_id != failed_wire_id
        assert retry_queue.size == 0

        mock_executor.execute_place.reset_mock()
        retry_queue.process_due()
        mock_executor.execute_place.assert_not_called()

    def test_inject_open_orders_upgrade_skips_when_existing_intent_missing(
        self, runner, caplog
    ):
        """Defensive path does not reconstruct intent from open-order payload."""
        prefix = "abc1234567890def"
        runner._tracked_orders[prefix] = TrackedOrder(
            client_order_id=prefix,
            status="failed",
            intent=None,
        )

        with caplog.at_level("WARNING"):
            runner.inject_open_orders([
                {
                    "orderId": "exchange_1",
                    "orderLinkId": f"{prefix}-1715170800000",
                    "price": "49000.0",
                    "qty": "0.001",
                    "side": "Buy",
                    "reduceOnly": False,
                },
            ])

        tracked = runner._tracked_orders[prefix]
        assert tracked.status == "failed"
        assert tracked.order_id is None
        assert "open-order upgrade skipped" in caplog.text

    def test_inject_open_orders_skips_symbol_mismatch(self, runner):
        """Orders with a symbol different from config are skipped."""
        orders = [
            {"orderId": "ex_wrong", "symbol": "ETHUSDT",
             "price": "3000", "qty": "0.01", "side": "Buy"},
            {"orderId": "ex_correct", "symbol": "BTCUSDT",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "ex_no_symbol",
             "price": "51000", "qty": "0.001", "side": "Sell"},
        ]
        runner.inject_open_orders(orders)

        counts = runner.get_tracked_order_count()
        assert counts["placed"] == 2  # ex_wrong skipped
        assert runner._find_tracked_order(None, "ex_wrong") is None
        assert runner._find_tracked_order(None, "ex_correct") is not None
        assert runner._find_tracked_order(None, "ex_no_symbol") is not None

    def test_inject_open_orders_uses_config_symbol(self, runner):
        """Injected orders always use config symbol, not the order's symbol field."""
        orders = [
            {"orderId": "ex_1", "symbol": "BTCUSDT",
             "price": "49000", "qty": "0.001", "side": "Buy"},
            {"orderId": "ex_2",
             "price": "51000", "qty": "0.001", "side": "Sell"},
        ]
        runner.inject_open_orders(orders)

        for tracked in runner._tracked_orders.values():
            assert tracked.intent.symbol == "BTCUSDT"

    # --- orderLinkId suffix handling (gridbot HOTFIX 2026-05-08) ---

    def test_find_tracked_order_strips_suffix_on_lookup(self, runner, mock_executor):
        """Fast-path lookup matches a suffixed order_link_id back to the
        deterministic prefix used as the dict key."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        suffixed = f"{intent.client_order_id}-1715170800000"
        # order_id=None forces fast-path; under the pre-fix code this would
        # miss the dict and the helper had no fallback path here.
        tracked = runner._find_tracked_order(suffixed, None)

        assert tracked is not None
        assert tracked.client_order_id == intent.client_order_id

    def test_find_tracked_order_unsuffixed_still_works(self, runner, mock_executor):
        """Backward-compat: lookup by the unsuffixed client_order_id still
        hits, so pre-hotfix events and tests don't regress."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("51000.0"),
            qty=Decimal("0.001"),
            grid_level=15,
            direction="short",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        tracked = runner._find_tracked_order(intent.client_order_id, None)

        assert tracked is not None
        assert tracked.client_order_id == intent.client_order_id

    def test_inject_open_orders_keys_by_prefix(self, runner):
        """Injected orders with suffixed orderLinkId are keyed by prefix,
        not by the raw suffixed string."""
        suffixed = "abc1234567890def-1715170800000"
        orders = [
            {"orderId": "ex_1", "orderLinkId": suffixed,
             "price": "49000", "qty": "0.001", "side": "Buy"},
        ]
        runner.inject_open_orders(orders)

        assert "abc1234567890def" in runner._tracked_orders
        assert suffixed not in runner._tracked_orders
        assert runner._find_tracked_order(suffixed, None).order_id == "ex_1"

    def test_inject_open_orders_empty_orderlinkid_fallback(self, runner):
        """Explicit empty-string orderLinkId falls back to orderId as the
        dict key (helper collapses "" to None, so `link_prefix or order_id`
        picks the order_id branch). Codifies the contract that "" and
        missing key are treated identically."""
        orders = [
            {"orderId": "ex_empty", "orderLinkId": "",
             "price": "49000", "qty": "0.001", "side": "Buy"},
        ]
        runner.inject_open_orders(orders)

        assert "ex_empty" in runner._tracked_orders
        assert "" not in runner._tracked_orders
        assert runner._tracked_orders["ex_empty"].order_id == "ex_empty"

    def test_inject_open_orders_collision_after_self_place(
        self, runner, mock_executor
    ):
        """Self-placed order followed by inject_open_orders for the same
        logical order (post-hotfix suffixed orderLinkId) collides on the
        prefix and the inject path skips — no duplicate _tracked_orders
        entries.

        This is the actual restart-path bug class flagged on PR #69:
        without prefix normalization, the inject path keys by the suffixed
        form and both entries coexist under different keys.
        """
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)
        assert intent.client_order_id in runner._tracked_orders
        before_count = len(runner._tracked_orders)

        # Bot restarts → REST snapshot returns the same order with the
        # suffixed orderLinkId Bybit echoes back.
        suffixed = f"{intent.client_order_id}-1715170800000"
        orders = [
            {"orderId": "exch_xyz", "orderLinkId": suffixed,
             "price": "49000", "qty": "0.001", "side": "Buy"},
        ]
        runner.inject_open_orders(orders)

        assert len(runner._tracked_orders) == before_count
        assert intent.client_order_id in runner._tracked_orders
        assert suffixed not in runner._tracked_orders

    def test_get_tracked_order_count(self, runner):
        """Test getting tracked order counts."""
        counts = runner.get_tracked_order_count()
        assert counts == {
            "pending": 0,
            "placed": 0,
            "filled": 0,
            "cancelled": 0,
            "failed": 0,
        }


class TestStrategyRunnerExecution:
    """Tests for order execution."""
    def test_execute_place_intent_success(self, runner, mock_executor):
        """Test successful order placement."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        runner._execute_place_intent(intent, EMPTY_LIMITS)

        mock_executor.execute_place.assert_called_once()
        assigned = mock_executor.execute_place.call_args.args[0]
        assert assigned is not intent
        assert assigned.client_order_id == intent.client_order_id
        assert assigned.order_link_id is not None
        assert assigned.order_link_id.startswith(f"{intent.client_order_id}-")
        assert intent.order_link_id is None
        assert intent.client_order_id in runner._tracked_orders
        assert runner._tracked_orders[intent.client_order_id].status == "placed"
        assert runner._tracked_orders[intent.client_order_id].intent == assigned
    def test_execute_place_intent_failure(self, runner, mock_executor):
        """Test failed order placement."""
        mock_executor.execute_place.return_value = OrderResult(
            success=False, error="Rate limited"
        )

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        runner._execute_place_intent(intent, EMPTY_LIMITS)

        assert runner._tracked_orders[intent.client_order_id].status == "failed"
        tracked_intent = runner._tracked_orders[intent.client_order_id].intent
        assert tracked_intent.order_link_id is not None
        assert intent.order_link_id is None

    def test_failed_reemission_reuses_previous_wire_id(
        self, runner, mock_executor, monkeypatch
    ):
        """Fresh engine re-emission after failure does not mint a new suffix."""
        minted: list[str] = []

        def fake_make_order_link_id(client_order_id):
            link_id = f"{client_order_id}-mint-{len(minted)}"
            minted.append(link_id)
            return link_id

        monkeypatch.setattr("gridbot.runner.make_order_link_id", fake_make_order_link_id)
        mock_executor.execute_place.return_value = OrderResult(
            success=False,
            error="Connection timeout",
        )
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        runner._execute_place_intent(intent, EMPTY_LIMITS)
        first_assigned = mock_executor.execute_place.call_args.args[0]
        fresh_reemission = replace(intent)
        runner._execute_place_intent(fresh_reemission, EMPTY_LIMITS)
        second_assigned = mock_executor.execute_place.call_args.args[0]

        assert minted == [first_assigned.order_link_id]
        assert second_assigned is not first_assigned
        assert second_assigned.order_link_id == first_assigned.order_link_id
        assert fresh_reemission.order_link_id is None

    def test_cancelled_reemission_mints_fresh_wire_id(
        self, runner, mock_executor, monkeypatch
    ):
        """New placement after cancel starts a new wire-id lifecycle."""
        minted: list[str] = []

        def fake_make_order_link_id(client_order_id):
            link_id = f"{client_order_id}-mint-{len(minted)}"
            minted.append(link_id)
            return link_id

        monkeypatch.setattr("gridbot.runner.make_order_link_id", fake_make_order_link_id)
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        runner._execute_place_intent(intent, EMPTY_LIMITS)
        first_assigned = mock_executor.execute_place.call_args.args[0]
        runner._tracked_orders[intent.client_order_id].mark_cancelled()

        fresh_reemission = replace(intent)
        runner._execute_place_intent(fresh_reemission, EMPTY_LIMITS)
        second_assigned = mock_executor.execute_place.call_args.args[0]

        assert minted == [
            first_assigned.order_link_id,
            second_assigned.order_link_id,
        ]
        assert second_assigned.order_link_id != first_assigned.order_link_id
        assert fresh_reemission.order_link_id is None

    def test_retry_queue_receives_and_retries_assigned_wire_id(
        self, strategy_config, mock_executor
    ):
        """Failed assigned intent keeps the same wire id through RetryQueue."""
        retry_executor = Mock(return_value=OrderResult(success=True, order_id="retry_1"))
        retry_queue = RetryQueue(
            executor_func=retry_executor,
            initial_backoff_seconds=0.0,
        )
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            on_intent_failed=lambda intent, error: retry_queue.add(intent, error),
        )
        mock_executor.execute_place.return_value = OrderResult(
            success=False,
            error="Connection timeout",
        )
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        runner._execute_place_intent(intent, EMPTY_LIMITS)
        assigned = mock_executor.execute_place.call_args.args[0]
        retry_queue.process_due()

        retry_executor.assert_called_once()
        retried = retry_executor.call_args.args[0]
        assert retried.order_link_id == assigned.order_link_id
    def test_execute_place_intent_duplicate_skipped(self, runner, mock_executor):
        """Test duplicate order placement is skipped."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        # First execution
        runner._execute_place_intent(intent, EMPTY_LIMITS)
        call_count = mock_executor.execute_place.call_count

        # Second execution (duplicate)
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        # Should not have been called again
        assert mock_executor.execute_place.call_count == call_count
    def test_execute_cancel_intent_success(self, runner, mock_executor):
        """Test successful order cancellation."""
        intent = CancelIntent(
            symbol="BTCUSDT",
            order_id="order_to_cancel",
            reason="test",
        )

        runner._execute_cancel_intent(intent)

        mock_executor.execute_cancel.assert_called_once_with(intent)


class TestStrategyRunnerPositionUpdate:
    """Tests for position updates."""
    def test_on_position_update_calculates_ratio(self, runner):
        """Test position update calculates position ratio."""
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}
        short_pos = {"size": "0.5", "avgPrice": "50000", "liqPrice": "60000"}

        runner.on_position_update(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        # Long ratio survives; short's is overwritten by calculate_amount_multiplier
        assert runner._long_position.position_ratio == 2.0  # 1.0 / 0.5
    def test_on_position_update_no_short(self, runner):
        """Test position update with no short position."""
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}

        runner.on_position_update(
            long_position=long_pos,
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        # Long's ratio is overwritten by calculate_amount_multiplier;
        # short's survives since calculate is not called (short_state is None)
        assert runner._short_position.position_ratio == float("inf")
    def test_on_position_update_no_positions(self, runner):
        """Test position update with no positions."""
        runner.on_position_update(
            long_position=None,
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        assert runner._long_position.position_ratio == 1.0
        assert runner._short_position.position_ratio == 1.0
    def test_on_position_update_stores_both_multiplier_keys(self, runner):
        """Test that both Buy and Sell multipliers are stored per direction."""
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}
        short_pos = {"size": "0.5", "avgPrice": "50000", "liqPrice": "60000"}

        runner.on_position_update(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        # Both dicts should have Buy and Sell keys
        long_mult = runner._long_position.get_amount_multiplier()
        short_mult = runner._short_position.get_amount_multiplier()
        assert "Buy" in long_mult
        assert "Sell" in long_mult
        assert "Buy" in short_mult
        assert "Sell" in short_mult
    def test_on_position_update_decimal_float_type_safety(self, runner):
        """Regression: ratio calculation must not raise TypeError when mixing Decimal/float.

        PositionState.size is Decimal but the None-fallback was float 0.0,
        causing 'unsupported operand type(s) for /: float and Decimal'.
        Test all three combos: both exist, only long, only short.
        """
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}
        short_pos = {"size": "0.5", "avgPrice": "50000", "liqPrice": "60000"}

        # Both positions present (Decimal / Decimal)
        runner.on_position_update(
            long_position=long_pos, short_position=short_pos,
            wallet_balance=10000.0, last_close=50000.0,
        )
        # Long ratio survives; short's is overwritten by calculate_amount_multiplier
        assert runner._long_position.position_ratio == 2.0  # 1.0 / 0.5

        # Only long (Decimal / float-fallback)
        runner.on_position_update(
            long_position=long_pos, short_position=None,
            wallet_balance=10000.0, last_close=50000.0,
        )
        # Short's ratio survives since calculate is not called (short_state is None)
        assert runner._short_position.position_ratio == float("inf")

        # Only short (float-fallback / Decimal)
        runner.on_position_update(
            long_position=None, short_position=short_pos,
            wallet_balance=10000.0, last_close=50000.0,
        )
        # Long's ratio survives since calculate is not called (long_state is None)
        # long_size=0.0 / short_size=0.5 = 0.0
        assert runner._long_position.position_ratio == 0.0
    def test_on_position_update_no_positions_keeps_default_multipliers(self, runner):
        """Test multipliers stay at defaults when no positions exist."""
        runner.on_position_update(
            long_position=None,
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        assert runner._long_position.get_amount_multiplier() == {"Buy": 1.0, "Sell": 1.0}
        assert runner._short_position.get_amount_multiplier() == {"Buy": 1.0, "Sell": 1.0}

    def test_on_position_update_without_price_does_not_poison_risk_multipliers(
        self, runner
    ):
        """Startup position refresh may arrive before the first ticker.

        Sizes and wallet balance still need to update, but risk multipliers
        must not be calculated with a fabricated 0.0 market price.
        """
        long_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "40000"}

        runner._long_position.amount_multiplier = {"Buy": 1.25, "Sell": 1.75}
        runner._short_position.amount_multiplier = {"Buy": 0.5, "Sell": 2.0}

        runner.on_position_update(
            long_position=long_pos,
            short_position=None,
            wallet_balance=25000.0,
            last_close=None,
        )

        assert runner._wallet_balance == Decimal("25000.0")
        assert runner._long_position.size == Decimal("1.0")
        assert runner._short_position.size == Decimal("0")
        assert runner._long_position.get_amount_multiplier() == {"Buy": 1.25, "Sell": 1.75}
        assert runner._short_position.get_amount_multiplier() == {"Buy": 0.5, "Sell": 2.0}

    def test_get_amount_multiplier_long(self, runner):
        """Test get_amount_multiplier returns correct value for long direction."""
        runner._long_position.amount_multiplier = {"Buy": 2.0, "Sell": 1.5}

        assert runner.get_amount_multiplier("long", "Buy") == 2.0
        assert runner.get_amount_multiplier("long", "Sell") == 1.5

    def test_get_amount_multiplier_short(self, runner):
        """Test get_amount_multiplier returns correct value for short direction."""
        runner._short_position.amount_multiplier = {"Buy": 1.5, "Sell": 2.0}

        assert runner.get_amount_multiplier("short", "Buy") == 1.5
        assert runner.get_amount_multiplier("short", "Sell") == 2.0

    def test_get_amount_multiplier_raises_on_invalid_side(self, runner):
        """Test get_amount_multiplier raises KeyError for unknown side."""
        with pytest.raises(KeyError):
            runner.get_amount_multiplier("long", "Unknown")
        with pytest.raises(KeyError):
            runner.get_amount_multiplier("short", "Unknown")


class TestBuildPositionStateRealizedPnl:
    """_build_position_state plumbs Bybit cum/curRealisedPnl into PositionState."""

    def test_realised_pnls_parsed(self, runner):
        pos = {"size": "1.0", "avgPrice": "50000",
               "cumRealisedPnl": "123.45", "curRealisedPnl": "5.50"}
        state = runner._build_position_state(pos, 10000.0, "long")
        assert state.cum_realized_pnl == Decimal("123.45")
        assert state.cur_realized_pnl == Decimal("5.50")

    def test_realised_pnls_negative(self, runner):
        pos = {"size": "1.0", "avgPrice": "50000",
               "cumRealisedPnl": "-16.74", "curRealisedPnl": "-2.00"}
        state = runner._build_position_state(pos, 10000.0, "short")
        assert state.cum_realized_pnl == Decimal("-16.74")
        assert state.cur_realized_pnl == Decimal("-2.00")

    def test_realised_pnls_absent_default_zero(self, runner):
        pos = {"size": "1.0", "avgPrice": "50000"}
        state = runner._build_position_state(pos, 10000.0, "long")
        assert state.cum_realized_pnl == Decimal("0")
        assert state.cur_realized_pnl == Decimal("0")

    def test_realised_pnls_empty_string_default_zero(self, runner):
        pos = {"size": "1.0", "avgPrice": "50000",
               "cumRealisedPnl": "", "curRealisedPnl": ""}
        state = runner._build_position_state(pos, 10000.0, "long")
        assert state.cum_realized_pnl == Decimal("0")
        assert state.cur_realized_pnl == Decimal("0")


class TestStrategyRunnerOrderUpdate:
    """Tests for order update events."""
    def test_on_order_update_fills_tracked(self, runner, mock_executor):
        """Test order update marks tracked order as filled."""
        # First place an order
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        # Simulate fill event
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id=intent.client_order_id,
            status="Filled",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
        )

        runner.on_order_update(event)

        assert runner._tracked_orders[intent.client_order_id].status == "filled"
    def test_on_order_update_strips_suffix_end_to_end(
        self, runner, mock_executor
    ):
        """End-to-end lifecycle: place intent → receive OrderUpdateEvent
        whose order_link_id carries the post-hotfix suffix → public
        on_order_update handler routes through _find_tracked_order, which
        strips the suffix and finds the entry by deterministic prefix.

        Pairs the executor-side suffix contract (test_executor.py) with
        the runner-side normalization (test_find_tracked_order_*) into a
        single integration test that exercises the public event path."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        # Bybit echoes back the suffixed orderLinkId on the event stream.
        suffixed = f"{intent.client_order_id}-1715170800000"
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id=suffixed,
            status="Filled",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
        )

        runner.on_order_update(event)

        assert runner._tracked_orders[intent.client_order_id].status == "filled"

    def test_on_order_update_cancels_tracked(self, runner, mock_executor):
        """Test order update marks tracked order as cancelled."""
        # First place an order
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        # Simulate cancel event
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id=intent.client_order_id,
            status="Cancelled",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
        )

        runner.on_order_update(event)

        assert runner._tracked_orders[intent.client_order_id].status == "cancelled"


class TestFindTrackedOrder:
    """Tests for _find_tracked_order (order_link_id and order_id lookup)."""
    def test_order_update_finds_by_order_id(self, runner, mock_executor):
        """Order update finds tracked order by order_id when order_link_id is empty."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000.0"),
            qty=Decimal("0.001"), grid_level=5, direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        # Simulate fill event with empty order_link_id (no orderLinkId sent to Bybit)
        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id="",
            status="Filled",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
        )

        runner.on_order_update(event)

        assert runner._tracked_orders[intent.client_order_id].status == "filled"
    def test_execution_finds_by_order_id(self, runner, mock_executor):
        """Execution event finds tracked order by order_id when order_link_id is empty."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000.0"),
            qty=Decimal("0.001"), grid_level=5, direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id="",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
            exec_id="exec_1",
            closed_size=Decimal("0"),
        )

        runner.on_execution(event)

        assert runner._tracked_orders[intent.client_order_id].status == "filled"
    def test_injected_order_found_by_order_id(self, runner, mock_executor):
        """Injected orders (keyed by orderId) are found via order_id lookup."""
        runner.inject_open_orders([
            {"orderId": "exch_1", "price": "49000", "qty": "0.001", "side": "Buy"},
        ])

        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="exch_1",
            order_link_id="",
            status="Cancelled",
            side="Buy",
            price=Decimal("49000"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0"),
        )

        runner.on_order_update(event)

        assert runner._tracked_orders["exch_1"].status == "cancelled"


class TestUnknownOrderCallback:
    """Tests for the on_unknown_order callback (mid-run manual order detection)."""

    def _make_event(self, status: str, order_id: str = "manual_1"):
        return OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id=order_id,
            order_link_id="",
            status=status,
            side="Buy",
            price=Decimal("49000"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0.001"),
        )

    def test_untracked_new_triggers_callback(self, strategy_config, mock_executor):
        callback = Mock()
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            on_unknown_order=callback,
        )

        runner.on_order_update(self._make_event("New"))

        callback.assert_called_once_with(strategy_config.strat_id)

    def test_untracked_cancelled_does_not_trigger(self, strategy_config, mock_executor):
        """Status guard: tail Cancelled events for foreign orders must not trigger sync."""
        callback = Mock()
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            on_unknown_order=callback,
        )

        runner.on_order_update(self._make_event("Cancelled"))
        runner.on_order_update(self._make_event("Filled"))

        callback.assert_not_called()

    def test_tracked_new_does_not_trigger(self, runner):
        """Bot's own placed orders find a tracked entry → callback never fires."""
        callback = Mock()
        runner._on_unknown_order = callback

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000.0"),
            qty=Decimal("0.001"), grid_level=5, direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)

        event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_123",
            order_link_id=intent.client_order_id,
            status="New",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            leaves_qty=Decimal("0.001"),
        )
        runner.on_order_update(event)

        callback.assert_not_called()

    def test_no_callback_configured_is_safe(self, strategy_config, mock_executor):
        """When no callback is wired, untracked-New events must not raise."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
        )
        runner.on_order_update(self._make_event("New"))


class TestStrategyRunnerFailureCallback:
    """Tests for failure callback."""
    def test_on_intent_failed_called(self, strategy_config, mock_executor):
        """Test failure callback is called on execution failure."""
        callback = Mock()
        mock_executor.execute_place.return_value = OrderResult(
            success=False, error="Network error"
        )

        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            on_intent_failed=callback,
        )

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.001"),
            grid_level=5,
            direction="long",
        )

        runner._execute_place_intent(intent, EMPTY_LIMITS)

        callback.assert_called_once()
        failed_intent, error = callback.call_args.args
        assert error == "Network error"
        assert failed_intent is not intent
        assert failed_intent.client_order_id == intent.client_order_id
        assert failed_intent.order_link_id is not None
        assert intent.order_link_id is None


class TestQtyResolution:
    """Tests for order qty resolution (engine emits qty=0, runner fills it in)."""

    def test_resolve_qty_wallet_fraction(self, runner):
        """x0.001 amount with wallet=10000 and price=50000 → 10000*0.001/50000 = 0.0002 → rounds to 0.001."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # 10000 * 0.001 / 50000 = 0.0002, rounded up to qty_step=0.001
        assert resolved.qty == Decimal("0.001")

    def test_resolve_qty_fixed_usdt(self, strategy_config, mock_executor, instrument_info):
        """Fixed USDT amount: '100' with price=50000 → 100/50000 = 0.002."""
        strategy_config.amount = "100"
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        runner._wallet_balance = Decimal("10000")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        assert resolved.qty == Decimal("0.002")

    def test_resolve_qty_fixed_usdt_yields_expected_base(self, strategy_config, mock_executor, instrument_info):
        """Fixed USDT amount '250' at price 50000 → 0.005 base qty."""
        strategy_config.amount = "250"
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        runner._wallet_balance = Decimal("10000")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        assert resolved.qty == Decimal("0.005")

    def test_resolve_qty_applies_risk_multiplier_and_rerounds(self, runner):
        """Risk multiplier scales the base qty, then re-rounds to qty_step."""
        # Set a multiplier of 0.5 for long Buy
        runner._long_position.amount_multiplier = {"Buy": 0.5, "Sell": 1.0}

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # base=0.001 * 0.5 = 0.0005, re-rounded UP to qty_step=0.001
        assert resolved.qty == Decimal("0.001")

    def test_resolve_qty_skips_nonzero(self, runner):
        """Intent with qty>0 passes through unchanged."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0.123"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        assert resolved.qty == Decimal("0.123")

    def test_resolve_qty_zero_wallet_returns_zero(self, strategy_config, mock_executor, instrument_info):
        """With wallet=0, qty resolves to 0."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        # wallet_balance defaults to 0

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        assert resolved.qty == Decimal("0")

    def test_resolve_qty_no_instrument_info(self, strategy_config, mock_executor):
        """Without instrument_info, qty is unrounded."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=None,
        )
        runner._wallet_balance = Decimal("10000")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # 10000 * 0.001 / 50000 = 0.0002 (no rounding applied)
        assert resolved.qty == Decimal("0.0002")

    def test_invalid_amount_string_raises(self, strategy_config, mock_executor, instrument_info):
        """Invalid amount string raises ValueError at construction."""
        strategy_config.amount = "abc"
        with pytest.raises(ValueError, match="invalid amount string"):
            StrategyRunner(
                strategy_config=strategy_config,
                executor=mock_executor,
                instrument_info=instrument_info,
            )
    def test_execute_place_skips_zero_qty(self, strategy_config, mock_executor, instrument_info):
        """Orders with resolved qty=0 are not sent to exchange."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        # wallet_balance=0 -> qty=0 -> order skipped

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)
        mock_executor.execute_place.assert_not_called()
    def test_wallet_balance_updated_on_position_update(self, runner):
        """on_position_update stores wallet_balance for qty computation."""
        runner.on_position_update(
            long_position=None,
            short_position=None,
            wallet_balance=25000.0,
            last_close=50000.0,
        )
        assert runner._wallet_balance == Decimal("25000.0")


class TestResolveQtyExtended:
    """Extended tests for _resolve_qty covering multipliers, min/max clamping, and edge cases."""

    def test_multiplier_1_5(self, strategy_config, mock_executor, instrument_info):
        """Risk multiplier 1.5 scales qty and re-rounds."""
        strategy_config.amount = "100"
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        runner._wallet_balance = Decimal("10000")
        runner._long_position.amount_multiplier = {"Buy": 1.5, "Sell": 1.0}

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # base = 100/50000 = 0.002, * 1.5 = 0.003
        assert resolved.qty == Decimal("0.003")

    def test_multiplier_2_0(self, strategy_config, mock_executor, instrument_info):
        """Risk multiplier 2.0 doubles qty."""
        strategy_config.amount = "100"
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        runner._wallet_balance = Decimal("10000")
        runner._long_position.amount_multiplier = {"Buy": 2.0, "Sell": 1.0}

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # base = 100/50000 = 0.002, * 2.0 = 0.004
        assert resolved.qty == Decimal("0.004")

    def test_multiplier_0_5_rerounds(self, strategy_config, mock_executor, instrument_info):
        """Multiplier 0.5 halves qty, then re-rounds up to qty_step."""
        strategy_config.amount = "150"  # 150/50000 = 0.003 base qty
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        runner._wallet_balance = Decimal("10000")
        runner._long_position.amount_multiplier = {"Buy": 0.5, "Sell": 1.0}

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # base = 0.003, * 0.5 = 0.0015, re-rounds up to 0.002
        assert resolved.qty == Decimal("0.002")

    def test_min_qty_clamping(self, strategy_config, mock_executor):
        """Qty below min_qty returns 0."""
        info = InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.01"),  # high min_qty
            max_qty=Decimal("1000"),
        )
        strategy_config.amount = "50"  # 50/50000 = 0.001 base qty
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=info,
        )
        runner._wallet_balance = Decimal("10000")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # 0.001 < min_qty 0.01 → qty=0
        assert resolved.qty == Decimal("0")

    def test_max_qty_clamping(self, strategy_config, mock_executor):
        """Qty above max_qty is clamped."""
        info = InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("0.005"),  # low max_qty
        )
        strategy_config.amount = "500"  # 500/50000 = 0.01 base qty
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=info,
        )
        runner._wallet_balance = Decimal("10000")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        # 0.01 > max_qty 0.005 → clamped to 0.005
        assert resolved.qty == Decimal("0.005")

    def test_inf_multiplier_returns_zero(self, strategy_config, mock_executor, instrument_info):
        """Infinite multiplier returns qty=0."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        runner._wallet_balance = Decimal("10000")
        runner._long_position.amount_multiplier = {"Buy": float("inf"), "Sell": 1.0}

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        assert resolved.qty == Decimal("0")

    def test_nan_multiplier_returns_zero(self, strategy_config, mock_executor, instrument_info):
        """NaN multiplier returns qty=0."""
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        runner._wallet_balance = Decimal("10000")
        runner._long_position.amount_multiplier = {"Buy": float("nan"), "Sell": 1.0}

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        resolved = runner._resolve_qty(intent)
        assert resolved.qty == Decimal("0")

    def test_zero_wallet_logs_debug_not_warning(self, strategy_config, mock_executor, instrument_info, caplog):
        """When wallet_balance=0, qty=0 log is DEBUG, not WARNING."""
        import logging
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        # wallet_balance defaults to 0

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )
        with caplog.at_level(logging.DEBUG):
            runner._resolve_qty(intent)

        qty_zero_records = [r for r in caplog.records if "Resolved qty=0" in r.message]
        assert len(qty_zero_records) == 1
        assert qty_zero_records[0].levelno == logging.DEBUG


class TestEarlyImbalanceMultiplier:
    """Tests for the early-imbalance qty multiplier (bbu2 ref: bybit_api_usdt.py:257-261).

    Asymmetric trigger: fires only when long dominates short
    (1.1 < ratio < 10) AND both positions are pre-liquidation
    (liq_price == 0). No symmetric short-dominant mirror.
    """

    def _make_intent(self):
        return PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )

    def _set_size_ratio(self, runner, long_size: str, short_size: str):
        """Set sizes (NOT position_ratio) — bbu2 keys early_imbalance on size."""
        runner._long_position.size = Decimal(long_size)
        runner._short_position.size = Decimal(short_size)

    def test_default_multiplier_is_no_op(self, runner):
        """Default early_imbalance_multiplier=1.0 → qty unchanged regardless of state."""
        self._set_size_ratio(runner, "2", "1")  # size_ratio = 2.0
        runner._long_position.liquidation_price = Decimal("0")
        runner._short_position.liquidation_price = Decimal("0")

        resolved = runner._resolve_qty(self._make_intent())
        # base = 10000 * 0.001 / 50000 = 0.0002 → round_up to 0.001
        assert resolved.qty == Decimal("0.001")

    def test_fires_when_long_dominates_and_both_pre_liquidation(self, strategy_config, mock_executor, instrument_info):
        """size_ratio=2.0, both liq=0, mult=1.5 → qty scaled by 1.5 before round_qty."""
        strategy_config.early_imbalance_multiplier = 1.5
        strategy_config.amount = "100"  # base = 100/50000 = 0.002
        r = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        r._wallet_balance = Decimal("10000")
        self._set_size_ratio(r, "2", "1")
        r._long_position.liquidation_price = Decimal("0")
        r._short_position.liquidation_price = Decimal("0")

        resolved = r._resolve_qty(self._make_intent())
        # base 0.002 * mult 1.0 (no risk) * early 1.5 = 0.003
        assert resolved.qty == Decimal("0.003")

    def test_no_op_when_ratio_out_of_band(self, strategy_config, mock_executor, instrument_info):
        """size_ratio=11 (above 10) → multiplier suppressed even with mult=1.5."""
        strategy_config.early_imbalance_multiplier = 1.5
        strategy_config.amount = "100"
        r = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        r._wallet_balance = Decimal("10000")
        self._set_size_ratio(r, "11", "1")
        r._long_position.liquidation_price = Decimal("0")
        r._short_position.liquidation_price = Decimal("0")

        resolved = r._resolve_qty(self._make_intent())
        # base 0.002, no early-imb boost
        assert resolved.qty == Decimal("0.002")

    def test_no_op_when_long_liq_price_set(self, strategy_config, mock_executor, instrument_info):
        """long.liq_price > 0 → not pre-liquidation, multiplier suppressed."""
        strategy_config.early_imbalance_multiplier = 1.5
        strategy_config.amount = "100"
        r = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        r._wallet_balance = Decimal("10000")
        self._set_size_ratio(r, "2", "1")
        r._long_position.liquidation_price = Decimal("45000")
        r._short_position.liquidation_price = Decimal("0")

        resolved = r._resolve_qty(self._make_intent())
        assert resolved.qty == Decimal("0.002")

    def test_no_op_when_short_empty_size_ratio_inf(self, strategy_config, mock_executor, instrument_info):
        """short.size=0, long.size>0 → size_ratio=inf, out of band (10), no-op.

        Regression guard: catches a bug that flipped the inf branch to 1.0
        (which would put us in the `1.0 < 1.1` band — also no-op by accident,
        but for the wrong reason). Strict `< 10` keeps inf out.
        """
        strategy_config.early_imbalance_multiplier = 1.5
        strategy_config.amount = "100"
        r = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        r._wallet_balance = Decimal("10000")
        self._set_size_ratio(r, "2", "0")  # short empty → ratio = inf
        r._long_position.liquidation_price = Decimal("0")
        r._short_position.liquidation_price = Decimal("0")

        resolved = r._resolve_qty(self._make_intent())
        assert resolved.qty == Decimal("0.002")

    def test_uses_size_not_margin_ratio(self, strategy_config, mock_executor, instrument_info):
        """Regression: must read sizes, not Position.position_ratio (which is margin-based after calculate_amount_multiplier)."""
        strategy_config.early_imbalance_multiplier = 1.5
        strategy_config.amount = "100"
        r = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )
        r._wallet_balance = Decimal("10000")
        # Sizes are EQUAL (size_ratio=1.0, out of band) but margin-ratio
        # field set to 2.0. With the bug, code reads margin_ratio=2.0 and
        # incorrectly fires. Fixed code reads sizes → 1.0 → no-op.
        self._set_size_ratio(r, "1", "1")
        r._long_position.position_ratio = 2.0  # poisoned
        r._short_position.position_ratio = 2.0
        r._long_position.liquidation_price = Decimal("0")
        r._short_position.liquidation_price = Decimal("0")

        resolved = r._resolve_qty(self._make_intent())
        assert resolved.qty == Decimal("0.002")

    def test_end_to_end_through_on_position_update(self, strategy_config, mock_executor, instrument_info):
        """End-to-end: drive on_position_update with realistic exchange dicts,
        then call _resolve_qty. Proves the plumbing wires Position.size and
        Position.liquidation_price correctly even after calculate_amount_multiplier
        runs and overwrites position_ratio.

        Critical setup: long entry $25k, short entry $50k. With size_ratio=2.0
        (in band), margin_ratio = 2 * (25000/50000) = 1.0 (OUT of band).
        If the implementation regressed to read position_ratio, the multiplier
        would NOT fire. Reading sizes (correct), it fires.
        """
        strategy_config.early_imbalance_multiplier = 1.5
        strategy_config.amount = "100"
        r = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
        )

        # Both positions pre-liquidation (liqPrice=0). Different entry prices
        # so size_ratio diverges from margin_ratio.
        long_pos = {"size": "2.0", "avgPrice": "25000", "liqPrice": "0"}
        short_pos = {"size": "1.0", "avgPrice": "50000", "liqPrice": "0"}
        r.on_position_update(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=50000.0,
        )

        # Plumbing assertion: Position.size and liquidation_price set via real path.
        assert r._long_position.size == Decimal("2.0")
        assert r._short_position.size == Decimal("1.0")
        assert r._long_position.liquidation_price == Decimal("0")
        assert r._short_position.liquidation_price == Decimal("0")

        # Capture the position-rules multiplier so the test isolates early_imb.
        strategy_mult = Decimal(str(r.get_amount_multiplier("long", "Buy")))
        # Expected: base * strategy_mult * early_imb, then round_qty (ceil to 0.001)
        # base = 100 / 50000 = 0.002
        from decimal import ROUND_UP
        expected_pre_round = Decimal("0.002") * strategy_mult * Decimal("1.5")
        steps = (expected_pre_round / Decimal("0.001")).to_integral_value(rounding=ROUND_UP)
        expected_qty = steps * Decimal("0.001")

        resolved = r._resolve_qty(self._make_intent())
        assert resolved.qty == expected_qty
        # Sanity: with mult=1.5 active, qty must exceed the no-boost result.
        assert resolved.qty > Decimal("0.002") * strategy_mult


class TestSameOrderDetection:
    """Tests for same-order detection (bbu2-style safety check)."""

    def test_same_order_error_initial_state(self, runner):
        """Test same_order_error is False initially."""
        assert runner.same_order_error is False

    def test_no_error_with_single_execution(self, runner):
        """Test no error detected with single execution."""
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Opening position
        )

        runner._check_same_orders(event)

        assert runner.same_order_error is False

    def test_no_error_with_different_prices(self, runner):
        """Test no error when executions are at different prices."""
        # First execution
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Second execution at different price
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("49000.0"),  # Different price
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is False

    def test_no_error_with_same_order_id_partial_fills(self, runner):
        """Test no error when same order_id fills multiple times (partial fills)."""
        # First partial fill
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",  # Same order ID
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Second partial fill (same order ID, same price)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_1",  # Same order ID - partial fill OK
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is False

    def test_error_detected_with_different_order_ids_same_price(self, runner):
        """Test error detected when different orders fill at same price."""
        # First execution
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Second execution - DIFFERENT order_id but SAME price = ERROR
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",  # Different order ID
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),  # Same price = duplicate!
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True

    def test_no_error_different_sides(self, runner):
        """Test no error when same price but different sides."""
        # Buy execution
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Opening long
        )
        runner._check_same_orders(event1)

        # Sell execution - same price but different side (and direction)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Sell",
            price=Decimal("50000.0"),  # Same price
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Opening short - goes to short buffer
        )
        runner._check_same_orders(event2)

        # Different buffers (long vs short), so no error
        assert runner.same_order_error is False

    def test_reset_same_order_error(self, runner):
        """Test reset_same_order_error clears flag and buffers."""
        # Trigger error
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True
        assert len(runner._recent_executions_long) == 2

        # Reset
        runner.reset_same_order_error()

        assert runner.same_order_error is False
        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 0

    def test_buffer_max_length(self, runner):
        """Test execution buffer keeps only last 2 entries (matches bbu2 [:2])."""
        for i in range(4):
            event = ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol="BTCUSDT",
                exchange_ts=datetime.now(UTC),
                local_ts=datetime.now(UTC),
                exec_id=f"exec_{i}",
                order_id=f"order_{i}",
                order_link_id=f"link_{i}",
                side="Buy",
                price=Decimal(str(50000 + i * 100)),  # Different prices
                qty=Decimal("0.1"),
                fee=Decimal("0.5"),
                closed_pnl=Decimal("0"),
            )
            runner._check_same_orders(event)

        # Buffer should only keep 2 entries
        assert len(runner._recent_executions_long) == 2

    def test_direction_separation_long(self, runner):
        """Test executions go to correct buffer - long direction."""
        # Buy opening (long buffer)
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Not closing = opening long
        )
        runner._check_same_orders(event)

        assert len(runner._recent_executions_long) == 1
        assert len(runner._recent_executions_short) == 0

    def test_direction_separation_short(self, runner):
        """Test executions go to correct buffer - short direction."""
        # Sell opening (short buffer)
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Sell",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),  # Not closing = opening short
        )
        runner._check_same_orders(event)

        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 1

    def test_closing_long_goes_to_long_buffer(self, runner):
        """Test Sell with closed_size (closing long) goes to long buffer."""
        # Sell closing long position
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Sell",
            price=Decimal("51000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("100.0"),
            closed_size=Decimal("0.1"),  # Non-zero = closing position
        )
        runner._check_same_orders(event)

        # Sell with closed_size != 0 = closing long = goes to long buffer
        assert len(runner._recent_executions_long) == 1
        assert len(runner._recent_executions_short) == 0

    def test_closing_short_goes_to_short_buffer(self, runner):
        """Test Buy with closed_size (closing short) goes to short buffer."""
        # Buy closing short position
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("100.0"),
            closed_size=Decimal("0.1"),  # Non-zero = closing position
        )
        runner._check_same_orders(event)

        # Buy with closed_size != 0 = closing short = goes to short buffer
        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 1

    def test_same_order_error_auto_clears_on_clean_execution(self, runner):
        """Test error auto-clears when a new fill at a different price arrives.

        Matches bbu2 behavior: only the 2 most recent fills per side are
        compared ([:2]). One clean fill at a different price pushes the
        older problematic entry out of the 2-entry buffer, clearing the error.
        """
        # Trigger error: two different orders at same price
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),  # Same price, different order = ERROR
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)
        assert runner.same_order_error is True

        # One clean fill at a different price clears the error
        # Buffer becomes [49000, 50000(order_2)] - different prices, no error
        event3 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_3",
            order_id="order_3",
            order_link_id="link_3",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event3)

        assert runner.same_order_error is False

    def test_opposite_side_fill_does_not_clear_error(self, runner):
        """Test that a fill on the opposite side does NOT clear a same-order error.

        Regression test: _check_same_orders must check BOTH buffers (like bbu2).
        If only the current side's buffer is checked, a clean long fill would
        reset the flag and silently clear a short-side error.
        """
        # Trigger error on the SHORT side (Sell + not closing = opening short)
        short_event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_s1",
            order_id="order_s1",
            order_link_id="short1",
            side="Sell",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(short_event1)

        short_event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_s2",
            order_id="order_s2",  # Different order ID
            order_link_id="short2",
            side="Sell",
            price=Decimal("50000.0"),  # Same price = ERROR
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(short_event2)
        assert runner.same_order_error is True

        # Now a clean LONG fill arrives (Buy + not closing = opening long)
        long_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_l1",
            order_id="order_l1",
            order_link_id="long1",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(long_event)

        # Error must still be active - the short-side error is not cleared
        assert runner.same_order_error is True

    def test_on_ticker_skips_intents_but_updates_engine_when_error(self, runner):
        """Test on_ticker still passes event to engine but skips intent execution.

        Engine must always see ticker events to keep last_close fresh,
        but no orders are placed while same-order error is active.
        """
        # Force same-order error
        runner._same_order_error = True

        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal("50000.0"),
        )

        # Mock _execute_intents to verify it's not called
        execute_called = False

        def mock_execute(intents, limits):
            nonlocal execute_called
            execute_called = True

        runner._execute_intents = mock_execute

        runner.on_ticker(ticker)

        # Intents must not have been executed
        assert execute_called is False
        # Error still active
        assert runner.same_order_error is True

    def test_on_execution_updates_grid_but_skips_intents_when_error(self, runner):
        """Test on_execution passes event to engine but skips intent execution when error."""

        # First, trigger same-order error via two duplicate fills
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)
        assert runner.same_order_error is True

        # Now process another execution through on_execution
        # Engine will get the event (grid update) but intents should not execute
        event3 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_3",
            order_id="order_3",
            order_link_id="ghi789",
            side="Buy",
            price=Decimal("50000.0"),  # Same price keeps error active
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )

        # Mock _execute_intents to verify it's not called
        execute_called = False
        original_execute = runner._execute_intents

        def mock_execute(intents, limits):
            nonlocal execute_called
            execute_called = True
            original_execute(intents)

        runner._execute_intents = mock_execute

        runner.on_execution(event3)

        # Error should still be active (3 consecutive same-price fills)
        assert runner.same_order_error is True
        # _execute_intents should not have been called
        assert execute_called is False

    def test_same_order_error_sends_telegram_alert(self, runner):
        """Test Telegram notification is sent when same-order error detected."""
        mock_notifier = Mock()
        runner._notifier = mock_notifier

        # Trigger error
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True
        mock_notifier.alert.assert_called_once()
        call_args = mock_notifier.alert.call_args
        assert "SAME ORDER ERROR" in call_args[0][0]
        assert "50000.0" in call_args[0][0]

    def test_on_order_update_skips_intents_when_same_order_error(self, runner):
        """Test on_order_update does not execute intents when same-order error active."""
        runner._same_order_error = True

        order_event = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            order_id="order_1",
            order_link_id="abc123",
            status="Filled",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            leaves_qty=Decimal("0"),
        )

        execute_called = False
        original_execute = runner._execute_intents

        def mock_execute(intents, limits):
            nonlocal execute_called
            execute_called = True
            original_execute(intents)

        runner._execute_intents = mock_execute

        runner.on_order_update(order_event)

        assert runner.same_order_error is True
        assert execute_called is False

    def test_partial_fill_skipped_from_buffer(self, runner):
        """Test partial fills (leavesQty != 0) are not added to buffer.

        Matches bbu2 handle_execution filter: leavesQty == '0'.
        Only fully filled orders enter the same-order detection buffer.
        """
        # Partial fill (leavesQty > 0) should NOT enter buffer
        partial_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partially filled
        )
        runner._check_same_orders(partial_event)

        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 0

    def test_partial_fill_does_not_trigger_same_order_error(self, runner):
        """Test that partial fills don't cause false same-order detection.

        Two partial fills at the same price with different order IDs should
        NOT trigger same-order error because they are filtered out.
        """
        # First partial fill
        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partial
        )
        runner._check_same_orders(event1)

        # Second partial fill - different order, same price
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partial
        )
        runner._check_same_orders(event2)

        # Neither entered the buffer, no error
        assert runner.same_order_error is False
        assert len(runner._recent_executions_long) == 0

    def test_fully_filled_enters_buffer_partial_does_not(self, runner):
        """Test mixed: fully filled enters buffer, partial does not."""
        # Fully filled (leavesQty == 0) enters buffer
        full_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0"),  # Fully filled
        )
        runner._check_same_orders(full_event)
        assert len(runner._recent_executions_long) == 1

        # Partial fill (leavesQty > 0) does NOT enter buffer
        partial_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.05"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0.05"),  # Partial - skipped
        )
        runner._check_same_orders(partial_event)

        # Buffer still has only 1 entry (the fully filled one)
        assert len(runner._recent_executions_long) == 1
        assert runner.same_order_error is False

    def test_same_order_skips_when_fills_far_apart(self, runner, caplog):
        """Sequential grid-walk replacement: two same-price fills 10 minutes apart.

        This is the production failure mode that feature 0025 fixes.
        Without the time-window guard, this fired SAME ORDER ERROR and
        blocked placement on every legitimate grid replacement.
        """
        import logging

        t0 = datetime.now(UTC)

        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t0,
            local_ts=t0,
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Second fill at the same price, 10 minutes later — legitimate
        # grid-walk replacement, not a duplicate.
        t1 = t0 + timedelta(minutes=10)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t1,
            local_ts=t1,
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        with caplog.at_level(logging.ERROR):
            runner._check_same_orders(event2)

        assert runner.same_order_error is False
        same_order_errors = [r for r in caplog.records if "SAME ORDER ERROR" in r.message]
        assert same_order_errors == []

    def test_same_order_fires_when_fills_within_window(self, runner):
        """Concurrent grid duplication: two same-price fills 2 seconds apart.

        Within the 5-second window, the detector still triggers — preserving
        the original bbu2 protection against the bug it was designed to catch.
        """
        t0 = datetime.now(UTC)

        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t0,
            local_ts=t0,
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        t1 = t0 + timedelta(seconds=2)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t1,
            local_ts=t1,
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True

    def test_same_order_fires_for_tracked_orders_placed_together_but_filled_later(self, runner):
        """Duplicate resting orders can fill far apart in thin markets.

        The safety check must key off placement proximity when tracking data is
        available. Otherwise two concurrently placed duplicates at the same
        price can evade detection just because the market fills them more than
        five seconds apart.
        """
        t0 = datetime.now(UTC)

        intent1 = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            grid_level=1,
            direction="long",
        )
        intent2 = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            grid_level=1,
            direction="long",
        )
        # Force distinct client IDs to simulate the duplicate-order bug that
        # bypassed deterministic intent identity and reached the exchange.
        tracked1 = TrackedOrder(
            client_order_id="link_1",
            order_id="order_1",
            intent=intent1,
            status="placed",
            placed_ts=t0,
        )
        tracked2 = TrackedOrder(
            client_order_id="link_2",
            order_id="order_2",
            intent=intent2,
            status="placed",
            placed_ts=t0 + timedelta(seconds=1),
        )
        runner._tracked_orders["link_1"] = tracked1
        runner._tracked_orders["link_2"] = tracked2

        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t0,
            local_ts=t0,
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="link_1",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        # Fill arrives after the old 5s fill-time window, but both orders were
        # placed together and were concurrently resting at the same grid slot.
        t1 = t0 + timedelta(seconds=30)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t1,
            local_ts=t1,
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="link_2",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True

    def test_same_order_skips_tracked_orders_placed_far_apart(self, runner):
        """Tracked sequential replacements use placement time to avoid false positives."""
        t0 = datetime.now(UTC)

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            grid_level=1,
            direction="long",
        )
        runner._tracked_orders["link_1"] = TrackedOrder(
            client_order_id="link_1",
            order_id="order_1",
            intent=intent,
            status="placed",
            placed_ts=t0,
        )
        runner._tracked_orders["link_2"] = TrackedOrder(
            client_order_id="link_2",
            order_id="order_2",
            intent=intent,
            status="placed",
            placed_ts=t0 + timedelta(minutes=10),
        )

        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t0 + timedelta(minutes=10),
            local_ts=t0,
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="link_1",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t0 + timedelta(minutes=10, seconds=1),
            local_ts=t0,
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="link_2",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is False

    def test_same_order_at_window_boundary(self, runner):
        """Boundary: delta_sec == 5.0 still fires (we use strict `>`)."""
        t0 = datetime.now(UTC)

        event1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t0,
            local_ts=t0,
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event1)

        t1 = t0 + timedelta(seconds=5)
        event2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t1,
            local_ts=t1,
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(event2)

        assert runner.same_order_error is True

    def test_same_order_window_does_not_break_hedge_pair_isolation(self, runner):
        """Regression-guard: concurrent hedge-pair fills at the same price.

        Hedge mode emits an Open-Long Buy and a Close-Short Buy at the same
        price simultaneously. ``closed_size`` routes them into different
        buffers (long vs short), so even within the time window they must
        never compare against each other and must not trigger SAME ORDER.
        """
        t0 = datetime.now(UTC)

        # Open Long Buy: closed_size == 0 → long buffer
        open_long = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t0,
            local_ts=t0,
            exec_id="exec_open",
            order_id="order_open",
            order_link_id="open_link",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
            closed_size=Decimal("0"),
        )
        runner._check_same_orders(open_long)

        # Close Short Buy: closed_size > 0 → short buffer. Same price,
        # ~1 ms later (within the time window).
        t1 = t0 + timedelta(milliseconds=1)
        close_short = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t1,
            local_ts=t1,
            exec_id="exec_close",
            order_id="order_close",
            order_link_id="close_link",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("5.0"),
            closed_size=Decimal("0.1"),
        )
        runner._check_same_orders(close_short)

        assert runner.same_order_error is False
        assert len(runner._recent_executions_long) == 1
        assert len(runner._recent_executions_short) == 1


class TestSameOrderWarnThrottle:
    """Feature 0046: throttle the `Same-order error active, skipping order
    placement` WARNING emitted by ``on_ticker`` while the SAME ORDER
    soft-block is latched. See docs/features/0046_PLAN.md and issue #94.
    """

    def _ticker(self):
        return TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal("50000.0"),
        )

    def _warn_records(self, caplog):
        return [
            r for r in caplog.records
            if r.levelname == "WARNING"
            and "Same-order error active" in r.message
        ]

    def _recovery_records(self, caplog):
        return [
            r for r in caplog.records
            if r.levelname == "INFO"
            and "Same-order error cleared" in r.message
        ]

    def test_warn_throttle_first_emit_loud(self, runner, caplog):
        """First WARNING after entering blocked state is loud (no suffix)."""
        import logging

        runner._same_order_error = True
        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            runner.on_ticker(self._ticker())

        warns = self._warn_records(caplog)
        assert len(warns) == 1
        assert "suppressed" not in warns[0].message
        assert runner._same_order_warn_last_ts is not None
        assert runner._same_order_warn_suppressed == 0

    def test_warn_throttle_suppresses_within_window(self, runner, caplog):
        """Subsequent ticks within the throttle window are suppressed (no log,
        counter increments)."""
        import logging

        runner._same_order_error = True
        # Prime the throttle so the next 50 calls are within the window.
        runner._same_order_warn_last_ts = datetime.now(UTC)

        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            for _ in range(50):
                runner.on_ticker(self._ticker())

        assert len(self._warn_records(caplog)) == 0
        assert runner._same_order_warn_suppressed == 50

    def test_warn_throttle_reemits_after_window(self, runner, caplog, monkeypatch):
        """After the throttle window elapses, a heartbeat WARNING re-emits
        with the suppressed-count suffix and the counter resets."""
        import logging
        from gridbot import runner as runner_mod

        # Make the window effectively 0 so the second tick re-emits.
        monkeypatch.setattr(runner_mod, "_SAME_ORDER_WARN_THROTTLE_SEC", 0.0)

        runner._same_order_error = True
        # Seed throttle state with a small suppressed count so the heartbeat
        # message has a non-zero N to surface.
        runner._same_order_warn_last_ts = datetime.now(UTC) - timedelta(seconds=1)
        runner._same_order_warn_suppressed = 42

        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            runner.on_ticker(self._ticker())

        warns = self._warn_records(caplog)
        assert len(warns) == 1
        assert "suppressed 42 since last" in warns[0].message
        assert runner._same_order_warn_suppressed == 0
        assert runner._same_order_warn_last_ts is not None

    def test_warn_throttle_recovery_emits_info_on_reset_call(self, runner, caplog):
        """External `reset_same_order_error()` on a latched block emits one
        recovery INFO and resets throttle state."""
        import logging

        runner._same_order_error = True
        runner._same_order_warn_last_ts = datetime.now(UTC)
        runner._same_order_warn_suppressed = 17

        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            runner.reset_same_order_error()

        recovery = self._recovery_records(caplog)
        assert len(recovery) == 1
        assert "suppressed 17 WARNINGs since" in recovery[0].message
        assert runner._same_order_error is False
        assert runner._same_order_warn_last_ts is None
        assert runner._same_order_warn_suppressed == 0

    def test_warn_throttle_recovery_via_clean_execution(self, runner, caplog):
        """P1/P2 regression: True→False net transition inside
        `_check_same_orders` (clean-fill auto-clear) must emit one recovery
        INFO and reset throttle state. A subsequent re-latch must re-emit a
        loud first WARNING (no suffix)."""
        import logging

        # Latch the block via two duplicate fills at the same price.
        ev1 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(ev1)
        ev2 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_2",
            order_id="order_2",
            order_link_id="def456",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(ev2)
        assert runner.same_order_error is True

        # Populate throttle state: one loud + several suppressed via on_ticker.
        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            for _ in range(5):
                runner.on_ticker(self._ticker())
        assert runner._same_order_warn_last_ts is not None
        suppressed_before_clear = runner._same_order_warn_suppressed
        assert suppressed_before_clear >= 1

        caplog.clear()

        # Drive a clean fill at a different price → True→False transition
        # inside `_check_same_orders`.
        ev3 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_3",
            order_id="order_3",
            order_link_id="link_3",
            side="Buy",
            price=Decimal("49000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            runner._check_same_orders(ev3)

        assert runner.same_order_error is False
        recovery = self._recovery_records(caplog)
        assert len(recovery) == 1
        assert f"suppressed {suppressed_before_clear} WARNINGs since" in recovery[0].message
        assert runner._same_order_warn_last_ts is None
        assert runner._same_order_warn_suppressed == 0

        # Re-latch via a fresh duplicate pair (different order_ids, same new
        # price) — drives the real `_check_same_orders` detection path. The
        # next on_ticker must emit a loud first WARNING (no `suppressed`
        # suffix) because throttle state is fresh.
        ev4 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_4",
            order_id="order_4",
            order_link_id="link_4",
            side="Buy",
            price=Decimal("48000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(ev4)
        ev5 = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_5",
            order_id="order_5",
            order_link_id="link_5",
            side="Buy",
            price=Decimal("48000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        runner._check_same_orders(ev5)
        assert runner.same_order_error is True

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            runner.on_ticker(self._ticker())
        warns = self._warn_records(caplog)
        assert len(warns) == 1
        assert "suppressed" not in warns[0].message

    def test_warn_throttle_no_spurious_clear_info_on_internal_reset(self, runner, caplog):
        """False→False non-transition (clean fill while block was never
        latched) must NOT emit a recovery INFO."""
        import logging

        ev = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            exec_id="exec_1",
            order_id="order_1",
            order_link_id="abc123",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
        )
        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            runner._check_same_orders(ev)

        assert runner.same_order_error is False
        assert len(self._recovery_records(caplog)) == 0

    def test_warn_throttle_recovery_via_rest_ws_glitch_auto_clear(self, runner, caplog):
        """P3: REST WS_GLITCH_SUSPECTED auto-clear path emits exactly ONE
        INFO containing both verdict context and the suppressed-count suffix.
        Throttle state is reset.

        Drives the natural flow end-to-end:
          1. First trigger pair → REST FAILS → cache entry = UNKNOWN,
             block latched (existing `test_rest_cross_check_exception_keeps_soft_block`
             behavior).
          2. Pump `on_ticker` to populate throttle state via the real
             ``_same_order_error=True`` placement-gate path.
          3. Re-stub REST to return only one match → on the next retrigger
             of the same pair, the dedup gate sees UNKNOWN and falls through
             to the first-trigger REST path (runner.py:1359), which now
             adjudicates WS_GLITCH_SUSPECTED and auto-clears.
        """
        import logging

        def _make_exec(order_id, t):
            return ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol="BTCUSDT",
                exchange_ts=t,
                local_ts=t,
                exec_id=f"e-{order_id}-{t.timestamp()}",
                order_id=order_id,
                order_link_id=f"link-{order_id}",
                side="Buy",
                price=Decimal("50000.0"),
                qty=Decimal("0.1"),
                fee=Decimal("0.5"),
                closed_pnl=Decimal("0"),
                leaves_qty=Decimal("0"),
                closed_size=Decimal("0"),
            )

        rest_match = {
            "orderId": "oa",
            "execId": "exec-oa",
            "execPrice": "50000",
            "execQty": "0.1",
            "orderLinkId": "link-oa",
            "execType": "Trade",
        }

        # Phase 1: REST fails → UNKNOWN cache entry, block latched.
        failing_client = MagicMock()
        failing_client.get_executions.side_effect = RuntimeError("boom")
        failing_client.get_executions_all.side_effect = RuntimeError("boom")
        runner._executor._client = failing_client

        t0 = datetime.now(UTC)
        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            runner._check_same_orders(_make_exec("oa", t0))
            runner._check_same_orders(_make_exec("ob", t0 + timedelta(milliseconds=500)))
        assert runner.same_order_error is True
        pair_key = frozenset(("oa", "ob"))
        assert runner._same_order_dedup_cache[pair_key].verdict == "UNKNOWN"

        # Phase 2: pump on_ticker to populate throttle state through the
        # real placement-gate path.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            for _ in range(5):
                runner.on_ticker(self._ticker())
        assert runner._same_order_warn_last_ts is not None
        suppressed_before_clear = runner._same_order_warn_suppressed
        assert suppressed_before_clear >= 1

        # Phase 3: REST now succeeds with one match → WS_GLITCH_SUSPECTED.
        # Re-trigger the same pair within the time window so the UNKNOWN
        # cache entry falls through to the first-trigger REST path.
        ok_client = MagicMock()
        ok_client.get_executions.return_value = ([rest_match], None)
        ok_client.get_executions_all.return_value = ([rest_match], False)
        runner._executor._client = ok_client

        caplog.clear()
        t1 = t0 + timedelta(seconds=2)
        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            # Add a new event with the same order_ids as the buffered pair
            # so pair_key matches the UNKNOWN cache entry.
            runner._check_same_orders(_make_exec("oa", t1))

        auto_cleared = [
            r for r in caplog.records
            if r.levelname == "INFO" and "soft-block auto-cleared" in r.message
        ]
        assert len(auto_cleared) == 1
        msg = auto_cleared[0].message
        assert "verdict=WS_GLITCH_SUSPECTED" in msg
        assert f"suppressed {suppressed_before_clear} WARNINGs since" in msg
        # No separate "Same-order error cleared" recovery INFO emitted —
        # the combined line above is the single recovery message.
        assert len(self._recovery_records(caplog)) == 0
        assert runner.same_order_error is False
        assert runner._same_order_warn_last_ts is None
        assert runner._same_order_warn_suppressed == 0
        assert runner._same_order_dedup_cache[pair_key].verdict == "WS_GLITCH_SUSPECTED"

    def test_warn_throttle_re_entry_after_clear_is_loud_again(self, runner, caplog):
        """After any clear path, the next latch re-emits a loud first
        WARNING (no `suppressed` suffix). Covers the operator's expectation
        that every fresh block is alert-worthy."""
        import logging

        # Latch, emit a few WARNINGs, clear via external reset.
        runner._same_order_error = True
        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            for _ in range(3):
                runner.on_ticker(self._ticker())
        runner.reset_same_order_error()
        assert runner._same_order_warn_last_ts is None

        # Re-latch. The very next on_ticker must emit a loud WARNING.
        runner._same_order_error = True
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="gridbot.runner"):
            runner.on_ticker(self._ticker())

        warns = self._warn_records(caplog)
        assert len(warns) == 1
        assert "suppressed" not in warns[0].message
        assert runner._same_order_warn_last_ts is not None


class TestSameOrderDedupAndAutoRecovery:
    """Feature 0031: dedup of SAME ORDER pair retriggers and soft-block
    auto-recovery on `WS_GLITCH_SUSPECTED` REST cross-check verdict.

    See docs/features/0031_PLAN.md.
    """

    def _stub_rest_executions(self, runner, executions=None, exc=None):
        """Wire executor._client.get_executions to return executions or raise."""
        client = MagicMock()
        if exc is not None:
            client.get_executions.side_effect = exc
            client.get_executions_all.side_effect = exc
        else:
            client.get_executions.return_value = (executions or [], None)
            client.get_executions_all.return_value = (executions or [], False)
        runner._executor._client = client
        return client

    def _rest_match(self, order_id):
        return {
            "orderId": order_id,
            "execId": f"exec-{order_id}",
            "execPrice": "50000",
            "execQty": "0.1",
            "orderLinkId": f"link-{order_id}",
            "execType": "Trade",
        }

    def _make_exec(self, order_id, t, exec_id=None, price="50000.0", side="Buy"):
        return ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=t,
            local_ts=t,
            exec_id=exec_id or f"e-{order_id}",
            order_id=order_id,
            order_link_id=f"link-{order_id}",
            side=side,
            price=Decimal(price),
            qty=Decimal("0.1"),
            fee=Decimal("0.5"),
            closed_pnl=Decimal("0"),
            leaves_qty=Decimal("0"),
            closed_size=Decimal("0"),
        )

    def _trigger_pair(self, runner, t0, ids=("oa", "ob")):
        """Drive two ExecutionEvents with the given order_id pair so the
        side-check pair is `frozenset(ids)`. Returns (event_a, event_b)."""
        ev_a = self._make_exec(ids[0], t0)
        ev_b = self._make_exec(ids[1], t0 + timedelta(seconds=1))
        runner._check_same_orders(ev_a)
        runner._check_same_orders(ev_b)
        return ev_a, ev_b

    # --- Phase 2: REST verdict effect on soft-block --------------------------

    def test_rest_verdict_ws_glitch_clears_soft_block(self, runner, caplog):
        """`WS_GLITCH_SUSPECTED` triggers reset_same_order_error and INFO log,
        no notifier call on the recovery path."""
        import logging

        notifier = Mock()
        runner._notifier = notifier
        # REST returns only one of the two order_ids → one match for prev,
        # zero for current → verdict=WS_GLITCH_SUSPECTED (current cur_matches=0
        # AND prev_matches=1 → not REAL_DUPLICATE).
        # The pair is (oa, ob); side-check compares (cur=ob, prev=oa).
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            self._trigger_pair(runner, datetime.now(UTC))

        assert runner.same_order_error is False
        assert len(runner._recent_executions_long) == 0
        assert len(runner._recent_executions_short) == 0
        assert any(
            "soft-block auto-cleared" in r.message and r.levelname == "INFO"
            for r in caplog.records
        )
        # Original first-trigger ALERT was emitted, but no recovery alert.
        assert notifier.alert.call_count == 1
        # Cache entry now carries WS_GLITCH_SUSPECTED.
        pair_key = frozenset(("oa", "ob"))
        assert runner._same_order_dedup_cache[pair_key].verdict == "WS_GLITCH_SUSPECTED"

    def test_rest_verdict_real_duplicate_keeps_soft_block(self, runner, caplog):
        """REAL_DUPLICATE keeps the block latched, no auto-clear log,
        cache entry verdict updated to REAL_DUPLICATE."""
        import logging

        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(
            runner, executions=[self._rest_match("oa"), self._rest_match("ob")]
        )

        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            self._trigger_pair(runner, datetime.now(UTC))

        assert runner.same_order_error is True
        assert all(
            "soft-block auto-cleared" not in r.message for r in caplog.records
        )
        assert notifier.alert.call_count == 1
        pair_key = frozenset(("oa", "ob"))
        assert runner._same_order_dedup_cache[pair_key].verdict == "REAL_DUPLICATE"

    def test_rest_cross_check_exception_keeps_soft_block(self, runner, caplog):
        """REST raising leaves the cache entry as UNKNOWN; soft-block stays."""
        import logging

        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(runner, exc=RuntimeError("boom"))

        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            self._trigger_pair(runner, datetime.now(UTC))

        assert runner.same_order_error is True
        assert all(
            "soft-block auto-cleared" not in r.message for r in caplog.records
        )
        pair_key = frozenset(("oa", "ob"))
        assert runner._same_order_dedup_cache[pair_key].verdict == "UNKNOWN"

    def test_rest_cross_check_truncated_keeps_soft_block(self, runner, caplog):
        """A truncated REST slice is not proof of a WS phantom.

        Regression guard: a single-page or otherwise incomplete REST result can
        be missing one real order_id during a busy account period. The runner
        must leave SAME ORDER latched instead of auto-clearing and dropping the
        current execution.
        """
        import logging

        notifier = Mock()
        runner._notifier = notifier
        client = MagicMock()
        client.get_executions_all.return_value = ([self._rest_match("oa")], True)
        runner._executor._client = client

        with caplog.at_level(logging.INFO, logger="gridbot.runner"):
            self._trigger_pair(runner, datetime.now(UTC))

        assert runner.same_order_error is True
        assert runner._drop_phantom_event_for_current_call is False
        assert all(
            "soft-block auto-cleared" not in r.message for r in caplog.records
        )
        pair_key = frozenset(("oa", "ob"))
        assert runner._same_order_dedup_cache[pair_key].verdict == "UNKNOWN"
        assert notifier.alert.call_count == 1

    # --- Phase 1: dedup gate behavior ----------------------------------------

    def test_same_order_ws_glitch_repeat_within_ttl_is_suppressed(self, runner, caplog):
        """Second occurrence of the same WS_GLITCH pair emits only DEBUG; one
        ERROR log and one notifier alert across both invocations."""
        import logging

        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        t0 = datetime.now(UTC)
        with caplog.at_level(logging.DEBUG, logger="gridbot.runner"):
            self._trigger_pair(runner, t0)

            # After auto-clear, buffers are empty. Re-form the pair with two
            # more events using the SAME order_ids — pair_key matches the
            # cached entry.
            t1 = t0 + timedelta(seconds=2)
            runner._check_same_orders(self._make_exec("oa", t1))
            runner._check_same_orders(self._make_exec("ob", t1 + timedelta(seconds=1)))

        same_order_errors = [
            r for r in caplog.records if "SAME ORDER ERROR" in r.message
        ]
        suppressed = [
            r for r in caplog.records
            if "duplicate SAME ORDER pair suppressed" in r.message
        ]
        assert len(same_order_errors) == 1, "exactly one ERROR across both triggers"
        assert len(suppressed) >= 1, "second trigger emits DEBUG suppression"
        assert notifier.alert.call_count == 1
        assert runner.same_order_error is False

    def test_same_order_real_duplicate_repeat_within_ttl_keeps_block_without_alert(
        self, runner, caplog,
    ):
        """REGRESSION: dedup gate must re-establish `_same_order_error=True` on
        REAL_DUPLICATE retriggers (because `_check_same_orders` resets the flag
        at the top of every call). Otherwise the block silently drops."""
        import logging

        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(
            runner, executions=[self._rest_match("oa"), self._rest_match("ob")]
        )

        t0 = datetime.now(UTC)
        with caplog.at_level(logging.DEBUG, logger="gridbot.runner"):
            self._trigger_pair(runner, t0)
            assert runner.same_order_error is True  # block latched

            # Drive the same pair again WITHIN TTL. _check_same_orders will
            # reset the flag at the top of the call; the dedup gate must
            # re-establish it.
            t1 = t0 + timedelta(seconds=10)
            runner._check_same_orders(self._make_exec("oa", t1))
            runner._check_same_orders(self._make_exec("ob", t1 + timedelta(seconds=1)))

        same_order_errors = [
            r for r in caplog.records if "SAME ORDER ERROR" in r.message
        ]
        suppressed = [
            r for r in caplog.records
            if "duplicate SAME ORDER pair suppressed" in r.message
        ]
        assert len(same_order_errors) == 1, "exactly one ERROR across both triggers"
        assert len(suppressed) >= 1, "second trigger emits DEBUG suppression"
        assert notifier.alert.call_count == 1, "no second ALERT"
        # Critical assertion: block is still latched after the dedup-suppressed
        # retrigger.
        assert runner.same_order_error is True

    def test_same_order_dedup_re_fires_after_ttl_expires(self, runner, caplog):
        """After the TTL window, the cache entry is dropped on lazy expiry and
        the same pair fires the full first-trigger path again."""
        import logging
        from gridbot.runner import _SAME_ORDER_DEDUP_TTL_SEC

        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        t0 = datetime.now(UTC)
        self._trigger_pair(runner, t0)
        assert notifier.alert.call_count == 1

        # Backdate the cached entry past TTL.
        pair_key = frozenset(("oa", "ob"))
        entry = runner._same_order_dedup_cache[pair_key]
        entry.last_seen_ts = t0 - timedelta(seconds=_SAME_ORDER_DEDUP_TTL_SEC + 60)
        entry.first_seen_ts = entry.last_seen_ts

        # Re-trigger with same order_ids. Lazy expiry drops the entry, full
        # first-trigger path runs again.
        t2 = datetime.now(UTC)
        with caplog.at_level(logging.ERROR, logger="gridbot.runner"):
            runner._check_same_orders(self._make_exec("oa", t2))
            runner._check_same_orders(
                self._make_exec("ob", t2 + timedelta(seconds=1))
            )

        same_order_errors = [
            r for r in caplog.records if "SAME ORDER ERROR" in r.message
        ]
        # caplog accumulates: 1 from initial trigger + 1 from post-TTL re-trigger.
        assert len(same_order_errors) == 2, "fresh ERROR after TTL"
        assert notifier.alert.call_count == 2, "fresh ALERT after TTL"

    def test_same_order_dedup_distinct_pairs_independent(self, runner):
        """Pair A in cache must not suppress detection on a different pair B."""
        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        t0 = datetime.now(UTC)
        # Pair A
        self._trigger_pair(runner, t0, ids=("oa", "ob"))
        # Buffers cleared by auto-clear; pair B uses different order_ids and
        # different price to avoid hedge-pair classification interference.
        self._stub_rest_executions(runner, executions=[self._rest_match("oc")])
        t1 = t0 + timedelta(seconds=10)
        runner._check_same_orders(self._make_exec("oc", t1, price="51000.0"))
        runner._check_same_orders(
            self._make_exec("od", t1 + timedelta(seconds=1), price="51000.0")
        )

        # Both pairs were adjudicated and live in the cache.
        assert frozenset(("oa", "ob")) in runner._same_order_dedup_cache
        assert frozenset(("oc", "od")) in runner._same_order_dedup_cache
        assert notifier.alert.call_count == 2, "both pairs alerted on first trigger"

    def test_same_order_dedup_cache_lazy_expiry_bounds_memory(self, runner):
        """Lazy expiry drops entries whose last_seen_ts is older than the TTL
        when a fresh detection runs."""
        from gridbot.runner import (
            _SAME_ORDER_DEDUP_TTL_SEC,
            _SameOrderDedupEntry,
        )

        notifier = Mock()
        runner._notifier = notifier
        # Pre-populate a stale entry under an unrelated pair_key.
        stale_key = frozenset(("stale_a", "stale_b"))
        stale_ts = datetime.now(UTC) - timedelta(
            seconds=_SAME_ORDER_DEDUP_TTL_SEC + 600,
        )
        runner._same_order_dedup_cache[stale_key] = _SameOrderDedupEntry(
            first_seen_ts=stale_ts,
            last_seen_ts=stale_ts,
            verdict="WS_GLITCH_SUSPECTED",
        )

        # Drive a fresh detection on a new pair → triggers lazy expiry.
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])
        self._trigger_pair(runner, datetime.now(UTC))

        assert stale_key not in runner._same_order_dedup_cache
        assert frozenset(("oa", "ob")) in runner._same_order_dedup_cache

    def test_unknown_verdict_falls_through_to_full_trigger(self, runner, caplog):
        """A cached UNKNOWN entry (cross-check earlier failed) must fall
        through to the full first-trigger path on the next occurrence."""
        import logging

        notifier = Mock()
        runner._notifier = notifier

        # First attempt: REST raises → cache verdict stays UNKNOWN.
        self._stub_rest_executions(runner, exc=RuntimeError("boom"))
        t0 = datetime.now(UTC)
        self._trigger_pair(runner, t0)
        pair_key = frozenset(("oa", "ob"))
        assert runner._same_order_dedup_cache[pair_key].verdict == "UNKNOWN"
        assert notifier.alert.call_count == 1

        # Second attempt within TTL with a working REST stub. Buffer was not
        # cleared (REST exception did not auto-clear), so manually reset to
        # let new events reform the pair without short-circuiting on
        # exchange_ts > 5s.
        runner._recent_executions_long.clear()
        runner._recent_executions_short.clear()
        runner._same_order_error = False
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])
        t1 = t0 + timedelta(seconds=10)
        with caplog.at_level(logging.ERROR, logger="gridbot.runner"):
            runner._check_same_orders(self._make_exec("oa", t1))
            runner._check_same_orders(
                self._make_exec("ob", t1 + timedelta(seconds=1))
            )

        same_order_errors = [
            r for r in caplog.records if "SAME ORDER ERROR" in r.message
        ]
        # caplog accumulates: 1 from first attempt (UNKNOWN) + 1 from second.
        assert len(same_order_errors) == 2, "UNKNOWN fell through to full path"
        assert notifier.alert.call_count == 2, "fresh ALERT on UNKNOWN fall-through"
        # Now the cross-check succeeded and updated verdict.
        assert (
            runner._same_order_dedup_cache[pair_key].verdict
            == "WS_GLITCH_SUSPECTED"
        )

    def test_burst_of_five_same_order_events_emits_one_alert(self, runner, caplog):
        """Incident replay: 5 SAME ORDER events on the same pair within ~1s,
        REST verdict=WS_GLITCH_SUSPECTED. Expect exactly one ERROR + one
        ALERT + one REST cross-check; bot recovers."""
        import logging

        notifier = Mock()
        runner._notifier = notifier
        client = self._stub_rest_executions(
            runner, executions=[self._rest_match("oa")]
        )

        t0 = datetime.now(UTC)
        # Alternate order_ids so the buffer pair = frozenset({oa, ob}) on
        # every detection cycle (matching the WS-replay pattern observed in
        # production where the same two events were emitted repeatedly).
        ids_sequence = ["oa", "ob", "oa", "ob", "oa"]
        with caplog.at_level(logging.DEBUG, logger="gridbot.runner"):
            for i, oid in enumerate(ids_sequence):
                runner._check_same_orders(
                    self._make_exec(oid, t0 + timedelta(milliseconds=200 * i))
                )

        same_order_errors = [
            r for r in caplog.records if "SAME ORDER ERROR" in r.message
        ]
        assert len(same_order_errors) == 1
        assert notifier.alert.call_count == 1
        assert client.get_executions_all.call_count == 1
        assert runner.same_order_error is False

    def test_on_execution_drops_phantom_event_before_engine(self, runner):
        """E2E (review feedback for feature 0031 P1, escalated): a phantom
        ExecutionEvent confirmed by REST cross-check (verdict=WS_GLITCH_SUSPECTED)
        must NOT reach `engine.on_event(event)` and must NOT trigger
        `_execute_intents`. Letting the engine see a fake fill would
        corrupt grid/position state and contaminate downstream events."""
        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        placement = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("50100.0"),
            qty=Decimal("0.1"),
            grid_level=2,
            direction="long",
        )
        runner._engine = MagicMock()
        runner._engine.on_event.return_value = [placement]
        runner._execute_intents = MagicMock()

        t0 = datetime.now(UTC)
        ev_a = self._make_exec("oa", t0)
        # First fill: real, no SAME ORDER trigger; reaches the engine.
        runner.on_execution(ev_a)
        assert any(
            call.args and getattr(call.args[0], "exec_id", None) == ev_a.exec_id
            for call in runner._engine.on_event.call_args_list
        ), "real event must still reach engine.on_event"

        # Second fill: phantom side. SAME ORDER fires; REST → WS_GLITCH →
        # auto-clear; on_execution returns BEFORE engine.on_event for this
        # event AND _execute_intents is not called.
        runner._engine.on_event.reset_mock()
        runner._execute_intents.reset_mock()
        ev_b = self._make_exec("ob", t0 + timedelta(seconds=1))
        runner.on_execution(ev_b)

        assert runner.same_order_error is False
        runner._execute_intents.assert_not_called()
        # Critical assertion: engine did NOT see the phantom event.
        for call in runner._engine.on_event.call_args_list:
            assert getattr(call.args[0], "exec_id", None) != ev_b.exec_id, (
                "engine.on_event must NOT be called for phantom event"
            )

    def test_on_execution_phantom_does_not_mark_tracked_order_filled(
        self, runner,
    ):
        """E2E (review feedback for feature 0031 P1, second escalation):
        when an ExecutionEvent is REST-classified as WS_GLITCH_SUSPECTED, the
        underlying exchange order is still resting. The runner must NOT call
        `tracked.mark_filled()` on it — otherwise `get_limit_orders()` would
        drop the order from the live in-memory view (it filters
        status not in {"placed"} at runner.py:324), causing the reconciler
        to operate on a stale book."""
        notifier = Mock()
        runner._notifier = notifier
        # REST returns only oa's fill → verdict=WS_GLITCH_SUSPECTED for the
        # ob (current) event in the side-check.
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        intent_a = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy",
            price=Decimal("50000.0"), qty=Decimal("0.1"),
            grid_level=1, direction="long",
        )
        intent_b = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy",
            price=Decimal("50000.0"), qty=Decimal("0.1"),
            grid_level=1, direction="long",
        )
        # Two tracked orders both currently `placed`. Distinct client IDs
        # simulate the same scenario as the existing dup-bug regression test.
        runner._tracked_orders["link-oa"] = TrackedOrder(
            client_order_id="link-oa", order_id="oa",
            intent=intent_a, status="placed",
        )
        runner._tracked_orders["link-ob"] = TrackedOrder(
            client_order_id="link-ob", order_id="ob",
            intent=intent_b, status="placed",
        )

        t0 = datetime.now(UTC)
        # First event (oa): real fill. tracked-oa should be marked filled.
        runner.on_execution(self._make_exec("oa", t0))
        assert runner._tracked_orders["link-oa"].status == "filled"

        # Second event (ob): SAME ORDER fires, REST → WS_GLITCH → drop.
        # tracked-ob must remain `placed`.
        runner.on_execution(self._make_exec("ob", t0 + timedelta(seconds=1)))

        assert runner._tracked_orders["link-ob"].status == "placed", (
            "phantom event must NOT mark its tracked order filled"
        )
        # And the order is still visible in the live limit-orders view.
        limit_orders = runner.get_limit_orders()
        long_order_ids = [o["orderId"] for o in limit_orders["long"]]
        assert "ob" in long_order_ids, (
            "phantom-event tracked order must still appear in get_limit_orders()"
        )

    def test_on_execution_cached_ws_glitch_repeat_drops_phantom(self, runner):
        """E2E (review feedback for feature 0031 P1, third escalation):
        a repeat of the same WS_GLITCH-classified pair (cache-hit dedup
        branch) must also drop the phantom event end-to-end. Without
        setting `_drop_phantom_event_for_current_call` in the cache-hit
        branch, the dedup gate silences the alert but the event still
        flows into `tracked.mark_filled()` and `engine.on_event(event)` —
        precisely the corruption the cache is supposed to prevent."""
        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        intent_a = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy",
            price=Decimal("50000.0"), qty=Decimal("0.1"),
            grid_level=1, direction="long",
        )
        intent_b = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy",
            price=Decimal("50000.0"), qty=Decimal("0.1"),
            grid_level=1, direction="long",
        )
        runner._tracked_orders["link-oa"] = TrackedOrder(
            client_order_id="link-oa", order_id="oa",
            intent=intent_a, status="placed",
        )
        runner._tracked_orders["link-ob"] = TrackedOrder(
            client_order_id="link-ob", order_id="ob",
            intent=intent_b, status="placed",
        )

        placement = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy",
            price=Decimal("50100.0"), qty=Decimal("0.1"),
            grid_level=2, direction="long",
        )
        runner._engine = MagicMock()
        runner._engine.on_event.return_value = [placement]
        runner._execute_intents = MagicMock()

        t0 = datetime.now(UTC)
        # First occurrence — full first-trigger path. ev_ob is dropped.
        runner.on_execution(self._make_exec("oa", t0))
        runner.on_execution(self._make_exec("ob", t0 + timedelta(seconds=1)))
        pair_key = frozenset(("oa", "ob"))
        assert (
            runner._same_order_dedup_cache[pair_key].verdict
            == "WS_GLITCH_SUSPECTED"
        )

        # Re-form the same pair within TTL. Auto-clear emptied buffers, so
        # both events are needed to reconstruct the side-check pair. The
        # phantom is ev_ob (REST has no match for "ob"); ev_oa replay is
        # NOT the phantom from REST's perspective and is allowed to flow
        # through normally — that part is a separate, fundamental gap
        # (single-event WS replays without a paired event cannot be
        # detected via SAME ORDER), out of scope for this feature.
        t1 = t0 + timedelta(seconds=10)
        ev_oa_replay = self._make_exec("oa", t1)
        ev_ob_replay = self._make_exec("ob", t1 + timedelta(seconds=1))
        runner._engine.on_event.reset_mock()
        runner.on_execution(ev_oa_replay)
        # Snapshot _execute_intents call count after ev_oa replay so we can
        # detect any *additional* call attributable to ev_ob.
        execute_calls_before_ob = runner._execute_intents.call_count

        runner.on_execution(ev_ob_replay)

        # Phantom-side replay (ev_ob) must not reach engine or executor.
        for call in runner._engine.on_event.call_args_list:
            assert (
                getattr(call.args[0], "exec_id", None) != ev_ob_replay.exec_id
            ), (
                "cached WS_GLITCH retrigger phantom must NOT reach "
                "engine.on_event"
            )
        assert runner._execute_intents.call_count == execute_calls_before_ob, (
            "no additional _execute_intents call attributable to phantom replay"
        )
        assert runner._tracked_orders["link-ob"].status == "placed", (
            "phantom-side tracked order must remain placed on cached repeat"
        )

    def test_on_execution_phantom_drop_flag_resets_between_events(self, runner):
        """The one-shot `_drop_phantom_event_for_current_call` flag must
        reset at the top of every `on_execution` call so a non-phantom event
        following a phantom one is processed normally — engine sees it AND
        placements run."""
        notifier = Mock()
        runner._notifier = notifier
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])

        placement = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("50100.0"),
            qty=Decimal("0.1"),
            grid_level=2,
            direction="long",
        )
        runner._engine = MagicMock()
        runner._engine.on_event.return_value = [placement]
        runner._execute_intents = MagicMock()

        t0 = datetime.now(UTC)
        runner.on_execution(self._make_exec("oa", t0))
        runner.on_execution(self._make_exec("ob", t0 + timedelta(seconds=1)))
        # ev_b was phantom-dropped. The flag must now be reset.

        runner._engine.on_event.reset_mock()
        runner._execute_intents.reset_mock()
        t2 = t0 + timedelta(minutes=1)
        ev_c = self._make_exec("oc", t2, price="51000.0")
        runner.on_execution(ev_c)

        # Subsequent non-phantom event reaches engine AND triggers placement.
        assert runner._engine.on_event.called
        runner._execute_intents.assert_called_once()
        assert runner._drop_phantom_event_for_current_call is False

    def test_dedup_then_real_duplicate_on_different_pair_fires_normally(
        self, runner, caplog,
    ):
        """Pair A cached as WS_GLITCH (suppressed); pair B (fresh order_ids)
        within TTL fires the full first-trigger path."""
        import logging

        notifier = Mock()
        runner._notifier = notifier

        # Pair A → WS_GLITCH auto-clear.
        self._stub_rest_executions(runner, executions=[self._rest_match("oa")])
        t0 = datetime.now(UTC)
        self._trigger_pair(runner, t0, ids=("oa", "ob"))
        assert runner.same_order_error is False

        # Pair B fresh (different order_ids). Use a different price so the
        # pair B fills do not share the same long-buffer slot as pair A
        # leftovers.
        self._stub_rest_executions(
            runner, executions=[self._rest_match("oc"), self._rest_match("od")]
        )
        t1 = t0 + timedelta(seconds=10)
        with caplog.at_level(logging.ERROR, logger="gridbot.runner"):
            runner._check_same_orders(self._make_exec("oc", t1, price="51000.0"))
            runner._check_same_orders(
                self._make_exec("od", t1 + timedelta(seconds=1), price="51000.0")
            )

        # Pair B fired: ERROR + ALERT, REAL_DUPLICATE verdict re-arms block.
        # caplog accumulates: 1 from pair A first-trigger + 1 from pair B.
        same_order_errors = [
            r for r in caplog.records if "SAME ORDER ERROR" in r.message
        ]
        assert len(same_order_errors) == 2
        assert notifier.alert.call_count == 2
        assert runner.same_order_error is True


class TestRunnerAuthCooldown:
    """Tests for runner skipping intents during auth cooldown."""
    def test_skips_intents_when_auth_cooldown_active(self, strategy_config):
        """Test intents are skipped when executor.auth_cooldown is True."""
        executor = Mock(spec=IntentExecutor)
        executor.shadow_mode = False
        executor.auth_cooldown = True
        executor.execute_place = MagicMock(
            return_value=OrderResult(success=True, order_id="order_123")
        )

        runner = StrategyRunner(strategy_config=strategy_config, executor=executor)

        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal("50000.0"),
        )
        runner.on_ticker(ticker)

        # Grid was built (engine processed ticker) but no orders placed
        assert len(runner.engine.grid.grid) > 0
        executor.execute_place.assert_not_called()
    def test_executes_intents_when_cooldown_cleared(self, strategy_config):
        """Test intents execute normally when auth_cooldown is False."""
        executor = Mock(spec=IntentExecutor)
        executor.shadow_mode = False
        executor.auth_cooldown = False
        executor.execute_place = MagicMock(
            return_value=OrderResult(success=True, order_id="order_123")
        )

        runner = StrategyRunner(strategy_config=strategy_config, executor=executor)
        runner._wallet_balance = Decimal("10000")

        ticker = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=datetime.now(UTC),
            local_ts=datetime.now(UTC),
            last_price=Decimal("50000.0"),
        )
        runner.on_ticker(ticker)

        # Orders should have been placed
        assert executor.execute_place.called
    def test_stops_mid_batch_when_cooldown_activates(self, strategy_config):
        """Test remaining intents are skipped if cooldown activates mid-batch."""
        executor = Mock(spec=IntentExecutor)
        executor.shadow_mode = False
        executor.auth_cooldown = False

        call_count = 0

        def place_that_triggers_cooldown(intent):
            nonlocal call_count
            call_count += 1
            # First call triggers cooldown
            executor.auth_cooldown = True
            return OrderResult(success=False, error="[10005] Permission denied")

        executor.execute_place = place_that_triggers_cooldown
        executor.execute_cancel = MagicMock(return_value=CancelResult(success=True))

        runner = StrategyRunner(strategy_config=strategy_config, executor=executor)

        # Manually call _execute_intents with multiple intents
        intents = [
            PlaceLimitIntent.create(
                symbol="BTCUSDT", side="Buy", price=Decimal(str(50000 - i * 100)),
                qty=Decimal("0.001"), grid_level=i, direction="long",
            )
            for i in range(5)
        ]

        runner._execute_intents(intents, EMPTY_LIMITS)

        # Only the first intent should have been executed;
        # the rest skipped because cooldown activated
        assert call_count == 1

    def test_cancels_run_before_places(self, strategy_config):
        """Cancels in a mixed batch must dispatch before any place, so margin
        and active-order slots held by stale orders are freed before new
        placements try to consume them."""
        executor = Mock(spec=IntentExecutor)
        executor.shadow_mode = False
        executor.auth_cooldown = False

        call_order: list[str] = []

        def record_place(intent):
            call_order.append(f"place@{intent.price}")
            return OrderResult(success=True, order_id=f"oid_{intent.price}")

        def record_cancel(intent):
            call_order.append(f"cancel@{intent.order_id}")
            return CancelResult(success=True)

        executor.execute_place = record_place
        executor.execute_cancel = record_cancel

        runner = StrategyRunner(strategy_config=strategy_config, executor=executor)
        runner._wallet_balance = Decimal("10000")

        # Engine-style interleaved order: place, cancel, place, cancel
        intents = [
            PlaceLimitIntent.create(
                symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
                qty=Decimal("0.001"), grid_level=0, direction="long",
            ),
            CancelIntent(symbol="BTCUSDT", order_id="stale_1", reason="outside_grid"),
            PlaceLimitIntent.create(
                symbol="BTCUSDT", side="Buy", price=Decimal("49900"),
                qty=Decimal("0.001"), grid_level=1, direction="long",
            ),
            CancelIntent(symbol="BTCUSDT", order_id="stale_2", reason="side_mismatch"),
        ]

        runner._execute_intents(intents, EMPTY_LIMITS)

        # All cancels must precede all places, regardless of input ordering.
        cancel_indices = [i for i, c in enumerate(call_order) if c.startswith("cancel@")]
        place_indices = [i for i, c in enumerate(call_order) if c.startswith("place@")]
        assert cancel_indices and place_indices
        assert max(cancel_indices) < min(place_indices), (
            f"cancels must run before places, got: {call_order}"
        )


class TestIsGoodToPlace:
    """Tests for _is_good_to_place reduce-only order validation.

    Reference: bbu_reference/bbu2-master/bybit_api_usdt.py:295-313
    """

    def test_exact_duplicate_rejected(self, runner):
        """Exact duplicate (same price, qty, side, reduce_only) is rejected."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.001"), grid_level=1, direction="long",
            reduce_only=False,
        )
        limits = {'long': [_make_limit_order(intent)], 'short': []}

        # Same params, different client_order_id
        dup_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.001"), grid_level=2, direction="long",
            reduce_only=False,
        )
        assert runner._is_good_to_place(dup_intent, limits) is False

    def test_exact_duplicate_different_qty_allowed(self, runner):
        """Order with same price but different qty is not a duplicate."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.001"), grid_level=1, direction="long",
            reduce_only=False,
        )
        limits = {'long': [_make_limit_order(intent)], 'short': []}

        different_qty = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.002"), grid_level=2, direction="long",
            reduce_only=False,
        )
        assert runner._is_good_to_place(different_qty, limits) is True

    def test_exact_duplicate_ignores_non_placed(self, runner):
        """Non-placed orders are excluded by get_limit_orders() before reaching
        _is_good_to_place, so empty limits means no duplicate rejection."""
        dup_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.001"), grid_level=2, direction="long",
            reduce_only=False,
        )
        assert runner._is_good_to_place(dup_intent, EMPTY_LIMITS) is True

    def test_open_order_always_good(self, runner):
        """Non-reduce-only (open) orders are always good to place."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.001"), grid_level=1, direction="long",
            reduce_only=False,
        )
        assert runner._is_good_to_place(intent, EMPTY_LIMITS) is True

    def test_reduce_only_within_position_size(self, runner):
        """Reduce-only order is good when qty fits within position size."""
        runner._short_position.size = Decimal("0.1")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.05"), grid_level=1, direction="short",
            reduce_only=True,
        )
        assert runner._is_good_to_place(intent, EMPTY_LIMITS) is True

    def test_reduce_only_equals_position_size(self, runner):
        """Reduce-only order is rejected when total qty equals position size (position_size > total is False when equal)."""
        runner._short_position.size = Decimal("0.1")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.1"), grid_level=1, direction="short",
            reduce_only=True,
        )
        assert runner._is_good_to_place(intent, EMPTY_LIMITS) is False

    def test_reduce_only_exceeds_position_size(self, runner):
        """Reduce-only order is rejected when qty exceeds position size."""
        runner._short_position.size = Decimal("0.1")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.2"), grid_level=1, direction="short",
            reduce_only=True,
        )
        assert runner._is_good_to_place(intent, EMPTY_LIMITS) is False

    def test_reduce_only_accounts_for_existing_orders(self, runner):
        """Existing placed reduce-only orders are counted toward the total."""
        runner._short_position.size = Decimal("0.1")

        # Simulate an existing placed reduce-only Buy order for short direction
        existing_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("48000"),
            qty=Decimal("0.05"), grid_level=2, direction="short",
            reduce_only=True,
        )
        limits = {'long': [], 'short': [_make_limit_order(existing_intent)]}

        # New order: 0.05 existing + 0.06 new = 0.11 > 0.1 position
        new_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.06"), grid_level=1, direction="short",
            reduce_only=True,
        )
        assert runner._is_good_to_place(new_intent, limits) is False

    def test_reduce_only_ignores_non_placed_orders(self, runner):
        """Non-placed orders are excluded by get_limit_orders() before reaching
        _is_good_to_place, so empty limits means no reduce-only qty counted."""
        runner._short_position.size = Decimal("0.1")

        # New order: only 0.05 new, position is 0.1 -> good
        new_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.05"), grid_level=1, direction="short",
            reduce_only=True,
        )
        assert runner._is_good_to_place(new_intent, EMPTY_LIMITS) is True

    def test_reduce_only_zero_position_rejects(self, runner):
        """Reduce-only order is rejected when position size is zero."""
        runner._short_position.size = Decimal("0")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.001"), grid_level=1, direction="short",
            reduce_only=True,
        )
        assert runner._is_good_to_place(intent, EMPTY_LIMITS) is False

    def test_reduce_only_long_direction(self, runner):
        """Reduce-only Sell order for long direction respects long position size."""
        runner._long_position.size = Decimal("0.1")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Sell", price=Decimal("51000"),
            qty=Decimal("0.05"), grid_level=1, direction="long",
            reduce_only=True,
        )
        assert runner._is_good_to_place(intent, EMPTY_LIMITS) is True

    def test_direction_string_enum_compatibility(self, runner):
        """close_side_map works with plain string direction (PlaceLimitIntent.direction is str)."""
        runner._long_position.size = Decimal("0.1")
        runner._short_position.size = Decimal("0.1")

        # direction="long" (plain string, not DirectionType.LONG)
        long_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Sell", price=Decimal("51000"),
            qty=Decimal("0.05"), grid_level=1, direction="long",
            reduce_only=True,
        )
        assert runner._is_good_to_place(long_intent, EMPTY_LIMITS) is True

        # direction="short" (plain string, not DirectionType.SHORT)
        short_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.05"), grid_level=1, direction="short",
            reduce_only=True,
        )
        assert runner._is_good_to_place(short_intent, EMPTY_LIMITS) is True
    def test_execute_place_skips_when_not_good(self, runner, mock_executor):
        """_execute_place_intent skips order when _is_good_to_place returns False."""
        runner._wallet_balance = Decimal("10000")
        runner._short_position.size = Decimal("0")

        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("49000"),
            qty=Decimal("0.001"), grid_level=1, direction="short",
            reduce_only=True,
        )
        runner._execute_place_intent(intent, EMPTY_LIMITS)
        mock_executor.execute_place.assert_not_called()


class TestStateStoreWiring:
    """Tests for the GridStateStore wiring on the runner."""

    def test_no_store_means_no_save_no_restore(self, strategy_config, mock_executor):
        """state_store=None must not crash on construction or grid mutation."""
        runner = StrategyRunner(strategy_config=strategy_config, executor=mock_executor)
        runner._engine.grid.build_grid(50000.0)
        assert len(runner._engine.grid.grid) > 0

    def test_load_grid_state_returns_none_when_no_store(self, strategy_config, mock_executor):
        runner = StrategyRunner(strategy_config=strategy_config, executor=mock_executor)
        assert runner._load_grid_state() is None

    def test_load_grid_state_config_match(self, strategy_config, mock_executor):
        """Saved grid with matching grid_step + grid_count is loaded."""
        store = Mock()
        store.load.return_value = {
            "grid": [{"side": "Buy", "price": 49000.0}, {"side": "Wait", "price": 50000.0}],
            "grid_step": strategy_config.grid_step,
            "grid_count": strategy_config.grid_count,
        }
        StrategyRunner(
            strategy_config=strategy_config, executor=mock_executor, state_store=store,
        )
        store.load.assert_called_once_with(strategy_config.strat_id)

    def test_load_grid_state_config_mismatch_returns_none(self, strategy_config, mock_executor):
        """Saved grid with different grid_step is discarded — engine starts empty."""
        store = Mock()
        store.load.return_value = {
            "grid": [{"side": "Buy", "price": 49000.0}],
            "grid_step": strategy_config.grid_step + 0.1,
            "grid_count": strategy_config.grid_count,
        }
        runner = StrategyRunner(
            strategy_config=strategy_config, executor=mock_executor, state_store=store,
        )
        assert runner._engine.grid.grid == []

    def test_on_grid_change_persists_full_grid(self, strategy_config, mock_executor):
        """Grid mutations flow through the on_change callback into store.save."""
        store = Mock()
        store.load.return_value = None
        runner = StrategyRunner(
            strategy_config=strategy_config, executor=mock_executor, state_store=store,
        )
        runner._engine.grid.build_grid(50000.0)

        store.save.assert_called_once()
        call = store.save.call_args
        assert call.kwargs["strat_id"] == strategy_config.strat_id
        assert call.kwargs["grid_step"] == strategy_config.grid_step
        assert call.kwargs["grid_count"] == strategy_config.grid_count
        assert len(call.kwargs["grid"]) == strategy_config.grid_count + 1

    def test_on_grid_change_skips_empty_grid(self, strategy_config, mock_executor):
        """Empty / single-WAIT intermediate states are not persisted."""
        store = Mock()
        store.load.return_value = None
        runner = StrategyRunner(
            strategy_config=strategy_config, executor=mock_executor, state_store=store,
        )
        runner._on_grid_change([], None)
        runner._on_grid_change([{"side": "Wait", "price": 100.0}], None)
        store.save.assert_not_called()

    def test_on_grid_change_no_store_is_noop(self, strategy_config, mock_executor):
        runner = StrategyRunner(strategy_config=strategy_config, executor=mock_executor)
        runner._on_grid_change([{"side": "Wait", "price": 100.0}] * 5, None)

    def test_account_id_required_when_grid_state_writer_wired(
        self, strategy_config, mock_executor,
    ):
        """0047: dummy account_id default must NOT be silently accepted
        when a DB writer is wired — would FK-mismatch replay's account
        scope and snapshots become invisible.
        """
        writer = Mock()
        with pytest.raises(ValueError, match="account_id must be set"):
            StrategyRunner(
                strategy_config=strategy_config,
                executor=mock_executor,
                # account_id omitted on purpose — dummy default kicks in.
                grid_state_writer=writer,
            )


# ---------------------------------------------------------------------------
# Feature 0047 — exchange_ts propagation through _on_grid_change.
#
# These tests assert each mutation path enumerated in plan v18 carries the
# correct triggering-event timestamp into ``GridStateWriter.write``. Wrong
# timestamps would persist cleanly (no test fails) but break replay's
# ``at_or_before(seed.at_ts)`` lookup — the same silent-failure class the
# plan calls out for the ``grid.py:78`` arity swallow.
# ---------------------------------------------------------------------------


class TestOnGridChangeDbWriter:
    """0047: ``_on_grid_change`` writer-side behaviour (independent of engine paths)."""

    _ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"

    def _runner(self, strategy_config, mock_executor, *, writer=None, store=None):
        return StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            account_id=self._ACCOUNT_ID,
            state_store=store,
            grid_state_writer=writer,
        )

    def test_writes_to_db_when_writer_configured(self, strategy_config, mock_executor):
        store = Mock()
        store.load.return_value = None
        writer = Mock()
        runner = self._runner(strategy_config, mock_executor, writer=writer, store=store)

        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        grid = [
            {"side": "Buy", "price": 99.0},
            {"side": "Wait", "price": 100.0},
            {"side": "Sell", "price": 101.0},
        ]
        runner._on_grid_change(grid, ts)

        writer.write.assert_called_once()
        call = writer.write.call_args
        assert call.kwargs["strat_id"] == strategy_config.strat_id
        assert call.kwargs["grid"] is grid
        assert call.kwargs["grid_step"] == strategy_config.grid_step
        assert call.kwargs["grid_count"] == strategy_config.grid_count
        assert call.kwargs["account_id"] == self._ACCOUNT_ID
        assert call.kwargs["symbol"] == strategy_config.symbol
        assert call.kwargs["exchange_ts"] == ts
        # File path also fired in parallel (independent backend guards).
        store.save.assert_called_once()

    def test_skips_db_write_when_writer_not_configured(self, strategy_config, mock_executor):
        store = Mock()
        store.load.return_value = None
        runner = self._runner(strategy_config, mock_executor, store=store)
        runner._on_grid_change(
            [{"side": "Buy", "price": 99.0}, {"side": "Sell", "price": 101.0}],
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        # No grid_state_writer attribute means no DB write path — file path still fires.
        store.save.assert_called_once()

    def test_db_write_fires_when_state_store_is_none(self, strategy_config, mock_executor):
        """v12 guard split: backends are independent. DB write fires even
        when the legacy file backend is disabled (the v11 plan would have
        skipped this case via the early ``state_store is None`` return)."""
        writer = Mock()
        runner = self._runner(strategy_config, mock_executor, writer=writer, store=None)
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        runner._on_grid_change(
            [{"side": "Buy", "price": 99.0}, {"side": "Sell", "price": 101.0}],
            ts,
        )
        writer.write.assert_called_once()
        assert writer.write.call_args.kwargs["exchange_ts"] == ts

    def test_skips_db_when_exchange_ts_none(self, strategy_config, mock_executor):
        """Constructor-time restore_grid (or any other code path without a
        triggering event) carries ``exchange_ts=None``; the DB write must
        be skipped — non-null column would FK-write garbage and break
        ``at_or_before`` lookups. File writer is unaffected."""
        store = Mock()
        store.load.return_value = None
        writer = Mock()
        runner = self._runner(strategy_config, mock_executor, writer=writer, store=store)
        runner._on_grid_change(
            [{"side": "Buy", "price": 99.0}, {"side": "Sell", "price": 101.0}],
            None,
        )
        writer.write.assert_not_called()
        store.save.assert_called_once()


class TestOnGridChangeExchangeTsPropagation:
    """0047: engine-path coverage that the triggering event's exchange_ts
    is the value that lands at ``GridStateWriter.write``.

    Each test drives one of the engine mutation paths enumerated in plan
    v18 (restored-grid OOB rebuild, deferred-fill consumption, etc.) and
    inspects the captured ``exchange_ts`` on a Mock writer.
    """

    _ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"

    def _runner(
        self,
        strategy_config,
        mock_executor,
        *,
        writer,
        restored_grid=None,
    ) -> StrategyRunner:
        store = Mock()
        store.load.return_value = (
            {
                "grid": restored_grid,
                "grid_step": strategy_config.grid_step,
                "grid_count": strategy_config.grid_count,
            }
            if restored_grid is not None
            else None
        )
        return StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            account_id=self._ACCOUNT_ID,
            state_store=store,
            grid_state_writer=writer,
        )

    @staticmethod
    def _ticker(price: float, ts: datetime) -> TickerEvent:
        return TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            last_price=Decimal(str(price)),
            mark_price=Decimal(str(price)),
            bid1_price=Decimal(str(price - 0.5)),
            ask1_price=Decimal(str(price + 0.5)),
            funding_rate=Decimal("0.0001"),
        )

    @staticmethod
    def _execution(price: float, ts: datetime, side: str = "Buy") -> ExecutionEvent:
        return ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            exec_id=f"exec-{ts.isoformat()}",
            order_id=f"order-{ts.isoformat()}",
            order_link_id="abcdef0123456789-1715170800000",
            side=side,
            price=Decimal(str(price)),
            qty=Decimal("0.001"),
        )

    def test_first_ticker_build_carries_ticker_ts(
        self, strategy_config, mock_executor,
    ):
        """The very first ticker triggers ``build_grid`` (engine.py:151);
        the snapshot must carry the ticker's ``exchange_ts``."""
        writer = Mock()
        runner = self._runner(strategy_config, mock_executor, writer=writer)
        runner._wallet_balance = Decimal("10000")
        ticker_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        runner.on_ticker(self._ticker(50000.0, ticker_ts))

        writer.write.assert_called_once()
        assert writer.write.call_args.kwargs["exchange_ts"] == ticker_ts

    def test_restored_grid_oob_rebuild_carries_ticker_ts(
        self, strategy_config, mock_executor,
    ):
        """``engine.py:160-181``: first ticker is outside the restored
        grid's bounds → engine rebuilds around ``last_close``. The
        snapshot from this mutation must carry the ticker's ts (not the
        save's wall-clock)."""
        # Restored grid pinned far below the incoming ticker. The grid
        # must be valid for ``is_grid_correct`` to accept restoration:
        # strict-ascending prices, Buy levels < Sell levels.
        restored_grid = [
            {"side": "Buy", "price": float(p)}
            for p in range(100, 100 + strategy_config.grid_count // 2)
        ] + [{"side": "Wait", "price": float(100 + strategy_config.grid_count // 2)}] + [
            {"side": "Sell", "price": float(p)}
            for p in range(
                100 + strategy_config.grid_count // 2 + 1,
                100 + strategy_config.grid_count + 1,
            )
        ]
        writer = Mock()
        runner = self._runner(
            strategy_config, mock_executor, writer=writer,
            restored_grid=restored_grid,
        )
        runner._wallet_balance = Decimal("10000")
        # Ticker at 50000 is far above the restored grid (100..150) →
        # bounds check triggers a rebuild.
        ticker_ts = datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)
        runner.on_ticker(self._ticker(50000.0, ticker_ts))

        # At least one snapshot must carry the ticker's exchange_ts.
        assert writer.write.called
        observed = [c.kwargs["exchange_ts"] for c in writer.write.call_args_list]
        assert ticker_ts in observed
        # No wall-clock substitute.
        assert all(ts == ticker_ts for ts in observed)

    def test_deferred_fill_consumption_uses_ticker_ts(
        self, strategy_config, mock_executor,
    ):
        """``engine.py:194``: execution arrives BEFORE first ticker
        (``_fill_pending`` set, no grid yet). When ticker arrives, the
        deferred fill is consumed and ``update_grid`` mutates the grid.
        The snapshot must carry the **ticker's** ts (the moment live
        actually mutates), not the earlier execution's.
        """
        writer = Mock()
        runner = self._runner(strategy_config, mock_executor, writer=writer)
        runner._wallet_balance = Decimal("10000")
        exec_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        ticker_ts = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)

        # Execution first — engine has no last_close so just sets _fill_pending.
        runner.on_execution(self._execution(50000.0, exec_ts))
        assert writer.write.call_count == 0  # no grid mutation yet

        # Ticker — builds grid AND consumes deferred fill.
        runner.on_ticker(self._ticker(50000.0, ticker_ts))
        assert writer.write.called
        observed = [c.kwargs["exchange_ts"] for c in writer.write.call_args_list]
        # The deferred-fill consumption snapshot must carry the ticker's ts.
        assert ticker_ts in observed
        # The earlier execution's ts MUST NOT appear — live mutated at
        # ticker time, not execution time.
        assert exec_ts not in observed

    def test_check_and_place_rebuild_carries_ticker_ts(
        self, strategy_config, mock_executor,
    ):
        """``engine.py:331``: when ``_check_and_place`` sees more limits
        than the grid can fit (``len(limits) > len(grid) + 10``), it
        rebuilds the grid via ``build_grid(self.last_close)``. The
        snapshot must carry the **ticker's** ``exchange_ts`` — the
        rebuild fires inside ``_handle_ticker_event`` whose try/finally
        already pinned ``_current_exchange_ts`` to the ticker's ts.
        """
        writer = Mock()
        runner = self._runner(strategy_config, mock_executor, writer=writer)
        runner._wallet_balance = Decimal("10000")

        # First ticker — builds the grid (grid_count=50 → 51 levels).
        first_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        runner.on_ticker(self._ticker(50000.0, first_ts))
        writer.reset_mock()

        # Stub get_limit_orders so the second ticker sees > 61 long
        # orders, which triggers the too-many-orders rebuild branch
        # (engine.py:347-349). Order shapes are intentionally minimal —
        # the rebuild check just measures len().
        runner.get_limit_orders = Mock(return_value={
            "long": [
                {
                    "orderId": f"o{i}",
                    "orderLinkId": f"link-{i}",
                    "price": "50000.0",
                    "qty": "0.001",
                    "side": "Buy",
                    "reduceOnly": False,
                }
                for i in range(70)
            ],
            "short": [],
        })

        # Second ticker at the same price — bounds OK, no grid mutation from
        # the tick path itself, but _check_and_place sees too many long orders
        # and rebuilds.
        ticker_ts = datetime(2026, 1, 1, 12, 1, 0, tzinfo=UTC)
        runner.on_ticker(self._ticker(50000.0, ticker_ts))

        assert writer.write.called
        observed = [c.kwargs["exchange_ts"] for c in writer.write.call_args_list]
        # Every snapshot from this mutation carries the ticker's ts.
        assert all(ts == ticker_ts for ts in observed)
        # Earlier ticker ts must NOT bleed in via stale state.
        assert first_ts not in observed

    def test_update_grid_out_of_bounds_rebuild_carries_execution_ts(
        self, strategy_config, mock_executor,
    ):
        """``engine.py:241``: first ticker sets ``last_close``; then an
        execution at an OOB price triggers ``update_grid`` rebuild →
        ``_notify_change`` fires twice (post-rebuild intermediate +
        post-side-assignment final). Both snapshots must carry the
        execution's ``exchange_ts``.
        """
        writer = Mock()
        runner = self._runner(strategy_config, mock_executor, writer=writer)
        runner._wallet_balance = Decimal("10000")
        ticker_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        exec_ts = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)

        # First ticker — builds grid centered around 50000 with step 0.2%.
        runner.on_ticker(self._ticker(50000.0, ticker_ts))
        writer.reset_mock()

        # Execution far above the grid → update_grid OOB branch fires;
        # _notify_change is called from build_grid (intermediate) and
        # again after _assign_sides (final).
        runner.on_execution(self._execution(70000.0, exec_ts, side="Buy"))

        assert writer.write.called
        observed = [c.kwargs["exchange_ts"] for c in writer.write.call_args_list]
        # Every snapshot from this mutation carries the execution's ts.
        assert all(ts == exec_ts for ts in observed)
        # Ticker ts must NOT bleed in via stale _current_exchange_ts.
        assert ticker_ts not in observed


class TestSafetyCapsIntegration:
    """Feature 0079 (issue #182) — caps wired through the runner dispatch path."""

    def _runner(self, strategy_config, mock_executor, instrument_info, caps, **kw):
        r = StrategyRunner(
            strategy_config=strategy_config,
            executor=mock_executor,
            instrument_info=instrument_info,
            safety_caps=caps,
            **kw,
        )
        r._wallet_balance = Decimal("10000")
        return r

    def _open(self, side="Buy", direction="long", price="50000", qty="0.001"):
        return PlaceLimitIntent.create(
            symbol="BTCUSDT", side=side, price=Decimal(price), qty=Decimal(qty),
            grid_level=1, direction=direction, reduce_only=False,
        )

    def _close(self, side="Sell", direction="long", price="51000", qty="0.001"):
        return PlaceLimitIntent.create(
            symbol="BTCUSDT", side=side, price=Decimal(price), qty=Decimal(qty),
            grid_level=1, direction=direction, reduce_only=True,
        )

    def test_c1_suppresses_open_but_allows_reduce_only_close(
        self, strategy_config, mock_executor, instrument_info
    ):
        caps = SafetyCaps(
            SafetyCapsConfig(max_notional_per_symbol="500"), strat_id="btcusdt_test"
        )
        r = self._runner(strategy_config, mock_executor, instrument_info, caps)
        # Exposure at the C1 cap.
        r._long_position_value = Decimal("500")
        r._short_position_value = Decimal("0")

        # OPEN is suppressed before the executor.
        r._execute_place_intent(self._open(), {"long": [], "short": []})
        mock_executor.execute_place.assert_not_called()

        # Reduce-only close is C1-exempt and reaches the executor (position big
        # enough to pass _is_good_to_place).
        r._long_position.size = Decimal("1.0")
        r._execute_place_intent(self._close(), {"long": [], "short": []})
        mock_executor.execute_place.assert_called_once()

    def test_c2_blocks_open_and_reduce_only(
        self, strategy_config, mock_executor, instrument_info
    ):
        caps = SafetyCaps(
            SafetyCapsConfig(max_open_orders=2), strat_id="btcusdt_test"
        )
        r = self._runner(strategy_config, mock_executor, instrument_info, caps)
        r._long_position.size = Decimal("1.0")
        # Two working orders == cap.
        limits = {
            "long": [
                {"orderId": "a", "orderLinkId": "a", "price": "49000",
                 "qty": "0.001", "side": "Buy", "reduceOnly": False},
                {"orderId": "b", "orderLinkId": "b", "price": "49500",
                 "qty": "0.001", "side": "Buy", "reduceOnly": False},
            ],
            "short": [],
        }
        r._execute_place_intent(self._open(), limits)
        r._execute_place_intent(self._close(), limits)
        mock_executor.execute_place.assert_not_called()

    def test_c3_trip_cancels_working_orders_then_suppresses_places(
        self, strategy_config, mock_executor, instrument_info
    ):
        caps = SafetyCaps(
            SafetyCapsConfig(session_loss_limit="25"), strat_id="btcusdt_test"
        )
        r = self._runner(strategy_config, mock_executor, instrument_info, caps)
        # One working order so the trip has something to cancel.
        intent = self._open(price="49000", qty="0.01")
        tracked = TrackedOrder(
            client_order_id=intent.client_order_id, intent=intent, status="placed"
        )
        tracked.order_id = "wire_1"
        r._tracked_orders[intent.client_order_id] = tracked

        # A position update whose session realized PnL breaches the limit trips C3.
        r.on_position_update(
            long_position={
                "size": "0.01", "avgPrice": "50000",
                "curRealisedPnl": "-30", "leverage": "10",
            },
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )
        assert caps.loss_tripped() is True
        # Working order cancelled once via the executor (wire id, not link id).
        mock_executor.execute_cancel.assert_called_once()
        cancel_arg = mock_executor.execute_cancel.call_args.args[0]
        assert cancel_arg.order_id == "wire_1"
        assert cancel_arg.symbol == "BTCUSDT"
        assert cancel_arg.reason == "safety_cap_loss_breaker"

        # Subsequent places are suppressed by the latch.
        mock_executor.execute_place.reset_mock()
        r._execute_place_intent(self._open(), {"long": [], "short": []})
        mock_executor.execute_place.assert_not_called()

    def test_rate_limit_sentinel_is_dropped_not_enqueued(
        self, strategy_config, mock_executor, instrument_info
    ):
        # Disable the preflight so the OPEN reaches the executor unconditionally.
        cfg = strategy_config.model_copy(
            update={"preflight_balance_check_enabled": False}
        )
        on_failed = Mock()
        mock_executor.execute_place = MagicMock(
            return_value=OrderResult(success=False, error="safety_cap_rate_limit")
        )
        r = StrategyRunner(
            strategy_config=cfg,
            executor=mock_executor,
            instrument_info=instrument_info,
            on_intent_failed=on_failed,
        )
        r._wallet_balance = Decimal("10000")
        r._execute_place_intent(self._open(), {"long": [], "short": []})
        # C4 sentinel must NOT be enqueued to the retry queue.
        on_failed.assert_not_called()

    def test_non_cap_failure_still_enqueues(
        self, strategy_config, mock_executor, instrument_info
    ):
        """Discriminator: a non-safety_cap failure (110072) still enqueues."""
        cfg = strategy_config.model_copy(
            update={"preflight_balance_check_enabled": False}
        )
        on_failed = Mock()
        mock_executor.execute_place = MagicMock(
            return_value=OrderResult(
                success=False, error="place_order failed (ErrCode: 110072) duplicate"
            )
        )
        r = StrategyRunner(
            strategy_config=cfg,
            executor=mock_executor,
            instrument_info=instrument_info,
            on_intent_failed=on_failed,
        )
        r._wallet_balance = Decimal("10000")
        r._execute_place_intent(self._open(), {"long": [], "short": []})
        on_failed.assert_called_once()

    def test_c3_trip_cancels_working_orders_in_shadow_mode(
        self, shadow_config, mock_executor, instrument_info
    ):
        """C3 cancel routes through the executor even in shadow mode (the runner
        dispatches the CancelIntent; the executor honors shadow downstream)."""
        mock_executor.shadow_mode = True
        caps = SafetyCaps(
            SafetyCapsConfig(session_loss_limit="25"), strat_id="btcusdt_shadow"
        )
        r = StrategyRunner(
            strategy_config=shadow_config,
            executor=mock_executor,
            instrument_info=instrument_info,
            safety_caps=caps,
        )
        r._wallet_balance = Decimal("10000")
        intent = self._open(price="49000", qty="0.01")
        tracked = TrackedOrder(
            client_order_id=intent.client_order_id, intent=intent, status="placed"
        )
        tracked.order_id = "wire_1"
        r._tracked_orders[intent.client_order_id] = tracked

        r.on_position_update(
            long_position={
                "size": "0.01", "avgPrice": "50000",
                "curRealisedPnl": "-30", "leverage": "10",
            },
            short_position=None,
            wallet_balance=10000.0,
            last_close=50000.0,
        )
        assert caps.loss_tripped() is True
        mock_executor.execute_cancel.assert_called_once()
        assert mock_executor.execute_cancel.call_args.args[0].order_id == "wire_1"
