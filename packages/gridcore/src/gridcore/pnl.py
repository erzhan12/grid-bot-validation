"""Pure PnL calculation functions.

Single source of truth for position PnL formulas used across the project.
All functions are pure (no side effects, no state) and use Decimal for precision.

Extracted from:
- bbu_reference/bbu2-master/position.py (unrealized PnL % formula)
- apps/pnl_checker/src/pnl_checker/calculator.py
- apps/backtest/src/backtest/position_tracker.py
"""

import bisect
import functools
import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")

# ---------------------------------------------------------------------------
# Risk-limit tier tables (from Bybit risk-limit documentation)
# Each tier: (max_position_value, mmr_rate, deduction, imr_rate)
# MM = position_value * mmr_rate - deduction
# Last verified against Bybit API: 2026-02-27
# API Reference: https://bybit-exchange.github.io/docs/v5/market/risk-limit
# ---------------------------------------------------------------------------

MMTiers = list[tuple[Decimal, Decimal, Decimal, Decimal]]

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

_MAX_TIER_CACHE_ENTRIES = 64


@functools.lru_cache(maxsize=_MAX_TIER_CACHE_ENTRIES)
def _get_tier_max_values(boundaries: tuple[Decimal, ...]) -> list[Decimal]:
    """Return a list of tier max_values suitable for ``bisect.bisect_left``."""
    return list(boundaries)


def _preseed_tier_cache() -> None:
    """Pre-compute cache entries for hardcoded tier tables at module load time."""
    for _tiers in (MM_TIERS_BTCUSDT, MM_TIERS_ETHUSDT, MM_TIERS_DEFAULT):
        _get_tier_max_values(tuple(t[0] for t in _tiers))


_preseed_tier_cache()


def _find_matching_tier(
    position_value: Decimal, tiers: MMTiers
) -> Optional[tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Find the tier whose max_value >= position_value using binary search.

    Tiers must be sorted by ascending max_value (the standard format).

    Returns:
        The matching tier tuple (max_value, mmr_rate, deduction, imr_rate),
        or None if no tier matches.
    """
    cache_key = tuple(t[0] for t in tiers)
    max_values = _get_tier_max_values(cache_key)
    idx = bisect.bisect_left(max_values, position_value)
    if idx < len(tiers):
        return tiers[idx]
    return None


def calc_maintenance_margin(
    position_value: Decimal,
    symbol: str = "BTCUSDT",
    tiers: Optional[MMTiers] = None,
) -> tuple[Decimal, Decimal]:
    """Tier-based maintenance margin.

    Formula: MM = position_value * mmr_rate - deduction

    Selects the first tier whose ``max_value`` >= ``position_value``,
    then applies that tier's ``mmr_rate`` and ``deduction``.

    Args:
        position_value: Absolute position notional value.
        symbol: Trading pair (used to select tier table when tiers is None).
        tiers: Optional explicit tier table overriding symbol lookup.

    Returns:
        (mm_amount, mmr_rate) where mm_amount is in quote currency.
    """
    if position_value < _ZERO:
        raise ValueError(f"Negative position_value in calc_maintenance_margin: {position_value}")
    if position_value == _ZERO:
        return _ZERO, _ZERO

    tier_table = tiers if tiers is not None else MM_TIERS.get(symbol, MM_TIERS_DEFAULT)
    tier = _find_matching_tier(position_value, tier_table)
    if tier is not None:
        _max_val, mmr_rate, deduction, _imr = tier
    else:
        _max_val, mmr_rate, deduction, _imr = tier_table[-1]
    mm = position_value * mmr_rate - deduction
    return max(mm, _ZERO), mmr_rate


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
        ValueError: If api_tiers is empty or invalid.
    """
    if not isinstance(api_tiers, list):
        raise ValueError("api_tiers must be a list")
    if not api_tiers:
        raise ValueError("api_tiers must not be empty")
    if not all(isinstance(t, dict) for t in api_tiers):
        raise ValueError("api_tiers must contain dict objects")

    sorted_tiers = _TierValidator.sort(api_tiers)
    _TierValidator.check_duplicate_boundaries(sorted_tiers)

    result: MMTiers = [_TierValidator.validate_tier(tier) for tier in sorted_tiers]

    # Replace last tier's cap with Infinity (if not already)
    last_val, last_mmr, last_ded, last_imr = result[-1]
    if last_val != Decimal("Infinity"):
        result[-1] = (Decimal("Infinity"), last_mmr, last_ded, last_imr)

    return result


class _TierValidator:
    """Validation logic for Bybit API risk-limit tier dicts."""

    @staticmethod
    def sort(api_tiers: list[dict]) -> list[dict]:
        """Sort API tier dicts by ascending riskLimitValue."""
        def _parse_limit(tier: dict) -> Decimal:
            max_val_str = tier.get("riskLimitValue")
            if max_val_str is None:
                raise ValueError("Missing required field: riskLimitValue")
            return Decimal(max_val_str)

        values = [_parse_limit(t) for t in api_tiers]
        for i in range(len(values) - 1):
            if values[i] > values[i + 1]:
                return sorted(api_tiers, key=_parse_limit)
        return api_tiers

    @staticmethod
    def check_duplicate_boundaries(sorted_tiers: list[dict]) -> None:
        """Validate that sorted tier boundaries are strictly ascending."""
        for i in range(1, len(sorted_tiers)):
            prev_val = Decimal(sorted_tiers[i - 1].get("riskLimitValue", "0"))
            curr_val = Decimal(sorted_tiers[i].get("riskLimitValue", "0"))
            if curr_val != Decimal("Infinity") and curr_val <= prev_val:
                raise ValueError(
                    f"Duplicate tier boundary detected: {prev_val} appears multiple times"
                )

    @classmethod
    def validate_tier(cls, tier: dict) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Validate and extract fields from a single API tier dict."""
        max_val_str = tier.get("riskLimitValue")
        if max_val_str is None:
            raise ValueError("Missing required field: riskLimitValue")
        max_val = Decimal(max_val_str)

        mmr_str = tier.get("maintenanceMargin")
        if mmr_str is None:
            raise ValueError("Missing required field: maintenanceMargin")
        mmr_rate = Decimal(mmr_str)
        if not (_ZERO <= mmr_rate <= _ONE):
            raise ValueError(f"MMR rate {mmr_rate} outside valid range [0, 1]")

        deduction_str = tier.get("mmDeduction", "") or "0"
        deduction = Decimal(deduction_str)

        imr_str = tier.get("initialMargin", "") or "0"
        imr_rate = Decimal(imr_str)

        return max_val, mmr_rate, deduction, imr_rate


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
