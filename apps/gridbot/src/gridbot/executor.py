"""Intent executor for converting strategy intents to Bybit API calls.

The executor is the bridge between the pure strategy logic (gridcore)
and the exchange. It handles the actual order placement and cancellation.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional

from bybit_adapter.rest_client import BybitRestClient
from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridcore.position import DirectionType


logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result of order placement attempt."""

    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC)


@dataclass
class CancelResult:
    """Result of order cancellation attempt."""

    success: bool
    error: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC)


class IntentExecutor:
    """Executes trading intents against Bybit API.

    Converts PlaceLimitIntent and CancelIntent objects from gridcore
    into actual API calls to Bybit.

    In shadow mode, logs intents without executing them.

    Example:
        client = BybitRestClient(api_key="...", api_secret="...", testnet=True)
        executor = IntentExecutor(client, shadow_mode=False)

        result = executor.execute_place(intent)
        if result.success:
            print(f"Order placed: {result.order_id}")
    """

    def __init__(
        self,
        rest_client: BybitRestClient,
        shadow_mode: bool = False,
        position_idx_long: int = 1,
        position_idx_short: int = 2,
    ):
        """Initialize executor.

        Args:
            rest_client: Bybit REST client for API calls.
            shadow_mode: If True, log intents without executing.
            position_idx_long: Position index for long direction (hedge mode).
            position_idx_short: Position index for short direction (hedge mode).
        """
        self._client = rest_client
        self._shadow_mode = shadow_mode
        self._position_idx_long = position_idx_long
        self._position_idx_short = position_idx_short

    @property
    def shadow_mode(self) -> bool:
        """Whether executor is in shadow mode."""
        return self._shadow_mode

    def execute_place(self, intent: PlaceLimitIntent) -> OrderResult:
        """Execute a place order intent.

        Args:
            intent: PlaceLimitIntent from strategy.

        Returns:
            OrderResult with success status and order_id if successful.
        """
        if self._shadow_mode:
            logger.info(
                f"[SHADOW] Would place {intent.side} order: "
                f"{intent.symbol} qty={intent.qty} price={intent.price} "
                f"reduce_only={intent.reduce_only} client_id={intent.client_order_id}"
            )
            return OrderResult(
                success=True,
                order_id=f"shadow_{intent.client_order_id}",
            )

        try:
            # Determine position index based on direction
            position_idx = self._get_position_idx(intent.direction)

            result = self._client.place_order(
                symbol=intent.symbol,
                side=intent.side,
                order_type="Limit",
                qty=str(intent.qty),
                price=str(intent.price),
                reduce_only=intent.reduce_only,
                position_idx=position_idx,
                order_link_id=intent.client_order_id,
            )

            order_id = result.get("orderId")
            logger.info(
                f"Placed {intent.side} order: {intent.symbol} "
                f"qty={intent.qty} price={intent.price} order_id={order_id}"
            )

            return OrderResult(success=True, order_id=order_id)

        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return OrderResult(success=False, error=str(e))

    def execute_cancel(self, intent: CancelIntent) -> CancelResult:
        """Execute a cancel order intent.

        Args:
            intent: CancelIntent from strategy.

        Returns:
            CancelResult with success status.
        """
        if self._shadow_mode:
            logger.info(
                f"[SHADOW] Would cancel order: {intent.symbol} "
                f"order_id={intent.order_id} reason={intent.reason}"
            )
            return CancelResult(success=True)

        try:
            success = self._client.cancel_order(
                symbol=intent.symbol,
                order_id=intent.order_id,
            )

            if success:
                logger.info(
                    f"Cancelled order: {intent.symbol} "
                    f"order_id={intent.order_id} reason={intent.reason}"
                )
            else:
                logger.warning(
                    f"Cancel returned False: {intent.symbol} "
                    f"order_id={intent.order_id} (may already be filled/cancelled)"
                )

            return CancelResult(success=success)

        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            return CancelResult(success=False, error=str(e))

    def execute_batch(
        self,
        intents: list[PlaceLimitIntent | CancelIntent],
    ) -> list[OrderResult | CancelResult]:
        """Execute a batch of intents sequentially.

        Args:
            intents: List of PlaceLimitIntent or CancelIntent.

        Returns:
            List of results in same order as intents.
        """
        results = []

        for intent in intents:
            if isinstance(intent, PlaceLimitIntent):
                result = self.execute_place(intent)
            elif isinstance(intent, CancelIntent):
                result = self.execute_cancel(intent)
            else:
                logger.warning(f"Unknown intent type: {type(intent)}")
                continue

            results.append(result)

        return results

    def _get_position_idx(self, direction: str) -> int:
        """Get position index for direction (hedge mode).

        Args:
            direction: 'long' or 'short'

        Returns:
            Position index (1 for long, 2 for short in hedge mode)

        Reference:
            bbu_reference/bbu2-master/bybit_api_usdt.py:275-280
        """
        if direction == DirectionType.LONG:
            return self._position_idx_long
        elif direction == DirectionType.SHORT:
            return self._position_idx_short
        return 0  # One-way mode
