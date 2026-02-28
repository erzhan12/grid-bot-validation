"""Pure PnL calculation functions.

Single source of truth for position PnL formulas used across the project.
All functions are pure (no side effects, no state) and use Decimal for precision.
"""

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")

# Type alias for risk-limit tier tables
# Each tier: (max_position_value, mmr_rate, deduction, imr_rate)
MMTiers = list[tuple[Decimal, Decimal, Decimal, Decimal]]

# ---------------------------------------------------------------------------
# Risk-limit tier tables (from Bybit risk-limit documentation)
# Each tier: (max_position_value, mmr_rate, deduction, imr_rate)
# MM = position_value * mmr_rate - deduction
# IM = position_value * imr_rate
# Last verified against Bybit API: 2026-02-27
# Important: These hardcoded values should be periodically updated by running
# the RiskLimitProvider with force_fetch=True and comparing against the latest API data.
# To verify/update: use RiskLimitProvider(rest_client=client).get(symbol, force_fetch=True)
# and compare the returned tiers against the tables below.
#
# These hardcoded tiers are a safe fallback when API data is unavailable.
# Bybit applies progressively higher IM/MM rates to larger position notional
# values, so each table is ordered by increasing notional cap. The first tier
# whose ``max_position_value`` is >= the current position value is selected.
# The final tier always uses ``Infinity`` to guarantee a match for any size.
# API Reference: https://bybit-exchange.github.io/docs/v5/market/risk-limit
# ---------------------------------------------------------------------------

MM_TIERS_BTCUSDT: MMTiers = [
    (Decimal("2000000"),  Decimal("0.005"),  Decimal("0"),       Decimal("0.01")),
    (Decimal("10000000"), Decimal("0.01"),   Decimal("10000"),   Decimal("0.02")),
    (Decimal("20000000"), Decimal("0.025"),  Decimal("160000"),  Decimal("0.05")),
    (Decimal("40000000"), Decimal("0.05"),   Decimal("660000"),  Decimal("0.1")),
    (Decimal("80000000"), Decimal("0.1"),    Decimal("2660000"), Decimal("0.2")),
    (Decimal("160000000"), Decimal("0.125"), Decimal("4660000"), Decimal("0.25")),
    (Decimal("Infinity"), Decimal("0.15"),   Decimal("8660000"), Decimal("0.3")),
]

MM_TIERS_ETHUSDT: MMTiers = [
    (Decimal("1000000"),  Decimal("0.005"),  Decimal("0"),       Decimal("0.01")),
    (Decimal("5000000"),  Decimal("0.01"),   Decimal("5000"),    Decimal("0.02")),
    (Decimal("10000000"), Decimal("0.025"),  Decimal("80000"),   Decimal("0.05")),
    (Decimal("20000000"), Decimal("0.05"),   Decimal("330000"),  Decimal("0.1")),
    (Decimal("40000000"), Decimal("0.1"),    Decimal("1330000"), Decimal("0.2")),
    (Decimal("80000000"), Decimal("0.125"),  Decimal("2330000"), Decimal("0.25")),
    (Decimal("Infinity"), Decimal("0.15"),   Decimal("4330000"), Decimal("0.3")),
]

