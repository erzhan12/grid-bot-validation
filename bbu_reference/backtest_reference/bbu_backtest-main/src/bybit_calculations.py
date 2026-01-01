"""
Bybit Calculations Engine

Accurate implementation of Bybit USDT perpetual contract calculations following
their official formulas for position management, liquidation, margin, and funding.

This module provides exact calculations matching Bybit's methodology for:
- Liquidation price (isolated & cross margin)
- Maintenance margin with tiered system
- Funding payments (8-hour cycles)
- Order costs and bankruptcy price
- Position value and initial margin
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

from src.constants import (
    DEFAULT_FUNDING_RATE,
    FUNDING_INTERVAL_HOURS,
    MAKER_FEE_RATE,
    MM_TIERS_BY_SYMBOL,
    MM_TIERS_DEFAULT,
    TAKER_FEE_RATE,
)
from src.enums import Direction, MarginMode


class BybitCalculator:
    """Accurate Bybit USDT perpetual calculations following official formulas"""

    def __init__(self):
        """Initialize calculator with Bybit parameters"""
        self.mm_tiers = MM_TIERS_BY_SYMBOL
        self.default_tiers = MM_TIERS_DEFAULT
        self.taker_fee_rate = TAKER_FEE_RATE
        self.maker_fee_rate = MAKER_FEE_RATE
        self.funding_interval_hours = FUNDING_INTERVAL_HOURS

    def calculate_position_value(self, contract_qty: float, mark_price: float) -> float:
        """
        Calculate position value using Bybit formula

        Position Value = Contract Quantity × Mark Price

        Args:
            contract_qty: Position size (quantity of contracts)
            mark_price: Current mark price

        Returns:
            Position value in USDT
        """
        return abs(contract_qty) * mark_price

    def calculate_initial_margin(self, position_value: float, leverage: float) -> float:
        """
        Calculate initial margin using Bybit formula

        Initial Margin = Position Value / Leverage

        Args:
            position_value: Position value in USDT
            leverage: Leverage multiplier (must be > 0)

        Returns:
            Initial margin required in USDT

        Raises:
            ValueError: If leverage is <= 0
        """
        if leverage <= 0:
            raise ValueError(f"Leverage must be positive, got {leverage}")
        return position_value / leverage

    def get_maintenance_margin_tier(self, position_value: float, symbol: str = "BTCUSDT") -> Dict[str, float]:
        """
        Get maintenance margin tier based on position value

        Args:
            position_value: Position value in USDT
            symbol: Trading symbol (e.g., 'BTCUSDT')

        Returns:
            Dictionary with 'mmr' (rate) and 'deduction' values
        """
        # Get tiers for symbol, fallback to default
        tiers = self.mm_tiers.get(symbol, self.default_tiers)

        for tier in tiers:
            if tier["min"] <= position_value < tier["max"]:
                return {
                    "mmr": tier["mmr"],
                    "deduction": tier["deduction"]
                }

        # Fallback to highest tier
        return {
            "mmr": tiers[-1]["mmr"],
            "deduction": tiers[-1]["deduction"]
        }

    def calculate_maintenance_margin(
        self,
        position_value: float,
        symbol: str = "BTCUSDT"
    ) -> Tuple[float, float]:
        """
        Calculate maintenance margin using Bybit's tiered system

        Maintenance Margin = (Position Value × MMR) - MM Deduction

        Args:
            position_value: Position value in USDT
            symbol: Trading symbol

        Returns:
            Tuple of (maintenance_margin, mmr_used)
        """
        tier = self.get_maintenance_margin_tier(position_value, symbol)
        mmr = tier["mmr"]
        deduction = tier["deduction"]

        maintenance_margin = (position_value * mmr) - deduction
        maintenance_margin = max(maintenance_margin, 0)  # Cannot be negative

        return maintenance_margin, mmr

    def calculate_liquidation_price(
        self,
        direction: Direction,
        entry_price: float,
        contract_qty: float,
        leverage: float,
        margin_mode: MarginMode = MarginMode.CROSS,
        available_balance: float = 0,
        symbol: str = "BTCUSDT"
    ) -> float:
        """
        Calculate liquidation price using exact Bybit formula

        Isolated Margin (Bybit Official Formula):
        - Long: Liq Price = Entry Price × (Leverage - 1) / (Leverage - 1 + MMR × Leverage)
        - Short: Liq Price = Entry Price × (Leverage + 1) / (Leverage + 1 - MMR × Leverage)

        Cross Margin:
        - Long: Liq Price = [Contract Qty × Entry Price - Available Balance + MM] / Contract Qty
        - Short: Liq Price = [Contract Qty × Entry Price + Available Balance - MM] / Contract Qty

        Args:
            direction: Position direction (LONG/SHORT)
            entry_price: Average entry price
            contract_qty: Position size (absolute value)
            leverage: Leverage used
            margin_mode: 'isolated' or 'cross'
            available_balance: Available balance for cross margin
            symbol: Trading symbol for MM tier calculation

        Returns:
            Liquidation price
        """
        contract_qty = abs(contract_qty)
        position_value = self.calculate_position_value(contract_qty, entry_price)
        maintenance_margin, mmr = self.calculate_maintenance_margin(position_value, symbol)

        if margin_mode == MarginMode.ISOLATED:
            # Isolated margin formula
            if direction == Direction.LONG:
                # Long isolated: Liq = Entry × (Leverage - 1) / (Leverage - 1 + MMR × Leverage)
                numerator = leverage - 1
                denominator = leverage - 1 + (mmr * leverage)
                if denominator <= 0:
                    return entry_price * 0.1  # Safety bound
                liq_price = entry_price * numerator / denominator
                return max(liq_price, entry_price * 0.1)  # Minimum 10% of entry

            else:  # SHORT
                # Short isolated: Liq = Entry × (Leverage + 1) / (Leverage + 1 - MMR × Leverage)
                numerator = leverage + 1
                denominator = leverage + 1 - (mmr * leverage)
                if denominator <= 0:
                    return entry_price * 10  # Safety bound
                liq_price = entry_price * numerator / denominator
                return min(liq_price, entry_price * 10)  # Maximum 10x entry

        else:  # cross margin
            # Cross margin formula
            if direction == Direction.LONG:
                # Long cross: Liq = (Qty × Entry - Available + MM) / Qty
                numerator = (contract_qty * entry_price) - available_balance + maintenance_margin
                liq_price = numerator / contract_qty
                return max(liq_price, entry_price * 0.1)

            else:  # SHORT
                # Short cross: Liq = (Qty × Entry + Available - MM) / Qty
                numerator = (contract_qty * entry_price) + available_balance - maintenance_margin
                liq_price = numerator / contract_qty
                return min(liq_price, entry_price * 10)

    def calculate_bankruptcy_price(
        self,
        direction: Direction,
        entry_price: float,
        contract_qty: float,
        leverage: float
    ) -> float:
        """
        Calculate bankruptcy price (price at which position equity = 0)

        Bankruptcy Price = Entry Price × (1 ± 1/Leverage)

        Args:
            direction: Position direction
            entry_price: Average entry price
            contract_qty: Position size
            leverage: Leverage used

        Returns:
            Bankruptcy price
        """
        if direction == Direction.LONG:
            # Long bankruptcy: Entry × (1 - 1/Leverage)
            return entry_price * (1 - 1 / leverage)
        else:  # SHORT
            # Short bankruptcy: Entry × (1 + 1/Leverage)
            return entry_price * (1 + 1 / leverage)

    def calculate_unrealized_pnl(
        self,
        direction: Direction,
        contract_qty: float,
        entry_price: float,
        mark_price: float
    ) -> float:
        """
        Calculate unrealized PnL using Bybit formula

        - Long: UPNL = (Mark Price - Entry Price) × Contract Qty
        - Short: UPNL = (Entry Price - Mark Price) × Contract Qty

        Args:
            direction: Position direction
            contract_qty: Position size (signed: + for long, - for short)
            entry_price: Average entry price
            mark_price: Current mark price

        Returns:
            Unrealized PnL in USDT
        """
        if direction == Direction.LONG:
            return (mark_price - entry_price) * abs(contract_qty)
        else:  # SHORT
            return (entry_price - mark_price) * abs(contract_qty)

    def calculate_realized_pnl(
        self,
        direction: Direction,
        contract_qty: float,
        entry_price: float,
        exit_price: float,
        commission_paid: float = 0
    ) -> float:
        """
        Calculate realized PnL including fees

        Realized PnL = (Exit Price - Entry Price) × Contract Qty × Direction - Fees

        Args:
            direction: Position direction
            contract_qty: Size being closed (positive)
            entry_price: Average entry price
            exit_price: Exit price
            commission_paid: Total commission paid on this trade

        Returns:
            Realized PnL in USDT (net of fees)
        """
        contract_qty = abs(contract_qty)

        if direction == Direction.LONG:
            gross_pnl = (exit_price - entry_price) * contract_qty
        else:  # SHORT
            gross_pnl = (entry_price - exit_price) * contract_qty

        return gross_pnl - commission_paid

    def calculate_funding_payment(
        self,
        position_value: float,
        funding_rate: float
    ) -> float:
        """
        Calculate funding payment for position

        Funding Payment = Position Value × Funding Rate
        (Positive = pay, Negative = receive)

        Args:
            position_value: Position value in USDT
            funding_rate: Current funding rate (e.g., 0.0001 = 0.01%)

        Returns:
            Funding payment (positive = pay, negative = receive)
        """
        return position_value * funding_rate

    def calculate_order_cost(
        self,
        order_value: float,
        leverage: float,
        is_maker: bool = False
    ) -> float:
        """
        Calculate total cost to place an order

        Order Cost = Initial Margin + (Fee Rate × Order Value)

        Args:
            order_value: Order value in USDT
            leverage: Leverage used
            is_maker: Whether this is a maker order

        Returns:
            Total cost to place order in USDT
        """
        initial_margin = order_value / leverage
        fee_rate = self.maker_fee_rate if is_maker else self.taker_fee_rate
        fee_cost = order_value * fee_rate

        return initial_margin + fee_cost

    def calculate_margin_ratio(
        self,
        unrealized_pnl: float,
        wallet_balance: float,
        position_value: float,
        symbol: str = "BTCUSDT"
    ) -> float:
        """
        Calculate margin ratio for liquidation monitoring

        Margin Ratio = (Wallet Balance + Unrealized PnL - Maintenance Margin) / Position Value

        Args:
            unrealized_pnl: Current unrealized PnL
            wallet_balance: Available wallet balance
            position_value: Total position value
            symbol: Trading symbol for MM calculation

        Returns:
            Margin ratio (values < 0 indicate liquidation risk)
        """
        maintenance_margin, _ = self.calculate_maintenance_margin(position_value, symbol)
        margin_balance = wallet_balance + unrealized_pnl

        if position_value == 0:
            return float('inf')

        return (margin_balance - maintenance_margin) / position_value

    def is_position_at_risk(
        self,
        unrealized_pnl: float,
        wallet_balance: float,
        position_value: float,
        symbol: str = "BTCUSDT",
        risk_threshold: float = 0.02  # 2% margin ratio threshold
    ) -> bool:
        """
        Check if position is at liquidation risk

        Args:
            unrealized_pnl: Current unrealized PnL
            wallet_balance: Available wallet balance
            position_value: Total position value
            symbol: Trading symbol
            risk_threshold: Margin ratio threshold for risk warning

        Returns:
            True if position is at risk
        """
        margin_ratio = self.calculate_margin_ratio(unrealized_pnl, wallet_balance, position_value, symbol)
        return margin_ratio < risk_threshold

    def get_next_funding_time(self, current_time: datetime = None) -> datetime:
        """
        Calculate next funding time (every 8 hours: 00:00, 08:00, 16:00 UTC)

        Args:
            current_time: Current time (UTC), defaults to now

        Returns:
            Next funding time in UTC
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)

        # Funding times: 00:00, 08:00, 16:00 UTC
        funding_hours = [0, 8, 16]

        current_hour = current_time.hour
        next_funding_hour = None

        for hour in funding_hours:
            if hour > current_hour:
                next_funding_hour = hour
                break

        if next_funding_hour is None:
            # Next funding is tomorrow at 00:00
            next_funding_time = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
            next_funding_time = next_funding_time + timedelta(days=1)
        else:
            next_funding_time = current_time.replace(
                hour=next_funding_hour,
                minute=0,
                second=0,
                microsecond=0
            )

        return next_funding_time

    def calculate_position_summary(
        self,
        direction: Direction,
        contract_qty: float,
        entry_price: float,
        mark_price: float,
        leverage: float,
        wallet_balance: float,
        symbol: str = "BTCUSDT",
        margin_mode: MarginMode = MarginMode.CROSS,
        funding_rate: float = DEFAULT_FUNDING_RATE
    ) -> Dict[str, Any]:
        """
        Calculate comprehensive position summary with all metrics

        Args:
            direction: Position direction
            contract_qty: Position size
            entry_price: Average entry price
            mark_price: Current mark price
            leverage: Leverage used
            wallet_balance: Available wallet balance
            symbol: Trading symbol
            margin_mode: Margin mode ('isolated' or 'cross')
            funding_rate: Current funding rate

        Returns:
            Dictionary with all position metrics
        """
        contract_qty_abs = abs(contract_qty)
        position_value = self.calculate_position_value(contract_qty_abs, mark_price)
        initial_margin = self.calculate_initial_margin(position_value, leverage)
        maintenance_margin, mmr = self.calculate_maintenance_margin(position_value, symbol)

        unrealized_pnl = self.calculate_unrealized_pnl(direction, contract_qty_abs, entry_price, mark_price)
        liquidation_price = self.calculate_liquidation_price(
            direction, entry_price, contract_qty_abs, leverage, margin_mode, wallet_balance, symbol
        )
        bankruptcy_price = self.calculate_bankruptcy_price(direction, entry_price, contract_qty_abs, leverage)

        margin_ratio = self.calculate_margin_ratio(unrealized_pnl, wallet_balance, position_value, symbol)
        at_risk = self.is_position_at_risk(unrealized_pnl, wallet_balance, position_value, symbol)

        funding_payment = self.calculate_funding_payment(position_value, funding_rate)
        next_funding = self.get_next_funding_time()

        return {
            # Basic position info
            'direction': direction.value,
            'size': contract_qty_abs,
            'entry_price': entry_price,
            'mark_price': mark_price,
            'leverage': leverage,
            'margin_mode': margin_mode.value,

            # Financial metrics
            'position_value': position_value,
            'initial_margin': initial_margin,
            'maintenance_margin': maintenance_margin,
            'maintenance_margin_rate': mmr,
            'unrealized_pnl': unrealized_pnl,

            # Risk metrics
            'liquidation_price': liquidation_price,
            'bankruptcy_price': bankruptcy_price,
            'margin_ratio': margin_ratio,
            'at_risk': at_risk,

            # Funding
            'funding_payment': funding_payment,
            'funding_rate': funding_rate,
            'next_funding_time': next_funding.isoformat(),

            # ROE
            'roe_percentage': (unrealized_pnl / initial_margin) * 100 if initial_margin > 0 else 0,

            # Symbol info
            'symbol': symbol
        }