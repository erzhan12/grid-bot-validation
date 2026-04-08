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
        allow_shared_symbol: bool = False,
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

        try:
            # Fetch open orders from exchange
            open_orders = self._client.get_open_orders(
                symbol=runner.symbol,
                order_type="Limit",
            )
            result.orders_fetched = len(open_orders)

            logger.info(
                f"{runner.strat_id}: Fetched {len(open_orders)} open orders from exchange"
            )

            # Inject all open orders into runner.
            # We no longer send orderLinkId to Bybit, so we can't distinguish
            # "our" orders from others by orderLinkId pattern. All limit orders
            # for this symbol are assumed to belong to this strategy.
            result.orders_injected = len(open_orders)

            if open_orders:
                if not allow_shared_symbol:
                    # Check for orders without orderLinkId — likely manual orders
                    # that predate the bot. Warn loudly since we'll adopt them all.
                    no_link_id = [
                        o for o in open_orders
                        if not o.get("orderLinkId")
                    ]
                    if no_link_id:
                        logger.error(
                            f"{runner.strat_id}: Found {len(no_link_id)} orders "
                            f"without orderLinkId for {runner.symbol} — these may "
                            f"be manual orders. All will be adopted by the bot. "
                            f"Close manual orders or set allow_shared_symbol: true."
                        )

                runner.inject_open_orders(open_orders)
                logger.error(
                    f"{runner.strat_id}: Injecting {len(open_orders)} open orders. "
                    f"If any manual orders exist for {runner.symbol}, they will be "
                    f"managed by the bot. Stop the bot before placing manual orders."
                )

        except Exception as e:
            logger.error(f"{runner.strat_id}: Reconciliation error: {e}")
            result.errors.append(str(e))

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

    def build_limit_orders_dict(
        self,
        open_orders: list[dict],
    ) -> dict[str, list[dict]]:
        """Build limit_orders dict in format expected by GridEngine.

        Args:
            open_orders: List of order dicts from exchange.

        Returns:
            Dict with 'long' and 'short' keys containing order lists.
        """
        result = {"long": [], "short": []}

        for order in open_orders:
            side = order.get("side", "")
            reduce_only = order.get("reduceOnly", False)

            # Determine direction based on side and reduce_only
            # Buy + not reduce_only = opening long = long direction
            # Buy + reduce_only = closing short = short direction
            # Sell + not reduce_only = opening short = short direction
            # Sell + reduce_only = closing long = long direction
            if side == "Buy":
                direction = "short" if reduce_only else "long"
            elif side == "Sell":
                direction = "long" if reduce_only else "short"
            else:
                continue

            result[direction].append(order)

        return result

