"""Tests for issue #206 — startup reconciliation failure handling.

Original fail-open scenario (pre-0086 behavior, mirrors the orchestrator flow):

1. Startup reconciliation REST call fails once (transient timeout).
   ``Reconciler.reconcile_startup`` swallows the exception into
   ``result.errors`` (reconciler.py:95-98) and the orchestrator ignores
   ``result.errors`` entirely (orchestrator.py:333-343) — startup proceeds
   with an EMPTY local order book while the old grid is still live on the
   exchange.
2. First ticker arrives (orchestrator._tick step 3 runs BEFORE the
   first-tick order sync in step 4): the engine builds a fresh grid over
   the empty state and places new orders at price levels where the old
   exchange orders already sit → live duplicates.
3. First-tick order sync (``reconcile_reconnect``) then injects the old
   orders — state is repaired, but the damage is not:
4. On the next ticker the engine indexes limits with a price-keyed dict
   (engine.py:359 ``limit_prices = {float(limit['price']): limit ...}``)
   which collapses same-price duplicates — the shadowed order is neither
   'outside_grid' nor 'side_mismatch', so it is NEVER cancelled. Double
   exposure persists until filled.

Feature 0086 fixed the fail-open startup (fail-closed + alert); the
orchestrator-level test below asserts the fixed behavior. The runner-level
test still documents the engine same-price duplicate-collapse gap (fix 2 of
the issue #206 analysis), which is tracked separately.
"""

import logging
from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch

import pytest

from gridcore import TickerEvent, EventType, InstrumentInfo
from gridcore.intents import PlaceLimitIntent

from gridbot.config import GridbotConfig, AccountConfig, StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult, CancelResult
from gridbot.notifier import Notifier
from gridbot.orchestrator import Orchestrator, StartupReconciliationError
from gridbot.reconciler import Reconciler
from gridbot.runner import StrategyRunner

logger = logging.getLogger(__name__)


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
def recording_executor():
    """Mock executor that assigns unique exchange order IDs and records intents."""
    executor = Mock(spec=IntentExecutor)
    executor.shadow_mode = False
    executor.auth_cooldown = False
    executor.placed_intents = []
    executor.cancelled_intents = []

    def _place(intent):
        executor.placed_intents.append(intent)
        return OrderResult(
            success=True, order_id=f"new-{len(executor.placed_intents)}"
        )

    def _cancel(intent):
        executor.cancelled_intents.append(intent)
        return CancelResult(success=True)

    executor.execute_place = MagicMock(side_effect=_place)
    executor.execute_cancel = MagicMock(side_effect=_cancel)
    return executor


@pytest.fixture
def runner(strategy_config, recording_executor, instrument_info):
    """Real StrategyRunner + real engine; only the executor is mocked."""
    r = StrategyRunner(
        strategy_config=strategy_config,
        executor=recording_executor,
        instrument_info=instrument_info,
    )
    # Set wallet balance so qty_calculator can resolve (x0.001 = 0.1% of wallet)
    r._wallet_balance = Decimal("10000")
    return r


def _ticker(price: str) -> TickerEvent:
    p = Decimal(price)
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        exchange_ts=datetime.now(UTC),
        local_ts=datetime.now(UTC),
        last_price=p,
        mark_price=p,
        bid1_price=p - Decimal("1"),
        ask1_price=p + Decimal("1"),
        funding_rate=Decimal("0.0001"),
    )


def _exchange_order_dict(intent: PlaceLimitIntent, order_id: str, *, link_id: str | None) -> dict:
    """Order dict in the shape get_open_orders() returns."""
    d = {
        "orderId": order_id,
        "symbol": intent.symbol,
        "price": str(intent.price),
        "qty": "0.001",
        "side": intent.side,
        "reduceOnly": intent.reduce_only,
    }
    if link_id is not None:
        d["orderLinkId"] = link_id
    return d


class TestStartupReconcileFailClosed:
    """Orchestrator-level: startup reconciliation errors abort startup (0086)."""

    @patch("gridbot.orchestrator.time.sleep")
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_startup_reconcile_error_aborts_startup_and_alerts(
        self, mock_private_ws, mock_public_ws, mock_rest_client, mock_sleep,
    ):
        """Fix for issue #206: persistent get_open_orders failure at startup
        raises StartupReconciliationError and emits a notifier alert — the
        bot never goes live on an unconfirmed order book."""
        config = GridbotConfig(
            accounts=[AccountConfig(
                name="test_account", api_key="k", api_secret="s", testnet=True,
            )],
            strategies=[StrategyConfig(
                strat_id="btcusdt_test", account="test_account",
                symbol="BTCUSDT", tick_size=Decimal("0.1"),
                grid_count=20, grid_step=0.2, shadow_mode=False,
            )],
            database_url="sqlite:///:memory:",
            position_check_interval=60.0,
        )
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(config, notifier=notifier)
        mock_rest_client.return_value.get_open_orders = Mock(
            side_effect=RuntimeError("REST timeout during startup reconcile")
        )

        try:
            with pytest.raises(StartupReconciliationError):
                orchestrator.start()

            assert orchestrator.running is False
            alert_texts = [
                str(c.args[0]) for c in notifier.alert.call_args_list
            ]
            assert any("startup reconciliation failed" in t for t in alert_texts)
        finally:
            orchestrator.stop()


