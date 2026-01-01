"""
Position state tracking and risk management.

Extracted from bbu2-master/position.py with exchange-specific dependencies removed.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class PositionState:
    """
    Position state snapshot.

    Represents the current state of a position (long or short) without
    any exchange-specific dependencies.
    """
    direction: str  # 'long' or 'short'
    size: Decimal = Decimal('0')
    entry_price: Optional[Decimal] = None
    unrealized_pnl: Decimal = Decimal('0')
    margin: Decimal = Decimal('0')
    liquidation_price: Decimal = Decimal('0')
    leverage: int = 1
    position_value: Decimal = Decimal('0')


@dataclass
class RiskConfig:
    """
    Risk management configuration for position sizing.

    Reference: bbu2-master/position.py:15-18
    """
    min_liq_ratio: float
    max_liq_ratio: float
    max_margin: float
    min_total_margin: float
    increase_same_position_on_low_margin: bool = False


class PositionRiskManager:
    """
    Position risk management and amount multiplier calculation.

    Extracted from bbu2-master/position.py Position class, specifically
    the __calc_amount_multiplier logic (lines 52-92).

    This class calculates order size multipliers based on position state,
    liquidation risk, and margin levels.
    """

    SIDE_BUY = 'Buy'
    SIDE_SELL = 'Sell'

    def __init__(self, direction: str, risk_config: RiskConfig):
        """
        Initialize position risk manager.

        Args:
            direction: 'long' or 'short'
            risk_config: Risk management parameters
        """
        self.direction = direction
        self.risk_config = risk_config
        self.amount_multiplier = {self.SIDE_BUY: 1.0, self.SIDE_SELL: 1.0}
        self.position_ratio = 1.0
        self.unrealized_pnl_pct = 0.0

    def reset_amount_multiplier(self) -> None:
        """
        Reset all multipliers to 1.0.

        Reference: bbu2-master/position.py:33-35
        """
        self.amount_multiplier[self.SIDE_BUY] = 1.0
        self.amount_multiplier[self.SIDE_SELL] = 1.0

    def calculate_amount_multiplier(
        self,
        position: PositionState,
        opposite_position: PositionState,
        last_close: float,
        wallet_balance: Decimal
    ) -> dict[str, float]:
        """
        Calculate order size multipliers based on position state.

        Reference: bbu2-master/position.py:52-92

        Args:
            position: Current position state
            opposite_position: Opposite direction position state
            last_close: Current market price
            wallet_balance: Total wallet balance

        Returns:
            Dictionary with 'Buy' and 'Sell' multipliers
        """
        self.reset_amount_multiplier()

        # Calculate position metrics
        if position.entry_price is None or position.entry_price == 0:
            return self.amount_multiplier

        entry_price = float(position.entry_price)
        leverage = position.leverage

        # Calculate unrealized PnL percentage
        if self.direction == 'long':
            self.unrealized_pnl_pct = (1 / entry_price - 1 / last_close) * entry_price * 100 * leverage
        else:  # short
            self.unrealized_pnl_pct = (1 / last_close - 1 / entry_price) * entry_price * 100 * leverage

        # Calculate liquidation ratio
        liq_ratio = self._get_liquidation_ratio(position.liquidation_price, last_close)

        # Calculate position ratio (margin ratio between long/short)
        opposite_margin = float(opposite_position.margin) if opposite_position.margin else 0.0001
        self.position_ratio = float(position.margin) / opposite_margin

        # Calculate total margin
        total_margin = float(position.margin) + float(opposite_position.margin)

        # Check if positions are equal
        is_position_equal = 0.94 < self.position_ratio < 1.05

        # Apply risk management rules
        if self.direction == 'long':
            self._apply_long_position_rules(
                liq_ratio,
                is_position_equal,
                total_margin,
                float(opposite_position.margin)
            )
        else:  # short
            self._apply_short_position_rules(
                liq_ratio,
                is_position_equal,
                total_margin,
                float(opposite_position.margin)
            )

        return self.amount_multiplier

    def _apply_long_position_rules(
        self,
        liq_ratio: float,
        is_position_equal: bool,
        total_margin: float,
        opposite_margin: float
    ) -> None:
        """
        Apply risk management rules for long positions.

        Reference: bbu2-master/position.py:58-74
        """
        # High liquidation risk → decrease long position
        if liq_ratio > 1.05 * self.risk_config.min_liq_ratio:
            self.amount_multiplier[self.SIDE_SELL] = 1.5

        # Moderate liquidation risk → increase opposite (short) position
        elif liq_ratio > self.risk_config.min_liq_ratio:
            self.amount_multiplier[self.SIDE_BUY] = 0.5  # Decrease long buys (increases short)

        # Positions equal but low total margin → adjust
        elif is_position_equal and total_margin < self.risk_config.min_total_margin:
            self._adjust_position_for_low_margin()

        # Long position too small and losing → increase long
        elif self.position_ratio < 0.5 and self.unrealized_pnl_pct < 0:
            self.amount_multiplier[self.SIDE_BUY] = 2.0

        # Long position very small → increase long
        elif self.position_ratio < 0.20:
            self.amount_multiplier[self.SIDE_BUY] = 2.0

    def _apply_short_position_rules(
        self,
        liq_ratio: float,
        is_position_equal: bool,
        total_margin: float,
        opposite_margin: float
    ) -> None:
        """
        Apply risk management rules for short positions.

        Reference: bbu2-master/position.py:76-92
        """
        # High liquidation risk (short) → decrease short position
        if 0.0 < liq_ratio < 0.95 * self.risk_config.max_liq_ratio:
            self.amount_multiplier[self.SIDE_BUY] = 1.5

        # Moderate liquidation risk → increase opposite (long) position
        elif 0.0 < liq_ratio < self.risk_config.max_liq_ratio:
            self.amount_multiplier[self.SIDE_SELL] = 0.5  # Decrease short sells (increases long)

        # Positions equal but low total margin → adjust
        elif is_position_equal and total_margin < self.risk_config.min_total_margin:
            self._adjust_position_for_low_margin()

        # Short position too large and losing → increase short
        elif self.position_ratio > 2.0 and self.unrealized_pnl_pct < 0:
            self.amount_multiplier[self.SIDE_SELL] = 2.0

        # Short position very large → increase short
        elif self.position_ratio > 5.0:
            self.amount_multiplier[self.SIDE_SELL] = 2.0

    def _adjust_position_for_low_margin(self) -> None:
        """
        Adjust position multipliers when total margin is below minimum and positions are equal.

        Reference: bbu2-master/position.py:37-50
        """
        if self.risk_config.increase_same_position_on_low_margin:
            # Increase same position by doubling order size
            if self.direction == 'long':
                self.amount_multiplier[self.SIDE_BUY] = 2.0
            else:  # short
                self.amount_multiplier[self.SIDE_SELL] = 2.0
        else:
            # Increase position by reducing opposite side order size
            if self.direction == 'long':
                self.amount_multiplier[self.SIDE_SELL] = 0.5
            else:  # short
                self.amount_multiplier[self.SIDE_BUY] = 0.5

    def _get_liquidation_ratio(self, liq_price: Decimal, last_close: float) -> float:
        """
        Calculate liquidation ratio.

        Reference: bbu2-master/position.py:121-122

        Args:
            liq_price: Liquidation price
            last_close: Current market price

        Returns:
            Ratio of liquidation price to current price
        """
        if last_close == 0:
            return 0.0
        return float(liq_price) / last_close
