"""Backtest executor for intent execution.

Executes PlaceLimitIntent and CancelIntent against simulated order book.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional

from gridcore import PlaceLimitIntent, CancelIntent

from backtest.order_manager import BacktestOrderManager, SimulatedOrder


@dataclass
class OrderResult:
    """Result of order placement."""

    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class CancelResult:
    """Result of order cancellation."""

    success: bool
    error: Optional[str] = None


class BacktestExecutor:
    """Execute intents against simulated order book.

    Analogous to gridbot's IntentExecutor but for simulation.
    """

    def __init__(
        self,
        order_manager: BacktestOrderManager,
        qty_calculator: Optional[Callable[[PlaceLimitIntent, Decimal], Decimal]] = None,
    ):
        """Initialize executor.

        Args:
            order_manager: Simulated order book
            qty_calculator: Optional function to calculate order qty.
                Takes (intent, wallet_balance) and returns qty.
                If None, uses intent.qty directly.
        """
        self.order_manager = order_manager
        self.qty_calculator = qty_calculator

    def execute_place(
        self,
        intent: PlaceLimitIntent,
        timestamp: datetime,
        wallet_balance: Decimal = Decimal("0"),
    ) -> OrderResult:
        """Place order in simulation.

        Args:
            intent: PlaceLimitIntent from GridEngine
            timestamp: Current timestamp
            wallet_balance: Current wallet balance for qty calculation

        Returns:
            OrderResult with success status and order_id
        """
        # Calculate qty
        if self.qty_calculator is not None:
            qty = self.qty_calculator(intent, wallet_balance)
        else:
            qty = intent.qty

        # Skip if qty is zero or negative
        if qty <= 0:
            return OrderResult(success=False, error="qty <= 0")

        # Place order
        order = self.order_manager.place_order(
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            price=intent.price,
            qty=qty,
            direction=intent.direction,
            grid_level=intent.grid_level,
            timestamp=timestamp,
            reduce_only=intent.reduce_only,
        )

        if order is None:
            # Duplicate order (already exists with same client_order_id)
            return OrderResult(success=False, error="duplicate client_order_id")

        return OrderResult(success=True, order_id=order.order_id)

    def execute_cancel(
        self,
        intent: CancelIntent,
        timestamp: datetime,
    ) -> CancelResult:
        """Cancel order in simulation.

        Args:
            intent: CancelIntent from GridEngine
            timestamp: Current timestamp

        Returns:
            CancelResult with success status
        """
        success = self.order_manager.cancel_order(intent.order_id, timestamp)

        if not success:
            return CancelResult(success=False, error="order not found")

        return CancelResult(success=True)

    def execute_batch(
        self,
        intents: list[PlaceLimitIntent | CancelIntent],
        timestamp: datetime,
        wallet_balance: Decimal = Decimal("0"),
    ) -> tuple[list[OrderResult], list[CancelResult]]:
        """Execute batch of intents.

        Args:
            intents: List of intents to execute
            timestamp: Current timestamp
            wallet_balance: Current wallet balance for qty calculation

        Returns:
            Tuple of (place_results, cancel_results)
        """
        place_results: list[OrderResult] = []
        cancel_results: list[CancelResult] = []

        for intent in intents:
            if isinstance(intent, PlaceLimitIntent):
                result = self.execute_place(intent, timestamp, wallet_balance)
                place_results.append(result)
            elif isinstance(intent, CancelIntent):
                result = self.execute_cancel(intent, timestamp)
                cancel_results.append(result)

        return place_results, cancel_results
