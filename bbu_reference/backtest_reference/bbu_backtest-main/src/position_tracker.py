"""
Position Tracker

Advanced position tracking with average price calculation and PnL management
following Bybit's perpetual futures logic.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.bybit_calculations import BybitCalculator
from src.constants import COMMISSION_RATE, DEFAULT_FUNDING_RATE
from src.enums import Direction, MarginMode


@dataclass
class PositionEntry:
    """Individual position entry for tracking multiple fills"""
    size: float
    price: float
    timestamp: datetime
    order_id: str
    is_increase: bool = True  # True for position increase, False for decrease


@dataclass
class PositionState:
    """Complete position state tracking"""
    total_size: float = 0.0
    average_entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    commission_paid: float = 0.0

    # Enhanced fields for accurate Bybit calculations
    funding_payments: float = 0.0  # Cumulative funding payments
    last_funding_time: Optional[datetime] = None
    liquidation_price: float = 0.0
    bankruptcy_price: float = 0.0
    maintenance_margin: float = 0.0
    margin_ratio: float = 0.0

    entries: List[PositionEntry] = field(default_factory=list)


class PositionTracker:
    """
    Advanced position tracking with average price calculation

    Handles position increases/decreases with proper PnL calculation
    following Bybit's USDT perpetual futures methodology.
    Enhanced with funding payments and accurate Bybit calculations.
    """

    def __init__(
        self,
        direction: Direction,
        commission_rate: float = COMMISSION_RATE,
        symbol: str = "BTCUSDT",
        leverage: float = 10,
        margin_mode: MarginMode = MarginMode.CROSS
    ):
        """
        Initialize position tracker

        Args:
            direction: LONG or SHORT position direction
            commission_rate: Commission rate (default 0.02% = 0.0002 for maker/limit orders)
            symbol: Trading symbol for maintenance margin calculations
            leverage: Leverage used for position
            margin_mode: 'isolated' or 'cross' margin mode
        """
        self.direction = direction
        self.state = PositionState()
        self.commission_rate = commission_rate
        self.symbol = symbol
        self.leverage = leverage
        self.margin_mode = margin_mode

        # Initialize calculator for accurate Bybit calculations
        self.calculator = BybitCalculator()
        
    def add_position(self, size: float, price: float, timestamp: datetime, order_id: str) -> float:
        """
        Add to position using average price calculation
        
        Args:
            size: Size to add to position
            price: Price of the new position entry
            timestamp: When the position was added
            order_id: Order ID that created this position entry
            
        Returns:
            Realized PnL (negative for commission cost)
        """
        if size <= 0:
            raise ValueError("Position size must be positive")
            
        # Calculate new average entry price
        current_total_value = self.state.total_size * self.state.average_entry_price
        new_value = size * price
        new_total_size = self.state.total_size + size
        
        if new_total_size > 0:
            self.state.average_entry_price = (current_total_value + new_value) / new_total_size
        
        self.state.total_size = new_total_size
        
        # Calculate commission
        commission = size * price * self.commission_rate
        self.state.commission_paid += commission

        # Subtract commission cost from realized PnL (matching reduce_position behavior)
        self.state.realized_pnl -= commission

        # Record the entry
        entry = PositionEntry(
            size=size,
            price=price,
            timestamp=timestamp,
            order_id=order_id,
            is_increase=True
        )
        self.state.entries.append(entry)
        
        # Return negative commission as realized PnL (cost)
        return -commission
    
    def reduce_position(self, size: float, price: float, timestamp: datetime, order_id: str) -> float:
        """
        Reduce position and calculate realized PnL
        
        Args:
            size: Size to reduce from position
            price: Price of the position exit
            timestamp: When the position was reduced
            order_id: Order ID that closed this position
            
        Returns:
            Realized PnL from the closed portion
        """
        if size <= 0:
            raise ValueError("Reduction size must be positive")
            
        if size > self.state.total_size:
            raise ValueError(f"Cannot reduce {size}, position size is only {self.state.total_size}")
        
        if self.state.total_size == 0:
            return 0.0
        
        # Calculate realized PnL on the closed portion
        pnl_per_unit = self._calculate_pnl_per_unit(price)
        gross_pnl = pnl_per_unit * size
        
        # Calculate commission on the exit
        commission = size * price * self.commission_rate
        self.state.commission_paid += commission
        
        # Net realized PnL
        net_realized_pnl = gross_pnl - commission
        
        # Update position state
        self.state.total_size -= size
        self.state.realized_pnl += net_realized_pnl
        
        # Average entry price remains the same for remaining position
        # (This follows Bybit's methodology)
        if self.state.total_size == 0:
            self.state.average_entry_price = 0.0
        
        # Record the reduction
        entry = PositionEntry(
            size=-size,  # Negative size for reduction
            price=price,
            timestamp=timestamp,
            order_id=order_id,
            is_increase=False
        )
        self.state.entries.append(entry)
        
        return net_realized_pnl
    
    def calculate_unrealized_pnl(self, current_price: float) -> float:
        """
        Calculate unrealized PnL using Bybit's methodology
        
        For USDT Perpetual:
        Long: PnL = (Mark Price - Avg Entry Price) × Position Size
        Short: PnL = (Avg Entry Price - Mark Price) × Position Size
        
        Args:
            current_price: Current market price
            
        Returns:
            Unrealized PnL
        """
        if self.state.total_size == 0:
            return 0.0
        
        pnl_per_unit = self._calculate_pnl_per_unit(current_price)
        unrealized_pnl = pnl_per_unit * self.state.total_size
        
        # Update state
        self.state.unrealized_pnl = unrealized_pnl
        
        return unrealized_pnl
    
    def _calculate_pnl_per_unit(self, current_price: float) -> float:
        """Calculate PnL per unit based on direction"""
        if self.direction == Direction.LONG:
            # Long position: profit when price goes up
            return current_price - self.state.average_entry_price
        else:
            # Short position: profit when price goes down
            return self.state.average_entry_price - current_price
    
    def calculate_total_pnl(self, current_price: float) -> float:
        """
        Calculate total PnL (realized + unrealized)
        
        Args:
            current_price: Current market price
            
        Returns:
            Total PnL
        """
        unrealized = self.calculate_unrealized_pnl(current_price)
        return self.state.realized_pnl + unrealized
    
    def calculate_roe(self, current_price: float, initial_margin: float = None) -> float:
        """
        Calculate Return on Equity (ROE) percentage

        Args:
            current_price: Current market price
            initial_margin: Initial margin used (auto-calculated if None)

        Returns:
            ROE as percentage
        """
        if initial_margin is None:
            position_value = self.calculator.calculate_position_value(self.state.total_size, current_price)
            initial_margin = self.calculator.calculate_initial_margin(position_value, self.leverage)

        if initial_margin == 0:
            return 0.0

        total_pnl = self.calculate_total_pnl(current_price)
        return (total_pnl / initial_margin) * 100

    def apply_funding_payment(
        self,
        funding_rate: float,
        current_price: float,
        timestamp: datetime
    ) -> float:
        """
        Apply funding payment to position

        Args:
            funding_rate: Current funding rate (e.g., 0.0001 = 0.01%)
            current_price: Current market price for position value calculation
            timestamp: Timestamp of funding application

        Returns:
            Funding payment amount (positive = paid out, negative = received)
        """
        if self.state.total_size == 0:
            return 0.0

        position_value = self.calculator.calculate_position_value(self.state.total_size, current_price)
        funding_payment = self.calculator.calculate_funding_payment(position_value, funding_rate)

        # Apply funding payment (positive = cost, negative = income)
        self.state.funding_payments += funding_payment
        self.state.realized_pnl -= funding_payment  # Subtract cost from realized PnL
        self.state.last_funding_time = timestamp

        return funding_payment

    def calculate_liquidation_price(
        self,
        available_balance: float = 0
    ) -> float:
        """
        Calculate accurate liquidation price using BybitCalculator

        Args:
            available_balance: Available balance for cross margin (0 for isolated)

        Returns:
            Liquidation price
        """
        if self.state.total_size == 0:
            return 0.0

        liquidation_price = self.calculator.calculate_liquidation_price(
            direction=self.direction,
            entry_price=self.state.average_entry_price,
            contract_qty=self.state.total_size,
            leverage=self.leverage,
            margin_mode=self.margin_mode,
            available_balance=available_balance,
            symbol=self.symbol
        )

        # Update state
        self.state.liquidation_price = liquidation_price

        return liquidation_price

    def calculate_bankruptcy_price(self) -> float:
        """
        Calculate bankruptcy price using BybitCalculator

        Returns:
            Bankruptcy price
        """
        if self.state.total_size == 0:
            return 0.0

        bankruptcy_price = self.calculator.calculate_bankruptcy_price(
            direction=self.direction,
            entry_price=self.state.average_entry_price,
            contract_qty=self.state.total_size,
            leverage=self.leverage
        )

        # Update state
        self.state.bankruptcy_price = bankruptcy_price

        return bankruptcy_price

    def calculate_maintenance_margin(self, current_price: float) -> float:
        """
        Calculate maintenance margin using tiered system

        Args:
            current_price: Current market price

        Returns:
            Maintenance margin required
        """
        if self.state.total_size == 0:
            return 0.0

        position_value = self.calculator.calculate_position_value(self.state.total_size, current_price)
        maintenance_margin, _ = self.calculator.calculate_maintenance_margin(position_value, self.symbol)

        # Update state
        self.state.maintenance_margin = maintenance_margin

        return maintenance_margin

    def calculate_margin_ratio(
        self,
        current_price: float,
        wallet_balance: float
    ) -> float:
        """
        Calculate margin ratio for risk monitoring

        Args:
            current_price: Current market price
            wallet_balance: Available wallet balance

        Returns:
            Margin ratio (values < 0 indicate high liquidation risk)
        """
        if self.state.total_size == 0:
            return float('inf')

        position_value = self.calculator.calculate_position_value(self.state.total_size, current_price)
        unrealized_pnl = self.calculate_unrealized_pnl(current_price)

        margin_ratio = self.calculator.calculate_margin_ratio(
            unrealized_pnl=unrealized_pnl,
            wallet_balance=wallet_balance,
            position_value=position_value,
            symbol=self.symbol
        )

        # Update state
        self.state.margin_ratio = margin_ratio

        return margin_ratio

    def is_position_at_risk(
        self,
        current_price: float,
        wallet_balance: float,
        risk_threshold: float = 0.02
    ) -> bool:
        """
        Check if position is at liquidation risk

        Args:
            current_price: Current market price
            wallet_balance: Available wallet balance
            risk_threshold: Margin ratio threshold for risk warning

        Returns:
            True if position is at liquidation risk
        """
        if self.state.total_size == 0:
            return False

        position_value = self.calculator.calculate_position_value(self.state.total_size, current_price)
        unrealized_pnl = self.calculate_unrealized_pnl(current_price)

        return self.calculator.is_position_at_risk(
            unrealized_pnl=unrealized_pnl,
            wallet_balance=wallet_balance,
            position_value=position_value,
            symbol=self.symbol,
            risk_threshold=risk_threshold
        )

    def get_comprehensive_summary(
        self,
        current_price: float,
        wallet_balance: float,
        funding_rate: float = DEFAULT_FUNDING_RATE
    ) -> Dict[str, Any]:
        """
        Get comprehensive position summary using BybitCalculator

        Args:
            current_price: Current market price
            wallet_balance: Available wallet balance
            funding_rate: Current funding rate

        Returns:
            Complete position summary with all Bybit metrics
        """
        if self.state.total_size == 0:
            return {
                'direction': self.direction.value,
                'size': 0,
                'position_empty': True
            }

        return self.calculator.calculate_position_summary(
            direction=self.direction,
            contract_qty=self.state.total_size,
            entry_price=self.state.average_entry_price,
            mark_price=current_price,
            leverage=self.leverage,
            wallet_balance=wallet_balance,
            symbol=self.symbol,
            margin_mode=self.margin_mode,
            funding_rate=funding_rate
        )

    def get_position_info(self, current_price: float) -> dict:
        """
        Get comprehensive position information
        
        Args:
            current_price: Current market price
            
        Returns:
            Dictionary with position details
        """
        unrealized_pnl = self.calculate_unrealized_pnl(current_price)
        
        return {
            'direction': self.direction.value,
            'size': self.state.total_size,
            'average_entry_price': self.state.average_entry_price,
            'current_price': current_price,
            'unrealized_pnl': unrealized_pnl,
            'realized_pnl': self.state.realized_pnl,
            'total_pnl': self.state.realized_pnl + unrealized_pnl,
            'commission_paid': self.state.commission_paid,
            'entry_count': len([e for e in self.state.entries if e.is_increase]),
            'exit_count': len([e for e in self.state.entries if not e.is_increase]),
            'total_entries': len(self.state.entries)
        }
    
    def is_empty(self) -> bool:
        """Check if position is empty"""
        return self.state.total_size == 0
    
    def get_last_entry(self) -> PositionEntry:
        """Get the most recent position entry"""
        if not self.state.entries:
            return None
        return self.state.entries[-1]
    
    def get_entry_history(self, limit: int = None) -> List[PositionEntry]:
        """
        Get position entry history
        
        Args:
            limit: Maximum number of entries to return (None for all)
            
        Returns:
            List of position entries
        """
        if limit is None:
            return self.state.entries.copy()
        return self.state.entries[-limit:].copy()
    
    def reset(self):
        """Reset position tracker to empty state"""
        self.state = PositionState()
    
    def __str__(self) -> str:
        """String representation of position"""
        if self.is_empty():
            return f"PositionTracker({self.direction.value}): Empty"
        
        return (f"PositionTracker({self.direction.value}): "
                f"Size={self.state.total_size:.6f}, "
                f"Avg={self.state.average_entry_price:.2f}, "
                f"Realized PnL={self.state.realized_pnl:.5f}, "
                f"Entries={len(self.state.entries)}")


class PositionManager:
    """
    Manages both long and short position trackers
    """
    
    def __init__(self, commission_rate: float = COMMISSION_RATE):
        """
        Initialize position manager
        
        Args:
            commission_rate: Commission rate for trades
        """
        self.long_tracker = PositionTracker(Direction.LONG, commission_rate)
        self.short_tracker = PositionTracker(Direction.SHORT, commission_rate)
        self.commission_rate = commission_rate
    
    def get_tracker(self, direction: Direction) -> PositionTracker:
        """Get tracker for specific direction"""
        if direction == Direction.LONG:
            return self.long_tracker
        else:
            return self.short_tracker
    
    def get_combined_pnl(self, current_price: float) -> dict:
        """
        Get combined PnL from both positions
        
        Args:
            current_price: Current market price
            
        Returns:
            Dictionary with combined PnL information
        """
        long_info = self.long_tracker.get_position_info(current_price)
        short_info = self.short_tracker.get_position_info(current_price)
        
        return {
            'long_position': long_info,
            'short_position': short_info,
            'total_realized_pnl': long_info['realized_pnl'] + short_info['realized_pnl'],
            'total_unrealized_pnl': long_info['unrealized_pnl'] + short_info['unrealized_pnl'],
            'total_pnl': long_info['total_pnl'] + short_info['total_pnl'],
            'total_commission': long_info['commission_paid'] + short_info['commission_paid'],
            'net_position_size': long_info['size'] - short_info['size']
        }
    
    def reset_all(self):
        """Reset both position trackers"""
        self.long_tracker.reset()
        self.short_tracker.reset()
