"""Pure PnL calculation functions.

Single source of truth for position PnL formulas used across the project.
All functions are pure (no side effects, no state) and use Decimal for precision.
"""

from decimal import Decimal
from typing import Optional

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")

# Type alias for maintenance-margin tier tables
# Each tier: (max_position_value, mmr_rate, deduction)
MMTiers = list[tuple[Decimal, Decimal, Decimal]]

# ---------------------------------------------------------------------------
# Maintenance-margin tier tables (from Bybit risk-limit documentation)
# Each tier: (max_position_value, mmr_rate, deduction)
# MM = position_value * mmr_rate - deduction
# ---------------------------------------------------------------------------

MM_TIERS_BTCUSDT: MMTiers = [
    (Decimal("2000000"),  Decimal("0.005"),  Decimal("0")),
    (Decimal("10000000"), Decimal("0.01"),   Decimal("10000")),
    (Decimal("20000000"), Decimal("0.025"),  Decimal("160000")),
    (Decimal("40000000"), Decimal("0.05"),   Decimal("660000")),
    (Decimal("80000000"), Decimal("0.1"),    Decimal("2660000")),
    (Decimal("160000000"), Decimal("0.125"), Decimal("4660000")),
    (Decimal("Infinity"), Decimal("0.15"),   Decimal("8660000")),
]

MM_TIERS_ETHUSDT: MMTiers = [
    (Decimal("1000000"),  Decimal("0.005"),  Decimal("0")),
    (Decimal("5000000"),  Decimal("0.01"),   Decimal("5000")),
    (Decimal("10000000"), Decimal("0.025"),  Decimal("80000")),
    (Decimal("20000000"), Decimal("0.05"),   Decimal("330000")),
    (Decimal("40000000"), Decimal("0.1"),    Decimal("1330000")),
    (Decimal("80000000"), Decimal("0.125"),  Decimal("2330000")),
    (Decimal("Infinity"), Decimal("0.15"),   Decimal("4330000")),
]

MM_TIERS_DEFAULT: MMTiers = [
    (Decimal("1000000"),  Decimal("0.01"),   Decimal("0")),
    (Decimal("5000000"),  Decimal("0.025"),  Decimal("15000")),
    (Decimal("10000000"), Decimal("0.05"),   Decimal("140000")),
    (Decimal("20000000"), Decimal("0.1"),    Decimal("640000")),
    (Decimal("Infinity"), Decimal("0.15"),   Decimal("1640000")),
]

MM_TIERS: dict[str, MMTiers] = {
    "BTCUSDT": MM_TIERS_BTCUSDT,
    "ETHUSDT": MM_TIERS_ETHUSDT,
}


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
    """Calculate unrealized PnL percentage (ROE) using standard Bybit formula.

    Long:  (close - entry) / entry * leverage * 100
    Short: (entry - close) / entry * leverage * 100

    This is the standard linear-contract ROE formula used by Bybit
    (without fee-to-close component).

    Returns Decimal("0") if entry_price or current_price is zero.
    """
    if entry_price == 0 or current_price == 0:
        return _ZERO

    if direction == "long":
        return (current_price - entry_price) / entry_price * leverage * _HUNDRED
    else:
        return (entry_price - current_price) / entry_price * leverage * _HUNDRED


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


def calc_maintenance_margin(
    position_value: Decimal,
    symbol: str = "BTCUSDT",
    tiers: Optional[MMTiers] = None,
) -> tuple[Decimal, Decimal]:
    """Tier-based maintenance margin.

    Formula: MM = position_value * mmr_rate - deduction

    Args:
        position_value: Absolute position notional value
        symbol: Trading pair (used to select tier table when tiers is None)
        tiers: Optional explicit tier table. When provided, overrides
               symbol-based lookup. Each tier: (max_value, mmr_rate, deduction).

    Returns:
        (mm_amount, mmr_rate) where mm_amount is in quote currency
    """
    if position_value <= _ZERO:
        return _ZERO, _ZERO

    tier_table = tiers if tiers is not None else MM_TIERS.get(symbol, MM_TIERS_DEFAULT)
    for max_val, mmr_rate, deduction in tier_table:
        if position_value <= max_val:
            mm = position_value * mmr_rate - deduction
            return max(mm, _ZERO), mmr_rate
    # Should not reach here (last tier has Infinity), but just in case
    _, mmr_rate, deduction = tier_table[-1]
    mm = position_value * mmr_rate - deduction
    return max(mm, _ZERO), mmr_rate


def calc_imr_pct(total_initial_margin: Decimal, margin_balance: Decimal) -> Decimal:
    """Account IMR% = total_initial_margin / margin_balance * 100.

    Returns Decimal("0") if margin_balance is zero or negative.
    """
    if margin_balance <= _ZERO:
        return _ZERO
    return total_initial_margin / margin_balance * _HUNDRED


def calc_mmr_pct(total_maintenance_margin: Decimal, margin_balance: Decimal) -> Decimal:
    """Account MMR% = total_maintenance_margin / margin_balance * 100.

    Returns Decimal("0") if margin_balance is zero or negative.
    """
    if margin_balance <= _ZERO:
        return _ZERO
    return total_maintenance_margin / margin_balance * _HUNDRED


def parse_risk_limit_tiers(api_tiers: list[dict]) -> MMTiers:
    """Convert Bybit ``/v5/market/risk-limit`` response to internal tier format.

    Each API tier dict has at minimum:
        - ``riskLimitValue``: max position value for this tier (string)
        - ``maintenanceMargin``: MMR rate as a decimal string (e.g. "0.005")
        - ``mmDeduction``: deduction amount (string, may be "" or missing)

    The returned list is sorted by ascending ``riskLimitValue``, with the
    last tier's cap replaced by ``Infinity``.

    Args:
        api_tiers: List of tier dicts from Bybit API response.

    Returns:
        MMTiers suitable for ``calc_maintenance_margin(tiers=...)``.

    Raises:
        ValueError: If api_tiers is empty.
    """
    if not api_tiers:
        raise ValueError("api_tiers must not be empty")

    # Sort by riskLimitValue ascending
    sorted_tiers = sorted(api_tiers, key=lambda t: Decimal(t["riskLimitValue"]))

    result: MMTiers = []
    for tier in sorted_tiers:
        max_val = Decimal(tier["riskLimitValue"])
        mmr_rate = Decimal(tier["maintenanceMargin"])
        # mmDeduction can be empty string "" or missing for tier 0 (no deduction)
        deduction_str = tier.get("mmDeduction", "") or "0"
        deduction = Decimal(deduction_str)
        result.append((max_val, mmr_rate, deduction))

    # Replace last tier's cap with Infinity
    last_val, last_mmr, last_ded = result[-1]
    result[-1] = (Decimal("Infinity"), last_mmr, last_ded)

    return result
