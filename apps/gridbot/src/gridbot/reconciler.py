"""Reconciliation module for syncing engine state with exchange.

Handles:
- Startup reconciliation (fetch open orders, inject into runner)
- Reconnect reconciliation (detect gaps, sync state)
- Orphan order detection
"""

import logging
from dataclasses import dataclass
from typing import Optional

from bybit_adapter.rest_client import BybitRestClient

from gridbot.runner import StrategyRunner


logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    """Result of a reconciliation operation.

    Attributes:
        orders_fetched: Number of open orders fetched from exchange.
        orders_injected: Number of orders injected into the runner.
        untracked_orders_on_exchange: Orders on exchange with no matching
            in-memory tracked order (only set during reconnect reconciliation).
        errors: List of error messages encountered.
    """

    orders_fetched: int = 0
    orders_injected: int = 0
    untracked_orders_on_exchange: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class Reconciler:
    """Handles state reconciliation between engine and exchange.

    Reconciliation ensures that the in-memory order tracking state
    matches the actual state on the exchange.

    Example:
        reconciler = Reconciler(rest_client)

        # On startup
        result = await reconciler.reconcile_startup(runner)
        if result.untracked_orders_on_exchange > 0:
            logger.warning(f"Found {result.untracked_orders_on_exchange} orphan orders")

        # On WebSocket reconnect
        result = await reconciler.reconcile_reconnect(runner)
    """

    def __init__(self, rest_client: BybitRestClient):
        """Initialize reconciler.

        Args:
            rest_client: Bybit REST client for fetching exchange state.
        """
        self._client = rest_client

    async def reconcile_startup(
        self,
        runner: StrategyRunner,
    ) -> ReconciliationResult:
        """Reconcile state on startup.

        Fetches all open limit orders from exchange and injects them into the runner.
        Since we no longer send orderLinkId to Bybit, all orders for this symbol
        are assumed to belong to this strategy.

        Args:
            runner: StrategyRunner to reconcile.

        Returns:
            ReconciliationResult with operation details.
        """
        result = ReconciliationResult()

        # Fetch open orders from exchange (API errors caught here)
        try:
            open_orders = self._client.get_open_orders(
                symbol=runner.symbol,
                order_type="Limit",
            )
        except Exception as e:
            logger.error(f"{runner.strat_id}: Reconciliation error: {e}")
            result.errors.append(str(e))
            return result

        result.orders_fetched = len(open_orders)
        logger.info(
            f"{runner.strat_id}: Fetched {len(open_orders)} open orders from exchange"
        )

        # Inject all open orders into runner.
        # We no longer send orderLinkId to Bybit, so we can't distinguish
        # "our" orders from others by orderLinkId presence. All limit orders
        # for this symbol are assumed to belong to this strategy. Two things
        # make that assumption safe:
        #   (1) Multi-strategy collisions are impossible: GridbotConfig
        #       rejects any configuration with two strategies on the same
        #       (account, symbol) pair at load time (see
        #       config.py:validate_no_shared_symbol). bbu2 enforces the same
        #       invariant structurally via its one-scalar-strat-per-account
        #       schema.
        #   (2) Manual orders are handled by the engine, not by reconciler.
        #       See the IMPORTANT block below for the mechanism.
        #
        # IMPORTANT: "adoption" here lasts exactly one ticker event. On the
        # first on_ticker after startup, GridEngine._place_grid_orders
        # (packages/gridcore/src/gridcore/engine.py:319-325) cancels any
        # injected order whose price is not in the current grid_price_set
        # ('outside_grid' reason), and engine.py:305-312 cancels any at a
        # grid price with the wrong side ('side_mismatch' reason). Over-limit
        # cases (engine.py:237-243) trigger a full rebuild that cancels
        # everything. bbu2 reference: strat.py:154-160, :145-149, :103-104.
        # Do NOT add a refuse-to-start check here — it would re-break normal
        # crash-restart (bot's own prior orders look identical to manual ones)
        # and was already removed in commit 138737a for that reason.
        result.orders_injected = len(open_orders)

        if open_orders:
            runner.inject_open_orders(open_orders)
            logger.warning(
                f"{runner.strat_id}: WARNING — injecting {len(open_orders)} open "
                f"orders for {runner.symbol}. If any manual orders exist, they "
                f"will be adopted by the bot. Stop the bot before placing manual "
                f"orders on this symbol."
            )

        return result

    async def reconcile_reconnect(
        self,
        runner: StrategyRunner,
    ) -> ReconciliationResult:
        """Reconcile state after WebSocket reconnection.

        Compares exchange state with in-memory state and logs discrepancies.

        Args:
            runner: StrategyRunner to reconcile.

        Returns:
            ReconciliationResult with operation details.
        """
        result = ReconciliationResult()

        try:
            # Fetch current open orders from exchange
            open_orders = self._client.get_open_orders(
                symbol=runner.symbol,
                order_type="Limit",
            )
            result.orders_fetched = len(open_orders)

            # Build set of exchange order IDs
            exchange_order_ids = {
                order.get("orderId")
                for order in open_orders
                if order.get("orderId")
            }

            # Get placed order IDs from runner
            tracked_order_ids = runner.get_placed_order_ids()

            # Find discrepancies
            missing_on_exchange = tracked_order_ids - exchange_order_ids
            missing_in_memory = exchange_order_ids - tracked_order_ids

            if missing_on_exchange:
                logger.warning(
                    f"{runner.strat_id}: {len(missing_on_exchange)} orders in memory "
                    f"but not on exchange (likely filled/cancelled)"
                )
                # Update tracked orders to reflect they're no longer on exchange
                for order_id in missing_on_exchange:
                    runner.mark_order_cancelled_by_order_id(order_id)

            if missing_in_memory:
                logger.warning(
                    f"{runner.strat_id}: {len(missing_in_memory)} orders on exchange "
                    f"but not in memory (orphans or missed updates)"
                )
                result.untracked_orders_on_exchange = len(missing_in_memory)

                # Inject missing orders
                orders_to_inject = [
                    o for o in open_orders
                    if o.get("orderId") in missing_in_memory
                ]
                if orders_to_inject:
                    runner.inject_open_orders(orders_to_inject)
                    result.orders_injected = len(orders_to_inject)

        except Exception as e:
            logger.error(f"{runner.strat_id}: Reconnect reconciliation error: {e}")
            result.errors.append(str(e))

        return result

