"""Pure PnL calculation functions.

Single source of truth for position PnL formulas used across the project.
All functions are pure (no side effects, no state) and use Decimal for precision.
"""

import bisect
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


# Cache of pre-computed max_value lists, keyed by tier list identity.
# Avoids rebuilding the list on every _find_matching_tier() call.
_tier_max_values_cache: dict[int, list[Decimal]] = {}


def _find_matching_tier(
    position_value: Decimal, tiers: MMTiers
) -> Optional[tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Find the tier whose max_value >= position_value using binary search.

    Tiers must be sorted by ascending max_value (the standard format).
    Uses O(log n) bisect lookup instead of O(n) linear scan.

    Returns:
        The matching tier tuple (max_value, mmr_rate, deduction, imr_rate),
        or None if no tier matches (should not happen when last tier is Infinity).
    """
    tier_id = id(tiers)
    max_values = _tier_max_values_cache.get(tier_id)
    if max_values is None:
        max_values = [t[0] for t in tiers]
        _tier_max_values_cache[tier_id] = max_values
    idx = bisect.bisect_left(max_values, position_value)
    if idx < len(tiers):
        return tiers[idx]
    return None


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
        tier = _find_matching_tier(position_value, tier_table)
        if tier is not None:
            _max_val, _mmr, _ded, imr_rate = tier
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

    The function selects the first tier whose ``max_value`` is >=
    ``position_value`` (binary search on the sorted tier list), then
    applies that tier's ``mmr_rate`` and ``deduction``.

    Args:
        position_value: Absolute position notional value
        symbol: Trading pair (used to select tier table when tiers is None)
        tiers: Optional explicit tier table. When provided, overrides
               symbol-based lookup. Each tier is a 4-tuple:
               ``(max_value, mmr_rate, deduction, imr_rate)``.

    Returns:
        (mm_amount, mmr_rate) where mm_amount is in quote currency

    Tier tables are expected to end with ``Infinity`` so every positive
    position value finds a matching tier.

    Example::

        >>> tiers = [
        ...     (Decimal("200000"),  Decimal("0.005"), Decimal("0"),    Decimal("0.01")),
        ...     (Decimal("1000000"), Decimal("0.01"),  Decimal("1000"), Decimal("0.02")),
        ...     (Decimal("Infinity"), Decimal("0.025"), Decimal("16000"), Decimal("0.05")),
        ... ]
        >>> # position_value=500000 matches tier 2 (500000 <= 1000000)
        >>> calc_maintenance_margin(Decimal("500000"), tiers=tiers)
        (Decimal('4000'), Decimal('0.01'))
        >>> # MM = 500000 * 0.01 - 1000 = 4000
    """
    if position_value <= _ZERO:
        return _ZERO, _ZERO

    tier_table = tiers if tiers is not None else MM_TIERS.get(symbol, MM_TIERS_DEFAULT)
    tier = _find_matching_tier(position_value, tier_table)
    if tier is not None:
        _max_val, mmr_rate, deduction, _imr = tier
    else:
        # Should not reach here (last tier has Infinity), but just in case
        _max_val, mmr_rate, deduction, _imr = tier_table[-1]
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


def _sort_tiers(api_tiers: list[dict]) -> list[dict]:
    """Sort API tier dicts by ascending riskLimitValue.

    Raises:
        ValueError: If any tier is missing riskLimitValue or has an invalid format.
    """
    def sort_key(tier: dict) -> Decimal:
        max_val_str = tier.get("riskLimitValue")
        if max_val_str is None:
            raise ValueError("Missing required field: riskLimitValue")
        try:
            return Decimal(max_val_str)
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"Invalid riskLimitValue format: {max_val_str}") from e

    return sorted(api_tiers, key=sort_key)


def _check_duplicate_boundaries(sorted_tiers: list[dict]) -> None:
    """Validate that sorted tier boundaries are strictly ascending.

    Raises:
        ValueError: If duplicate or near-duplicate boundaries are detected.
    """
    for i in range(1, len(sorted_tiers)):
        prev_val = Decimal(sorted_tiers[i - 1].get("riskLimitValue", "0"))
        curr_val = Decimal(sorted_tiers[i].get("riskLimitValue", "0"))
        if curr_val != Decimal("Infinity") and (
            curr_val < prev_val
            or (curr_val - prev_val).copy_abs() < Decimal("0.01")
        ):
            raise ValueError(
                f"Duplicate tier boundary detected: {prev_val} appears multiple times"
            )


