"""
Position state tracking and risk management.

Extracted from bbu2-master/position.py with exchange-specific dependencies removed.

ARCHITECTURE: This module implements the two-position architecture from Bybit.
Each trading pair has TWO separate Position objects (long and short) that can
reference and modify each other's multipliers via set_opposite().
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Optional

logger = logging.getLogger(__name__)


class DirectionType(StrEnum):
    """Position direction type constants."""
    LONG = 'long'
    SHORT = 'short'


class SideType(StrEnum):
    """Order side type constants."""
    BUY = 'Buy'
    SELL = 'Sell'


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


class Position:
    """
    Position risk management and amount multiplier calculation.

    Extracted from bbu2-master/position.py Position class.

    CRITICAL: This class represents ONE position direction (long OR short).
    For proper risk management, create TWO Position objects and link them
    with set_opposite() so they can modify each other's multipliers.

    Reference: bbu2-master/position.py:4-159
    """

    # Order side constants (aliases for SideType enum values)
    SIDE_BUY = SideType.BUY
    SIDE_SELL = SideType.SELL

    # Direction constants (aliases for DirectionType enum values)
    DIRECTION_LONG = DirectionType.LONG
    DIRECTION_SHORT = DirectionType.SHORT

    def __init__(self, direction: str, risk_config: RiskConfig):
        """
        Initialize position manager for one direction.

        Args:
            direction: 'long' or 'short'
            risk_config: Risk management parameters

        Reference: bbu2-master/position.py:8-22
        """
        self.direction = direction
        self.risk_config = risk_config
        self.amount_multiplier = {self.SIDE_BUY: 1.0, self.SIDE_SELL: 1.0}
        self.position_ratio = 1.0
        self._opposite: Optional['Position'] = None

    def set_opposite(self, opposite: 'Position') -> None:
        """
        Link this position to its opposite direction position.

        This allows cross-position multiplier adjustments during risk management.

        Args:
            opposite: The opposite direction Position object

        Reference: bbu2-master/position.py:110-111
        """
        self._opposite = opposite

    def set_amount_multiplier(self, side: str, mult: float) -> None:
        """
        Set order size multiplier for a specific side.

        Args:
            side: 'Buy' or 'Sell'
            mult: Multiplier value

        Reference: bbu2-master/position.py:95-96
        """
        self.amount_multiplier[side] = mult

    def get_amount_multiplier(self) -> dict[str, float]:
        """
        Get current amount multipliers.

        Returns:
            Dictionary with 'Buy' and 'Sell' multipliers

        Reference: bbu2-master/position.py:98-99
        """
        return self.amount_multiplier

    def reset_amount_multiplier(self) -> None:
        """
        Reset all multipliers to 1.0.

        Reference: bbu2-master/position.py:33-35
        """
        self.set_amount_multiplier(self.SIDE_BUY, 1.0)
        self.set_amount_multiplier(self.SIDE_SELL, 1.0)

    def calculate_amount_multiplier(
        self,
        position: PositionState,
        opposite_position: PositionState,
        last_close: float
    ) -> dict[str, float]:
        """
        Calculate order size multipliers based on position state.

        IMPORTANT: Caller must reset multipliers on BOTH positions before calling
        this method for each direction. This matches bbu2 pattern where reset
        happens once before both long and short calculations, so cross-position
        effects from the first call are preserved during the second call.

        Example:
            long_mgr.reset_amount_multiplier()
            short_mgr.reset_amount_multiplier()
            long_mult = long_mgr.calculate_amount_multiplier(long_state, short_state, price)
            short_mult = short_mgr.calculate_amount_multiplier(short_state, long_state, price)

        IMPORTANT: The opposite position must be linked via set_opposite() before
        calling this method. Without linking, moderate liquidation risk adjustments
        (which modify the opposite position's multipliers) will fail.

        Reference: bbu2-master/position.py:52-92, 101-108

        Args:
            position: Current position state
            opposite_position: Opposite direction position state
            last_close: Current market price

        Returns:
            Dictionary with 'Buy' and 'Sell' multipliers for this position

        Raises:
            ValueError: If opposite position is not linked via set_opposite()
        """
        # Validate that opposite position is linked
        if self._opposite is None:
            raise ValueError(
                f"Position {self.direction} requires opposite position to be linked. "
                f"Call set_opposite() before calculate_amount_multiplier() or use "
                f"Position.create_linked_pair() to create properly linked positions."
            )

        # Calculate position metrics
        if position.entry_price is None or position.entry_price == 0:
            return self.amount_multiplier

        entry_price = float(position.entry_price)
        leverage = position.leverage

        # Calculate unrealized PnL percentage
        if self.direction == self.DIRECTION_LONG:
            unrealized_pnl_pct = (1 / entry_price - 1 / last_close) * entry_price * 100 * leverage
        else:  # short
            unrealized_pnl_pct = (1 / last_close - 1 / entry_price) * entry_price * 100 * leverage

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
        if self.direction == self.DIRECTION_LONG:
            self._apply_long_position_rules(
                liq_ratio,
                is_position_equal,
                total_margin,
                unrealized_pnl_pct
            )
        else:  # short
            self._apply_short_position_rules(
                liq_ratio,
                is_position_equal,
                total_margin,
                unrealized_pnl_pct
            )

        # Log position state (matches reference position.py:24-31)
        logger.debug(
            '%s margin=%.2f liq_ratio=%.2f unrealized_pnl=%.2f%% '
            'multiplier=%s position_ratio=%.2f total_margin=%.2f',
            self.direction,
            float(position.margin),
            liq_ratio,
            unrealized_pnl_pct,
            self.amount_multiplier,
            self.position_ratio,
            total_margin
        )

        return self.amount_multiplier

    def _apply_long_position_rules(
        self,
        liq_ratio: float,
        is_position_equal: bool,
        total_margin: float,
        unrealized_pnl_pct: float
    ) -> None:
        """
        Apply risk management rules for long positions.

        Reference: bbu2-master/position.py:58-74

        Priority order (SAFER - liquidation-first):
        1. High liquidation risk (emergency) - prevents total loss
        2. Moderate liquidation risk (safety) - hedging via opposite position
        3. Specific position sizing conditions - strategic adjustments

        Capital preservation > strategy optimization.
        """
        # High liquidation risk → decrease long position (HIGHEST PRIORITY)
        if liq_ratio > 1.05 * self.risk_config.min_liq_ratio:
            logger.info('Position adjustment: %s high_liq_risk (ratio=%.2f)', self.direction, liq_ratio)
            self.set_amount_multiplier(self.SIDE_SELL, 1.5)

        # Moderate liquidation risk → increase opposite (short) position as hedge
        elif liq_ratio > self.risk_config.min_liq_ratio:
            logger.info('Position adjustment: %s moderate_liq_risk (ratio=%.2f)', self.direction, liq_ratio)
            if self._opposite:
                # Reduce short's buy orders → allows short to grow as hedge
                self._opposite.set_amount_multiplier(self.SIDE_BUY, 0.5)

        # Positions equal but low total margin → adjust
        elif is_position_equal and total_margin < self.risk_config.min_total_margin:
            logger.info('Position adjustment: %s low_margin (total=%.2f)', self.direction, total_margin)
            self._adjust_position_for_low_margin()

        # Long position too small and losing → increase long
        elif self.position_ratio < 0.5 and unrealized_pnl_pct < 0:
            logger.info('Position adjustment: %s ratio=%.2f increasing buys', self.direction, self.position_ratio)
            self.set_amount_multiplier(self.SIDE_BUY, 2.0)

        # Long position very small → increase long
        elif self.position_ratio < 0.20:
            logger.info('Position adjustment: %s ratio=%.2f increasing buys', self.direction, self.position_ratio)
            self.set_amount_multiplier(self.SIDE_BUY, 2.0)

    def _apply_short_position_rules(
        self,
        liq_ratio: float,
        is_position_equal: bool,
        total_margin: float,
        unrealized_pnl_pct: float
    ) -> None:
        """
        Apply risk management rules for short positions.

        Reference: bbu2-master/position.py:76-92

        Priority order (SAFER - liquidation-first):
        1. High liquidation risk (emergency) - prevents total loss
        2. Specific position sizing conditions - strategic adjustments
        3. Moderate liquidation risk (safety) - hedging via opposite position

        Note: High liquidation checked first, moderate checked last.
        Capital preservation > strategy optimization.
        """
        # High liquidation risk (short) → decrease short position (EMERGENCY)
        # For shorts: liq_ratio > max means liquidation is imminent (corrected from original bug)
        if liq_ratio > 0.95 * self.risk_config.max_liq_ratio:
            logger.info('Position adjustment: %s EMERGENCY high_liq_risk (ratio=%.2f)', self.direction, liq_ratio)
            self.set_amount_multiplier(self.SIDE_BUY, 1.5)

        # Positions equal but low total margin → adjust
        elif is_position_equal and total_margin < self.risk_config.min_total_margin:
            logger.info('Position adjustment: %s low_margin (total=%.2f)', self.direction, total_margin)
            self._adjust_position_for_low_margin()

        # Short position too large and losing → increase short
        elif self.position_ratio > 2.0 and unrealized_pnl_pct < 0:
            logger.info('Position adjustment: %s ratio=%.2f increasing sells', self.direction, self.position_ratio)
            self.set_amount_multiplier(self.SIDE_SELL, 2.0)

        # Short position very large → increase short
        elif self.position_ratio > 5.0:
            logger.info('Position adjustment: %s ratio=%.2f increasing sells', self.direction, self.position_ratio)
            self.set_amount_multiplier(self.SIDE_SELL, 2.0)

        # Moderate liquidation risk → increase opposite (long) position as hedge
        # Reference: bbu2-master/position.py:81-86
        # Checked AFTER position ratio checks (per original sequence)
        elif 0.0 < liq_ratio < self.risk_config.max_liq_ratio:
            logger.info('Position adjustment: %s moderate_liq_risk (ratio=%.2f)', self.direction, liq_ratio)
            if self._opposite:
                # Reduce long's sell orders → allows long to grow as hedge
                self._opposite.set_amount_multiplier(self.SIDE_SELL, 0.5)

    def _adjust_position_for_low_margin(self) -> None:
        """
        Adjust position multipliers when total margin is below minimum and positions are equal.

        Reference: bbu2-master/position.py:37-50
        """
        if self.risk_config.increase_same_position_on_low_margin:
            # Increase same position by doubling order size
            if self.direction == self.DIRECTION_LONG:
                self.set_amount_multiplier(self.SIDE_BUY, 2.0)
            else:  # short
                self.set_amount_multiplier(self.SIDE_SELL, 2.0)
        else:
            # Increase position by reducing opposite side order size
            if self.direction == self.DIRECTION_LONG:
                self.set_amount_multiplier(self.SIDE_SELL, 0.5)
            else:  # short
                self.set_amount_multiplier(self.SIDE_BUY, 0.5)

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

    @staticmethod
    def create_linked_pair(
        long_config: RiskConfig,
        short_config: Optional[RiskConfig] = None
    ) -> tuple['Position', 'Position']:
        """
        Create and link long/short position managers.

        This is the recommended way to create Position objects as it ensures
        they are properly linked for cross-position risk adjustments.

        Args:
            long_config: Risk configuration for long position
            short_config: Risk configuration for short position (defaults to long_config if not provided)

        Returns:
            Tuple of (long_position, short_position) with opposite references set

        Example:
            >>> risk_config = RiskConfig(
            ...     min_liq_ratio=0.8,
            ...     max_liq_ratio=1.2,
            ...     max_margin=5.0,
            ...     min_total_margin=1.0
            ... )
            >>> long_mgr, short_mgr = Position.create_linked_pair(risk_config)
            >>> # Now both positions can modify each other during risk management

            >>> # Or with separate configs:
            >>> long_mgr, short_mgr = Position.create_linked_pair(long_config, short_config)
        """
        if short_config is None:
            short_config = long_config

        long_mgr = Position(Position.DIRECTION_LONG, long_config)
        short_mgr = Position(Position.DIRECTION_SHORT, short_config)

        long_mgr.set_opposite(short_mgr)
        short_mgr.set_opposite(long_mgr)

        return long_mgr, short_mgr


# Backward compatibility alias
PositionRiskManager = Position
