"""Position tracking for backtest with PnL calculations.

Tracks position size, entry price, and calculates realized/unrealized PnL.
Separate from gridcore.Position which handles risk multipliers.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from gridcore import DirectionType, SideType
from gridcore.pnl import (
    MMTiers,
    calc_initial_margin,
    calc_maintenance_margin,
    calc_position_value,
    calc_unrealised_pnl,
    calc_unrealised_pnl_pct,
)


logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    """Current position state."""

    size: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_entry_price: Decimal = field(default_factory=lambda: Decimal("0"))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealized_pnl_percent: Decimal = field(default_factory=lambda: Decimal("0"))
    commission_paid: Decimal = field(default_factory=lambda: Decimal("0"))
    funding_paid: Decimal = field(default_factory=lambda: Decimal("0"))

    # Margin fields (calculated via gridcore.pnl functions)
    position_value: Decimal = field(default_factory=lambda: Decimal("0"))
    initial_margin: Decimal = field(default_factory=lambda: Decimal("0"))
    imr_rate: Decimal = field(default_factory=lambda: Decimal("0"))
    maintenance_margin: Decimal = field(default_factory=lambda: Decimal("0"))
    mmr_rate: Decimal = field(default_factory=lambda: Decimal("0"))


class BacktestPositionTracker:
    """Track position and calculate PnL for backtest.

    Handles position changes from fills and calculates:
    - Average entry price (weighted by size)
    - Realized PnL when reducing/closing position
    - Unrealized PnL at current price
    - Commission tracking
    - Funding payment tracking
    """

    def __init__(
        self,
        direction: str,
        commission_rate: Decimal = Decimal("0.0002"),
        leverage: int = 10,
        tiers: Optional[MMTiers] = None,
        symbol: str = "",
    ):
        """Initialize position tracker.

        Args:
            direction: 'long' or 'short' (or DirectionType enum)
            commission_rate: Commission rate per trade (default 0.02%)
            leverage: Position leverage for IM calculation
            tiers: MM tier table (from RiskLimitProvider or hardcoded fallback)
            symbol: Trading symbol (used for MM fallback when tiers is None)
        """
        if direction not in (DirectionType.LONG, DirectionType.SHORT):
            raise ValueError(f"direction must be '{DirectionType.LONG}' or '{DirectionType.SHORT}', got '{direction}'")
        if commission_rate < 0 or commission_rate > Decimal("0.01"):
            raise ValueError(f"Commission rate {commission_rate} outside expected range [0, 0.01]")

        self.direction = direction
        self.commission_rate = commission_rate
        self.leverage = Decimal(str(leverage))
        self.tiers = tiers
        self.symbol = symbol
        self.state = PositionState()

    def process_fill(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
    ) -> Decimal:
        """Process a fill and update position.

        Args:
            side: 'Buy' or 'Sell'
            qty: Fill quantity
            price: Fill price

        Returns:
            Realized PnL from this fill (0 if opening/adding to position)
        """
        if price <= 0 or qty <= 0:
            raise ValueError(f"Invalid fill: price={price}, qty={qty}")

        # Calculate and deduct commission
        commission = qty * price * self.commission_rate
        self.state.commission_paid += commission

        # Determine if this fill opens or closes position
        is_opening = self._is_opening_fill(side)

        if is_opening:
            return self._add_to_position(qty, price)
        else:
            return self._reduce_position(qty, price)

    def _is_opening_fill(self, side: str) -> bool:
        """Determine if fill opens/adds to position or reduces it.

        Long position:
        - Buy = opening/adding
        - Sell = reducing/closing

        Short position:
        - Sell = opening/adding
        - Buy = reducing/closing (covering)
        """
        if self.direction == DirectionType.LONG:
            return side == SideType.BUY
        else:  # short
            return side == SideType.SELL

    def _add_to_position(self, qty: Decimal, price: Decimal) -> Decimal:
        """Add to position (no realized PnL).

        Updates average entry price using weighted average.
        """
        old_size = self.state.size
        old_value = old_size * self.state.avg_entry_price
        new_value = qty * price

        self.state.size = old_size + qty

        if self.state.size > 0:
            self.state.avg_entry_price = (old_value + new_value) / self.state.size
        else:
            self.state.avg_entry_price = Decimal("0")

        return Decimal("0")  # No realized PnL when adding

    def _reduce_position(self, qty: Decimal, price: Decimal) -> Decimal:
        """Reduce position and calculate realized PnL.

        Returns realized PnL based on entry vs exit price.
        """
        if self.state.size == 0:
            # No position to reduce, treat as opening opposite direction
            # (but we don't track opposite, so just ignore)
            return Decimal("0")

        # Clamp qty to position size (can't reduce more than we have)
        close_qty = min(qty, self.state.size)

        # Calculate realized PnL
        realized_pnl = calc_unrealised_pnl(
            self.direction, self.state.avg_entry_price, price, close_qty
        )

        self.state.realized_pnl += realized_pnl
        self.state.size -= close_qty

        # Reset avg_entry_price if fully closed
        if self.state.size == 0:
            self.state.avg_entry_price = Decimal("0")

        return realized_pnl

    def calculate_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Calculate unrealized PnL at current price.

        Also recalculates and caches margin metrics (IM/MM) in ``self.state``
        since position value depends on entry price which may have changed
        from fills. Margin fields remain valid until the next call.

        Args:
            current_price: Current market price

        Returns:
            Unrealized PnL (positive = profit, negative = loss)
        """
        if self.state.size == 0:
            self.state.unrealized_pnl = Decimal("0")
            self._reset_margin()
            return Decimal("0")

        unrealized = calc_unrealised_pnl(
            self.direction, self.state.avg_entry_price, current_price, self.state.size
        )
        self.state.unrealized_pnl = unrealized
        self._update_margin()
        return unrealized

    def _update_margin(self) -> None:
        """Recalculate IM/MM from current position state.

        Uses the same gridcore functions as pnl_checker:
        - calc_position_value(size, entry_price)
        - calc_initial_margin(position_value, leverage, symbol, tiers)
        - calc_maintenance_margin(position_value, symbol, tiers)
        """
        pv = calc_position_value(self.state.size, self.state.avg_entry_price)
        self.state.position_value = pv
        im, imr = calc_initial_margin(pv, self.leverage, self.symbol, tiers=self.tiers)
        self.state.initial_margin = im
        self.state.imr_rate = imr
        mm, mmr = calc_maintenance_margin(pv, self.symbol, tiers=self.tiers)
        self.state.maintenance_margin = mm
        self.state.mmr_rate = mmr

    def _reset_margin(self) -> None:
        """Zero out margin fields when position is closed."""
        self.state.position_value = Decimal("0")
        self.state.initial_margin = Decimal("0")
        self.state.imr_rate = Decimal("0")
        self.state.maintenance_margin = Decimal("0")
        self.state.mmr_rate = Decimal("0")

    def calculate_unrealized_pnl_percent(
        self, current_price: Decimal, leverage: Decimal
    ) -> Decimal:
        """Calculate unrealized PnL as percentage (ROE).

        Formula from bbu2:
        - Long: (1/entry - 1/close) * entry * 100 * leverage
        - Short: (1/close - 1/entry) * entry * 100 * leverage

        Args:
            current_price: Current market price
            leverage: Position leverage

        Returns:
            Unrealized PnL percentage (ROE)
        """
        if self.state.size == 0 or current_price == 0 or self.state.avg_entry_price == 0:
            self.state.unrealized_pnl_percent = Decimal("0")
            return Decimal("0")

        pnl_percent = calc_unrealised_pnl_pct(
            self.direction, self.state.avg_entry_price, current_price, leverage
        )
        self.state.unrealized_pnl_percent = pnl_percent
        return pnl_percent

    def apply_funding(self, rate: Decimal, current_price: Decimal) -> Decimal:
        """Apply funding payment and return amount.

        Funding is calculated on position notional value.
        - Long pays when rate > 0
        - Short receives when rate > 0
        (and vice versa when rate < 0)

        Args:
            rate: Funding rate (e.g., 0.0001 = 0.01%)
            current_price: Current price for notional calculation

        Returns:
            Funding payment amount (negative = paid, positive = received)
        """
        if abs(rate) > Decimal("0.01"):
            logger.warning("Unusually high funding rate: %s", rate)

        if self.state.size == 0:
            return Decimal("0")

        notional = self.state.size * current_price
        funding = notional * rate

        # Long pays, short receives when rate > 0
        if self.direction == DirectionType.LONG:
            payment = -funding  # Negative = paying
        else:  # short
            payment = funding  # Positive = receiving

        self.state.funding_paid -= payment  # Track total paid (negative payment = positive tracking)
        return payment

    def get_total_pnl(self) -> Decimal:
        """Get total PnL including realized, unrealized, commission, funding."""
        return (
            self.state.realized_pnl
            + self.state.unrealized_pnl
            - self.state.commission_paid
            - self.state.funding_paid  # funding_paid is positive when we paid
        )

    @property
    def has_position(self) -> bool:
        """Check if there's an open position."""
        return self.state.size > 0
