"""Trade-through fill simulator for backtest.

Implements the fill logic: orders fill when price crosses their limit price.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from gridcore import SideType

if TYPE_CHECKING:
    from backtest.order_manager import SimulatedOrder


@dataclass(frozen=True)
class FillResult:
    """Result of a fill check."""

    should_fill: bool
    fill_price: Decimal


class TradeThroughFillSimulator:
    """Strict cross fill model for limit orders.

    Fill logic (conservative):
    - BUY limit orders fill when current_price < limit_price
    - SELL limit orders fill when current_price > limit_price

    At limit price (price == limit), order does NOT fill because:
    - Queue position is unknown (others may be ahead)
    - Volume at that level may be insufficient
    - Conservative assumption is better for backtesting

    Fill price is always the limit price.
    """

    def check_fill(self, order: "SimulatedOrder", current_price: Decimal) -> FillResult:
        """Check if order should fill based on price crossing.

        Args:
            order: The order to check
            current_price: Current market price

        Returns:
            FillResult with should_fill flag and fill_price
        """
        should_fill = self._should_fill(order.side, order.price, current_price)
        fill_price = order.price if should_fill else Decimal("0")
        return FillResult(should_fill=should_fill, fill_price=fill_price)

    def _should_fill(self, side: str, limit_price: Decimal, current_price: Decimal) -> bool:
        """Determine if order should fill based on strict price crossing.

        Strict cross model (conservative):
        - BUY fills when market price drops BELOW limit price
        - SELL fills when market price rises ABOVE limit price
        - At limit price exactly, order does NOT fill

        Args:
            side: 'Buy' or 'Sell'
            limit_price: Order's limit price
            current_price: Current market price

        Returns:
            True if order should fill
        """
        if side == SideType.BUY:
            # Buy limit fills when price crosses below limit
            return current_price < limit_price
        else:  # Sell
            # Sell limit fills when price crosses above limit
            return current_price > limit_price

    def get_fill_price(self, order: "SimulatedOrder") -> Decimal:
        """Get fill price for an order.

        Always returns the limit price (conservative assumption).
        In reality, fills might get better prices, but we assume worst case.

        Args:
            order: The order being filled

        Returns:
            Fill price (always the limit price)
        """
        return order.price