class TestStartupReconcileRaceDuplicates:
    """Runner-level: the full duplicate-order mechanism, step by step."""

    def test_race_places_duplicates_and_sync_cannot_heal_them(
        self, runner, recording_executor,
    ):
        """REPRO issue #206: failed startup reconcile → first ticker places a
        fresh grid over live legacy orders → order sync adopts the legacy
        orders → next ticker never cancels the same-price duplicates."""
        rest = Mock()
        reconciler = Reconciler(rest)

        # --- Step 1: startup reconcile fails transiently (swallowed).
        rest.get_open_orders = Mock(
            side_effect=RuntimeError("REST timeout during startup reconcile")
        )
        result = reconciler.reconcile_startup(runner)
        assert result.errors, "startup reconcile must have recorded the error"
        assert result.orders_injected == 0
        logger.info("STEP1: startup reconcile failed, errors=%s", result.errors)

        # --- Step 2: first ticker (orchestrator._tick step 3) — engine
        # builds grid on EMPTY state and places new orders.
        runner.on_ticker(_ticker("50000"))
        new_intents = list(recording_executor.placed_intents)
        assert new_intents, "engine placed a fresh grid"
        logger.info(
            "STEP2: engine placed %d new orders at prices %s",
            len(new_intents), [str(i.price) for i in new_intents],
        )

        # The exchange ALSO still holds the legacy grid from the previous
        # session at (some of) the same price levels. Pick one Buy and one
        # Sell level actually placed in step 2.
        legacy_buy_src = next(i for i in new_intents if i.side == "Buy")
        legacy_sell_src = next(i for i in new_intents if i.side == "Sell")
        legacy_orders = [
            _exchange_order_dict(legacy_buy_src, "legacy-buy", link_id=None),
            _exchange_order_dict(legacy_sell_src, "legacy-sell", link_id=None),
        ]

        # DUPLICATES EXIST: new orders were placed at prices where legacy
        # orders already sit (the bot could not know — reconcile had failed).
        dup_prices = {str(legacy_buy_src.price), str(legacy_sell_src.price)}
        logger.warning("STEP2: duplicate exposure at prices %s", dup_prices)

        # --- Step 3: first-tick order sync (orchestrator._tick step 4).
        # Exchange now reports legacy + new orders; legacy ones are unknown
        # to the runner and get injected.
        new_order_dicts = [
            _exchange_order_dict(
                intent, f"new-{n}", link_id=intent.client_order_id,
            )
            for n, intent in enumerate(new_intents, start=1)
        ]
        rest.get_open_orders = Mock(return_value=legacy_orders + new_order_dicts)
        sync_result = reconciler.reconcile_reconnect(runner)
        assert sync_result.untracked_orders_on_exchange == 2
        assert sync_result.orders_injected == 2
        logger.info(
            "STEP3: order sync adopted %d legacy orders", sync_result.orders_injected,
        )

        # --- Step 4: next ticker. State is now "repaired", but the engine
        # indexes limits by price (engine.py:359) — same-price duplicates
        # collapse and the shadowed order is never cancelled.
        recording_executor.cancelled_intents.clear()
        runner.on_ticker(_ticker("50000"))

        cancelled_ids = {
            c.order_id for c in recording_executor.cancelled_intents
        }
        # BUG: neither legacy duplicate is cancelled.
        assert "legacy-buy" not in cancelled_ids
        assert "legacy-sell" not in cancelled_ids
        logger.warning(
            "STEP4: cancels issued=%s — legacy duplicates survive", cancelled_ids,
        )

        # BUG: both duplicates remain tracked as live → 2x exposure at
        # those grid levels until one side fills.
        limits = runner.get_limit_orders()
        all_limits = limits["long"] + limits["short"]
        for price in dup_prices:
            at_price = [lim for lim in all_limits if str(lim["price"]) == price]
            assert len(at_price) == 2, (
                f"expected duplicate pair at {price}, got {at_price}"
            )
        logger.warning(
            "FINAL: %d live orders across %d tracked; duplicate pairs at %s",
            len(all_limits), runner.get_tracked_order_count()["placed"], dup_prices,
        )
