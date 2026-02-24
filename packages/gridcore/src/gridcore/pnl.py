"""Pure PnL calculation functions.

Single source of truth for position PnL formulas used across the project.
All functions are pure (no side effects, no state) and use Decimal for precision.

Extracted from:
- bbu_reference/bbu2-master/position.py (unrealized PnL % formula)
- apps/pnl_checker/src/pnl_checker/calculator.py
- apps/backtest/src/backtest/position_tracker.py
"""

from decimal import Decimal

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")


def calc_unrealised_pnl(
    direction: str, entry_price: Decimal, current_price: Decimal, size: Decimal
) -> Decimal:
    """Calculate unrealized PnL (absolute).

    Long:  (current_price - entry_price) * size
    Short: (entry_price - current_price) * size

    Args:
        direction: 'long' or 'short'
        entry_price: Average entry price
        current_price: Current market price (mark or last)
        size: Position size (always positive)

    Returns:
        Unrealized PnL in quote currency (positive = profit)
    """
    if direction == "long":
        return (current_price - entry_price) * size
    else:
        return (entry_price - current_price) * size


def calc_unrealised_pnl_pct(
    direction: str, entry_price: Decimal, current_price: Decimal, leverage: Decimal
) -> Decimal:
    """Calculate unrealized PnL percentage (ROE) using the bbu2 formula.

    Long:  (1/entry - 1/close) * entry * 100 * leverage
    Short: (1/close - 1/entry) * entry * 100 * leverage

    This is algebraically close to the standard ROE formula
    ``(close - entry) / entry * 100 * leverage`` but uses reciprocal
    prices.  The bbu2 codebase chose this form because it generalises
    to inverse contracts.  For linear USDT contracts the difference is
    negligible (< 0.4% relative).

    Returns Decimal("0") if entry_price or current_price is zero.

    Reference: bbu2-master/position.py (unrealized PnL % in multiplier calc)
    """
    if entry_price == 0 or current_price == 0:
        return _ZERO

    if direction == "long":
        return (_ONE / entry_price - _ONE / current_price) * entry_price * _HUNDRED * leverage
    else:
        return (_ONE / current_price - _ONE / entry_price) * entry_price * _HUNDRED * leverage


def calc_position_value(size: Decimal, entry_price: Decimal) -> Decimal:
    """Calculate position value (notional at entry).

    Matches Bybit's ``positionValue`` field: size * avgEntryPrice.

    Args:
        size: Position size
        entry_price: Average entry price

    Returns:
        Position value in quote currency
    """
    return size * entry_price


def calc_initial_margin(position_value: Decimal, leverage: Decimal) -> Decimal:
    """Calculate initial margin.

    Formula: position_value / leverage

    Returns Decimal("0") if leverage is zero.

    Args:
        position_value: Position notional value
        leverage: Position leverage

    Returns:
        Initial margin in quote currency
    """
    if leverage == 0:
        return _ZERO
    return position_value / leverage


def calc_liq_ratio(liq_price: Decimal, current_price: Decimal) -> float:
    """Calculate liquidation ratio.

    Formula: liq_price / current_price

    Returns 0.0 if current_price is zero.

    Args:
        liq_price: Liquidation price
        current_price: Current market price

    Returns:
        Liquidation ratio as float
    """
    if current_price == 0:
        return 0.0
    return float(liq_price) / float(current_price)