MM_TIERS_DEFAULT: MMTiers = [
    (Decimal("1000000"),  Decimal("0.01"),   Decimal("0"),       Decimal("0.02")),
    (Decimal("5000000"),  Decimal("0.025"),  Decimal("15000"),   Decimal("0.05")),
    (Decimal("10000000"), Decimal("0.05"),   Decimal("140000"),  Decimal("0.1")),
    (Decimal("20000000"), Decimal("0.1"),    Decimal("640000"),  Decimal("0.2")),
    (Decimal("Infinity"), Decimal("0.15"),   Decimal("1640000"), Decimal("0.3")),
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
    if entry_price <= 0 or current_price <= 0:
        if entry_price < 0:
            logger.warning(f"Negative entry_price for {direction}: entry={entry_price}")
        if current_price < 0:
            logger.warning(f"Negative current_price for {direction}: current={current_price}")
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


def calc_initial_margin(
    position_value: Decimal,
    leverage: Decimal,
    symbol: str = "",
    tiers: Optional[MMTiers] = None,
) -> tuple[Decimal, Decimal]:
    """Calculate initial margin using tier-based IMR rate.

    When *tiers* are provided (or looked up by symbol), the IM is calculated
    as ``position_value * imr_rate`` using the tier matching the position value.
    Falls back to ``position_value / leverage`` when no tier matches or no
    tiers are available. Tier tables are expected to end with ``Infinity`` so
    every positive position value finds a matching tier.

    Args:
        position_value: Position notional value
        leverage: Position leverage (used as fallback)
        symbol: Trading pair (used to select tier table when tiers is None)
        tiers: Optional explicit tier table with 4-tuple entries.

    Returns:
        (im_amount, imr_rate) — initial margin in quote currency and the
        IMR rate used.
    """
    if position_value < _ZERO:
        logger.warning("Negative position_value in calc_initial_margin: %s", position_value)
        return _ZERO, _ZERO
    if position_value == _ZERO:
        return _ZERO, _ZERO

    tier_table = tiers if tiers is not None else MM_TIERS.get(symbol, MM_TIERS_DEFAULT) if symbol else MM_TIERS_DEFAULT
    if tier_table is not None:
        for max_val, _mmr, _ded, imr_rate in tier_table:
            if position_value <= max_val:
                return position_value * imr_rate, imr_rate

    # Fallback: position_value / leverage
    if leverage <= 0:
        return _ZERO, _ZERO
    imr_rate = _ONE / leverage
    return position_value / leverage, imr_rate


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

    Tier tables are expected to end with ``Infinity`` so every positive
    position value finds a matching tier.
    """
    if position_value <= _ZERO:
        return _ZERO, _ZERO

    tier_table = tiers if tiers is not None else MM_TIERS.get(symbol, MM_TIERS_DEFAULT)
    for max_val, mmr_rate, deduction, _imr in tier_table:
        if position_value <= max_val:
            mm = position_value * mmr_rate - deduction
            return max(mm, _ZERO), mmr_rate
    # Should not reach here (last tier has Infinity), but just in case
    _, mmr_rate, deduction, _imr = tier_table[-1]
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

    Example::

        >>> api_tiers = [
        ...     {"riskLimitValue": "200000", "maintenanceMargin": "0.005",
        ...      "mmDeduction": "0", "initialMargin": "0.01"},
        ...     {"riskLimitValue": "1000000", "maintenanceMargin": "0.01",
        ...      "mmDeduction": "1000", "initialMargin": "0.02"},
        ... ]
        >>> parse_risk_limit_tiers(api_tiers)
        [
            (Decimal('200000'), Decimal('0.005'), Decimal('0'), Decimal('0.01')),
            (Decimal('Infinity'), Decimal('0.01'), Decimal('1000'), Decimal('0.02')),
        ]
        # Each tuple: (max_position_value, mmr_rate, deduction, imr_rate)

    Args:
        api_tiers: List of tier dicts from Bybit API response.

    Returns:
        MMTiers suitable for ``calc_maintenance_margin(tiers=...)``.

    Raises:
        ValueError: If api_tiers is empty.

    Note:
        Validates that MMR and IMR rates are in [0, 1] range and that
        riskLimitValue is a valid positive number or "Infinity".
    """
    if not isinstance(api_tiers, list):
        raise ValueError("api_tiers must be a list")
    if not api_tiers:
        raise ValueError("api_tiers must not be empty")

    def _risk_limit_sort_key(tier: dict) -> Decimal:
        max_val_str = tier.get("riskLimitValue")
        if max_val_str is None:
            raise ValueError("Missing required field: riskLimitValue")
        try:
            return Decimal(max_val_str)
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"Invalid riskLimitValue format: {max_val_str}") from e

    # Sort by riskLimitValue ascending
    sorted_tiers = sorted(api_tiers, key=_risk_limit_sort_key)

    # Validate strictly ascending tier boundaries (detect duplicates/out-of-order)
    for i in range(1, len(sorted_tiers)):
        prev_val = Decimal(sorted_tiers[i - 1].get("riskLimitValue", "0"))
        curr_val = Decimal(sorted_tiers[i].get("riskLimitValue", "0"))
        if curr_val != Decimal("Infinity") and (curr_val < prev_val or (curr_val - prev_val).copy_abs() < Decimal("0.01")):
            raise ValueError(
                f"Duplicate tier boundary detected: {prev_val} appears multiple times"
            )

    result: MMTiers = []
    for tier in sorted_tiers:
        max_val_str = tier.get("riskLimitValue")
        if max_val_str is None:
            raise ValueError("Missing required field: riskLimitValue")
        try:
            max_val = Decimal(max_val_str)
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"Invalid riskLimitValue format: {max_val_str}") from e
        if max_val_str != "Infinity":
            if max_val.is_nan() or max_val <= 0:
                raise ValueError(f"Invalid riskLimitValue: {max_val}")

        mmr_str = tier.get("maintenanceMargin")
        if mmr_str is None:
            raise ValueError("Missing required field: maintenanceMargin")
        try:
            mmr_rate = Decimal(mmr_str)
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"Invalid maintenanceMargin format: {mmr_str}") from e
        # Bybit can return empty string "" or omit these fields for tier 0.
        # The ``or "0"`` fallback handles both so Decimal() never receives "".
        deduction_str = tier.get("mmDeduction", "") or "0"
        try:
            deduction = Decimal(deduction_str)
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"Invalid mmDeduction format: {deduction_str}") from e
        if deduction < 0:
            raise ValueError(f"Negative mmDeduction not allowed: {deduction}")
        imr_str = tier.get("initialMargin", "") or "0"
        try:
            imr_rate = Decimal(imr_str)
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"Invalid initialMargin format: {imr_str}") from e
        if not (Decimal("0") <= mmr_rate <= Decimal("1")):
            raise ValueError(f"MMR rate {mmr_rate} outside valid range [0, 1]")
        if mmr_rate == Decimal("0"):
            logger.debug("Zero MMR rate for tier riskLimitValue=%s", max_val)
        if not (Decimal("0") <= imr_rate <= Decimal("1")):
            raise ValueError(f"IMR rate {imr_rate} outside valid range [0, 1]")
        if imr_rate == Decimal("0"):
            logger.debug("Zero IMR rate for tier riskLimitValue=%s", max_val)
        result.append((max_val, mmr_rate, deduction, imr_rate))

    # Replace last tier's cap with Infinity (if not already)
    last_val, last_mmr, last_ded, last_imr = result[-1]
    if last_val != Decimal("Infinity"):
        result[-1] = (Decimal("Infinity"), last_mmr, last_ded, last_imr)

    return result
