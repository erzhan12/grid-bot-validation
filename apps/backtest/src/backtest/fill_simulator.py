"""Trade-through fill simulator for backtest.

Implements the fill logic: orders fill when price crosses their limit price.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from gridcore import SideType, TickerEvent

if TYPE_CHECKING:
    from backtest.order_manager import SimulatedOrder


class FillMode(StrEnum):
    """Supported simulated limit-order fill modes."""

    STRICT_CROSS = "strict_cross"
    TRADE_THROUGH_AT_LIMIT = "trade_through_at_limit"
    BOOK_TOUCH = "book_touch"


@dataclass(frozen=True)
class FillResult:
    """Result of a fill check."""

    should_fill: bool
    fill_price: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    """Internal normalized market data used for fill checks."""

    last_price: Decimal
    bid1_price: Decimal | None = None
    ask1_price: Decimal | None = None


class TradeThroughFillSimulator:
    """Configurable fill model for simulated limit orders.

    Supported modes:
    - strict_cross: conservative default; BUY below limit, SELL above limit.
      Exact limit touches do not fill because queue position and volume are
      unknown.
    - trade_through_at_limit: last-price model that includes exact limit
      touches.
    - book_touch: L1-aware parity mode; BUY when ask touches/crosses limit,
      SELL when bid touches/crosses limit, with last-price-at-limit fallback
      only when L1 data is unavailable.

    Fill price is always the limit price.
    """

    def __init__(self, mode: FillMode = FillMode.STRICT_CROSS):
        try:
            self._mode = FillMode(mode)
        except ValueError as exc:
            valid = ", ".join(m.value for m in FillMode)
            raise ValueError(
                f"Invalid fill mode {mode!r}. Valid modes: {valid}"
            ) from exc

    @property
    def mode(self) -> FillMode:
        """Fill mode used by this simulator."""
        return self._mode

    def check_fill(
        self,
        order: "SimulatedOrder",
        market: TickerEvent | Decimal,
    ) -> FillResult:
        """Check if order should fill based on the configured fill mode.

        Args:
            order: The order to check
            market: TickerEvent for L1-aware modes, or legacy bare Decimal.
                Bare Decimal is retained for backward compatibility; prefer
                TickerEvent for new callers.

        Returns:
            FillResult with should_fill flag and fill_price
        """
        snapshot = self._to_snapshot(market)
        should_fill = self._should_fill(order.side, order.price, snapshot)
        fill_price = order.price if should_fill else Decimal("0")
        return FillResult(should_fill=should_fill, fill_price=fill_price)

    def _to_snapshot(self, market: TickerEvent | Decimal) -> MarketSnapshot:
        """Normalize supported market inputs to an internal snapshot."""
        if isinstance(market, TickerEvent):
            return MarketSnapshot(
                last_price=market.last_price,
                bid1_price=self._normalize_l1_price(market.bid1_price),
                ask1_price=self._normalize_l1_price(market.ask1_price),
            )

        return MarketSnapshot(last_price=market)

    def _normalize_l1_price(self, price: Decimal) -> Decimal | None:
        """Treat non-positive L1 defaults as missing book data."""
        return price if price > 0 else None

    def _should_fill(
        self,
        side: str,
        limit_price: Decimal,
        snapshot: MarketSnapshot,
    ) -> bool:
        """Determine if order should fill based on configured mode.

        Modes:
        - strict_cross: BUY below limit, SELL above limit
        - trade_through_at_limit: BUY at/below, SELL at/above
        - book_touch: BUY ask at/below, SELL bid at/above; falls back to
          trade_through_at_limit when L1 data is unavailable

        Args:
            side: 'Buy' or 'Sell'
            limit_price: Order's limit price
            snapshot: Normalized market snapshot

        Returns:
            True if order should fill
        """
        match self._mode:
            case FillMode.STRICT_CROSS:
                return self._should_fill_strict_cross(
                    side, limit_price, snapshot.last_price,
                )
            case FillMode.TRADE_THROUGH_AT_LIMIT:
                return self._should_fill_at_limit(
                    side, limit_price, snapshot.last_price,
                )
            case FillMode.BOOK_TOUCH:
                return self._should_fill_book_touch(side, limit_price, snapshot)

        raise ValueError(f"Unsupported fill mode: {self._mode}")

    def _should_fill_strict_cross(
        self,
        side: str,
        limit_price: Decimal,
        current_price: Decimal,
    ) -> bool:
        """Strict conservative fill check."""
        if current_price <= 0:
            return False
        if side == SideType.BUY:
            # Buy limit fills when price crosses below limit
            return current_price < limit_price
        else:  # Sell
            # Sell limit fills when price crosses above limit
            return current_price > limit_price

    def _should_fill_at_limit(
        self,
        side: str,
        limit_price: Decimal,
        current_price: Decimal,
    ) -> bool:
        """Trade-through fill check that includes exact limit touches."""
        if current_price <= 0:
            return False
        if side == SideType.BUY:
            return current_price <= limit_price
        else:  # Sell
            return current_price >= limit_price

    def _should_fill_book_touch(
        self,
        side: str,
        limit_price: Decimal,
        snapshot: MarketSnapshot,
    ) -> bool:
        """L1-aware fill check with legacy last-price fallback."""
        if side == SideType.BUY:
            if snapshot.ask1_price is None:
                return self._should_fill_at_limit(side, limit_price, snapshot.last_price)
            return snapshot.ask1_price <= limit_price
        elif side == SideType.SELL:
            if snapshot.bid1_price is None:
                return self._should_fill_at_limit(side, limit_price, snapshot.last_price)
            return snapshot.bid1_price >= limit_price
        else:
            raise ValueError(f"Invalid order side: {side!r}")

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
