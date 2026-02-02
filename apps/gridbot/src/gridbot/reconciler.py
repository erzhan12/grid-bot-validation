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
    """Result of a reconciliation operation."""

    orders_fetched: int = 0
    orders_injected: int = 0
    orphan_orders: int = 0
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
        if result.orphan_orders > 0:
            logger.warning(f"Found {result.orphan_orders} orphan orders")

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
        cancel_orphans: bool = False,
    ) -> ReconciliationResult:
        """Reconcile state on startup.

        Fetches open orders from exchange and injects them into the runner.
        Detects orphan orders (orders on exchange not matching our pattern).

        Args:
            runner: StrategyRunner to reconcile.
            cancel_orphans: If True, cancel orders not matching our pattern.

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

            # Separate our orders from orphans
            our_orders = []
            orphan_orders = []

            for order in open_orders:
                order_link_id = order.get("orderLinkId", "")
                # Our orders have 16-char hex client_order_id (SHA256 hash prefix)
                if order_link_id and len(order_link_id) == 16 and self._is_hex(order_link_id):
                    our_orders.append(order)
                else:
                    orphan_orders.append(order)

            result.orders_injected = len(our_orders)
            result.orphan_orders = len(orphan_orders)

            # Inject our orders into runner
            if our_orders:
                runner.inject_open_orders(our_orders)
                logger.info(f"{runner.strat_id}: Injected {len(our_orders)} orders")

            # Handle orphan orders
            if orphan_orders:
                logger.warning(
                    f"{runner.strat_id}: Found {len(orphan_orders)} orphan orders "
                    f"(orders not matching our pattern)"
                )

                if cancel_orphans:
                    for order in orphan_orders:
                        try:
                            self._client.cancel_order(
                                symbol=runner.symbol,
                                order_id=order.get("orderId"),
                            )
                            logger.info(
                                f"{runner.strat_id}: Cancelled orphan order {order.get('orderId')}"
                            )
                        except Exception as e:
                            result.errors.append(f"Failed to cancel orphan: {e}")

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

            # Get tracked orders from runner
            tracked = runner._tracked_orders
            tracked_order_ids = {
                t.order_id
                for t in tracked.values()
                if t.order_id and t.status == "placed"
            }

            # Find discrepancies
            missing_on_exchange = tracked_order_ids - exchange_order_ids
            missing_in_memory = exchange_order_ids - tracked_order_ids

            if missing_on_exchange:
                logger.warning(
                    f"{runner.strat_id}: {len(missing_on_exchange)} orders in memory "
                    f"but not on exchange (likely filled/cancelled)"
                )
                # Update tracked orders to reflect they're no longer on exchange
                for client_id, tracked_order in tracked.items():
                    if tracked_order.order_id in missing_on_exchange:
                        # Mark as cancelled (or could be filled - would need execution check)
                        tracked_order.mark_cancelled()

            if missing_in_memory:
                logger.warning(
                    f"{runner.strat_id}: {len(missing_in_memory)} orders on exchange "
                    f"but not in memory (orphans or missed updates)"
                )
                result.orphan_orders = len(missing_in_memory)

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

    def _is_hex(self, s: str) -> bool:
        """Check if string is valid hexadecimal."""
        try:
            int(s, 16)
            return True
        except ValueError:
            return False