def _validate_max_val(tier: dict) -> Decimal:
    """Validate and extract riskLimitValue from a tier dict.

    Raises:
        ValueError: If missing, malformed, negative, or NaN.
    """
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
    return max_val


def _validate_mmr_rate(tier: dict) -> Decimal:
    """Validate and extract maintenanceMargin (MMR rate) from a tier dict.

    Raises:
        ValueError: If missing, malformed, or outside [0, 1].
    """
    mmr_str = tier.get("maintenanceMargin")
    if mmr_str is None:
        raise ValueError("Missing required field: maintenanceMargin")
    try:
        mmr_rate = Decimal(mmr_str)
    except (ValueError, ArithmeticError) as e:
        raise ValueError(f"Invalid maintenanceMargin format: {mmr_str}") from e
    if not (Decimal("0") <= mmr_rate <= Decimal("1")):
        raise ValueError(f"MMR rate {mmr_rate} outside valid range [0, 1]")
    return mmr_rate


def _validate_deduction(tier: dict) -> Decimal:
    """Validate and extract mmDeduction from a tier dict.

    Bybit can return empty string "" or omit this field for tier 0.
    The ``or "0"`` fallback handles both so Decimal() never receives "".

    Raises:
        ValueError: If malformed or negative.
    """
    deduction_str = tier.get("mmDeduction", "") or "0"
    try:
        deduction = Decimal(deduction_str)
    except (ValueError, ArithmeticError) as e:
        raise ValueError(f"Invalid mmDeduction format: {deduction_str}") from e
    if deduction < 0:
        raise ValueError(f"Negative mmDeduction not allowed: {deduction}")
    return deduction


def _validate_imr_rate(tier: dict) -> Decimal:
    """Validate and extract initialMargin (IMR rate) from a tier dict.

    Bybit can return empty string "" or omit this field for tier 0.
    The ``or "0"`` fallback handles both so Decimal() never receives "".

    Raises:
        ValueError: If malformed or outside [0, 1].
    """
    imr_str = tier.get("initialMargin", "") or "0"
    try:
        imr_rate = Decimal(imr_str)
    except (ValueError, ArithmeticError) as e:
        raise ValueError(f"Invalid initialMargin format: {imr_str}") from e
    if not (Decimal("0") <= imr_rate <= Decimal("1")):
        raise ValueError(f"IMR rate {imr_rate} outside valid range [0, 1]")
    return imr_rate


def _validate_tier_dict(tier: dict) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Validate and extract fields from a single API tier dict.

    Returns:
        (max_val, mmr_rate, deduction, imr_rate) tuple.

    Raises:
        ValueError: If any required field is missing or has an invalid value.
    """
    max_val = _validate_max_val(tier)
    mmr_rate = _validate_mmr_rate(tier)
    deduction = _validate_deduction(tier)
    imr_rate = _validate_imr_rate(tier)

    if mmr_rate == _ZERO:
        logger.debug("Zero MMR rate for tier riskLimitValue=%s", max_val)
    if imr_rate == _ZERO:
        logger.debug("Zero IMR rate for tier riskLimitValue=%s", max_val)

    return max_val, mmr_rate, deduction, imr_rate


def parse_risk_limit_tiers(api_tiers: list[dict]) -> MMTiers:
    """Convert Bybit ``/v5/market/risk-limit`` response to internal tier format.

    Each API tier dict has at minimum:
        - ``riskLimitValue``: max position value for this tier (string)
        - ``maintenanceMargin``: MMR rate as a decimal string (e.g. "0.005")
        - ``mmDeduction``: deduction amount (string, may be "" or missing)

    The returned list is sorted by ascending ``riskLimitValue``, with the
    last tier's cap replaced by ``Infinity``.

    Orchestrates three steps: sort → validate boundaries → validate and
    extract each tier dict.

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

    sorted_tiers = _sort_tiers(api_tiers)
    _check_duplicate_boundaries(sorted_tiers)

    result: MMTiers = [_validate_tier_dict(tier) for tier in sorted_tiers]

    # Replace last tier's cap with Infinity (if not already)
    last_val, last_mmr, last_ded, last_imr = result[-1]
    if last_val != Decimal("Infinity"):
        result[-1] = (Decimal("Infinity"), last_mmr, last_ded, last_imr)

    return result
