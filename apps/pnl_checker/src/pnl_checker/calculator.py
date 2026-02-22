"""PnL and risk calculations using our formulas.

Computes values independently from Bybit so they can be compared.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from gridcore.position import Position, RiskConfig, PositionState

from pnl_checker.fetcher import FetchResult, PositionData

logger = logging.getLogger(__name__)

# Minimum thresholds below which division produces meaningless results
MIN_POSITION_IM = Decimal("1E-8")  # Initial margin floor for PnL % calculation
MIN_LEVERAGE = Decimal("1E-8")  # Leverage floor for margin calculation


@dataclass
class PositionCalcResult:
    """Our calculated values for a single position."""

    symbol: str
    direction: str  # 'long' or 'short'

    # Unrealized PnL (absolute)
    unrealised_pnl_mark: Decimal  # Using mark price
    unrealised_pnl_last: Decimal  # Using last traded price

    # Unrealized PnL % (ROE)
    unrealised_pnl_pct_bbu2_mark: Decimal  # bbu2 formula, mark price
    unrealised_pnl_pct_bbu2_last: Decimal  # bbu2 formula, last price
    unrealised_pnl_pct_bybit: Decimal  # Bybit standard: unrealised / IM * 100

    # Position value and margin
    position_value_mark: Decimal  # size * mark_price
    initial_margin: Decimal  # position_value / leverage

    # Liquidation
    liq_ratio: float  # liq_price / last_price

    # Funding (snapshot)
    funding_snapshot: Decimal  # size * mark_price * funding_rate (current moment)

    # Risk multipliers
    buy_multiplier: float = 1.0
    sell_multiplier: float = 1.0
    risk_rule_triggered: str = "none"


@dataclass
class CalculationResult:
    """All calculated values across positions."""

    positions: list[PositionCalcResult] = field(default_factory=list)


def _calc_unrealised_pnl(
    direction: str, entry_price: Decimal, current_price: Decimal, size: Decimal
) -> Decimal:
    """Calculate unrealized PnL (absolute).

    Long: (current - entry) * size
    Short: (entry - current) * size
    """
    if direction == "long":
        return (current_price - entry_price) * size
    else:
        return (entry_price - current_price) * size


def _calc_unrealised_pnl_pct_bbu2(
    direction: str, entry_price: Decimal, current_price: Decimal, leverage: Decimal
) -> Decimal:
    """Calculate unrealized PnL % using bbu2 formula.

    Long: (1/entry - 1/close) * entry * 100 * leverage
    Short: (1/close - 1/entry) * entry * 100 * leverage

    Returns 0 if entry_price or current_price is zero.

    Reference: backtest/position_tracker.py:calculate_unrealized_pnl_percent()
    """
    if entry_price == 0 or current_price == 0:
        return Decimal("0")

    one = Decimal("1")
    hundred = Decimal("100")

    if direction == "long":
        return (one / entry_price - one / current_price) * entry_price * hundred * leverage
    else:
        return (one / current_price - one / entry_price) * entry_price * hundred * leverage


def _calc_risk_multipliers(
    long_pos: PositionData | None,
    short_pos: PositionData | None,
    last_price: float,
    risk_config: RiskConfig,
) -> dict[str, tuple[float, float, str]]:
    """Calculate position risk multipliers using gridcore.Position.

    Returns:
        Dict keyed by direction ('long'/'short') with tuple of
        (buy_multiplier, sell_multiplier, risk_rule_triggered)
    """
    long_mgr, short_mgr = Position.create_linked_pair(risk_config)

    # Build PositionState for each direction
    long_state = PositionState(
        direction="long",
        size=long_pos.size if long_pos else Decimal("0"),
        entry_price=long_pos.avg_price if long_pos else None,
        margin=long_pos.position_im if long_pos else Decimal("0"),
        liquidation_price=long_pos.liq_price if long_pos else Decimal("0"),
        leverage=int(long_pos.leverage) if long_pos else 1,
        position_value=long_pos.position_value if long_pos else Decimal("0"),
    )

    short_state = PositionState(
        direction="short",
        size=short_pos.size if short_pos else Decimal("0"),
        entry_price=short_pos.avg_price if short_pos else None,
        margin=short_pos.position_im if short_pos else Decimal("0"),
        liquidation_price=short_pos.liq_price if short_pos else Decimal("0"),
        leverage=int(short_pos.leverage) if short_pos else 1,
        position_value=short_pos.position_value if short_pos else Decimal("0"),
    )

    results = {}

    # Reset both before calculating (matches bbu2 pattern)
    long_mgr.reset_amount_multiplier()
    short_mgr.reset_amount_multiplier()

    # Calculate long multipliers
    if long_pos and long_pos.size > 0:
        long_mult = long_mgr.calculate_amount_multiplier(long_state, short_state, last_price)
        rule = _detect_risk_rule(long_mult)
        results["long"] = (long_mult["Buy"], long_mult["Sell"], rule)

    # Calculate short multipliers
    if short_pos and short_pos.size > 0:
        short_mult = short_mgr.calculate_amount_multiplier(short_state, long_state, last_price)
        rule = _detect_risk_rule(short_mult)
        results["short"] = (short_mult["Buy"], short_mult["Sell"], rule)

    return results


class RiskMultiplier(Enum):
    """Known risk multiplier values from gridcore/position.py."""

    NONE = 1.0
    HIGH_LIQ_RISK = 1.5
    MODERATE_RISK = 0.5
    POSITION_RATIO = 2.0


# Map multiplier values to human-readable labels
_RISK_LABELS: dict[float, str] = {
    RiskMultiplier.HIGH_LIQ_RISK.value: "high_liq_risk",
    RiskMultiplier.MODERATE_RISK.value: "moderate_liq_risk or low_margin",
    RiskMultiplier.POSITION_RATIO.value: "position_ratio_adjustment",
}


def _detect_risk_rule(multipliers: dict[str, float]) -> str:
    """Detect which risk rule was triggered based on multiplier values.

    Multiplier values are defined in gridcore/position.py
    (Position.calculate_amount_multiplier). If new values are added
    there, the fallback branch reports them as "adjusted".
    """
    buy = multipliers["Buy"]
    sell = multipliers["Sell"]

    if buy == RiskMultiplier.NONE.value and sell == RiskMultiplier.NONE.value:
        return "none"

    parts = []
    if buy != RiskMultiplier.NONE.value:
        label = _RISK_LABELS.get(buy, "adjusted")
        parts.append(f"{label} (buy {buy}x)")
    if sell != RiskMultiplier.NONE.value:
        label = _RISK_LABELS.get(sell, "adjusted")
        parts.append(f"{label} (sell {sell}x)")

    return ", ".join(parts)


def calculate(fetch_result: FetchResult, risk_config: RiskConfig) -> CalculationResult:
    """Run all PnL and risk calculations on fetched data.

    Args:
        fetch_result: Raw data from Bybit
        risk_config: Risk management parameters

    Returns:
        CalculationResult with our computed values for each position
    """
    result = CalculationResult()

    for symbol_data in fetch_result.symbols:
        ticker = symbol_data.ticker
        positions = symbol_data.positions

        # Separate long and short positions
        long_pos = next((p for p in positions if p.direction == "long"), None)
        short_pos = next((p for p in positions if p.direction == "short"), None)

        # Calculate risk multipliers (needs both positions together)
        risk_multipliers = _calc_risk_multipliers(
            long_pos, short_pos, float(ticker.last_price), risk_config
        )

        # Calculate per-position values
        for pos in positions:
            mark = pos.mark_price
            last = ticker.last_price

            # Unrealized PnL
            unrealised_mark = _calc_unrealised_pnl(pos.direction, pos.avg_price, mark, pos.size)
            unrealised_last = _calc_unrealised_pnl(pos.direction, pos.avg_price, last, pos.size)

            # Unrealized PnL % (bbu2 formula)
            pct_bbu2_mark = _calc_unrealised_pnl_pct_bbu2(pos.direction, pos.avg_price, mark, pos.leverage)
            pct_bbu2_last = _calc_unrealised_pnl_pct_bbu2(pos.direction, pos.avg_price, last, pos.leverage)

            # Unrealized PnL % (Bybit standard)
            pct_bybit = Decimal("0")
            if pos.position_im >= MIN_POSITION_IM:
                pct_bybit = unrealised_mark / pos.position_im * Decimal("100")
            else:
                logger.warning(f"{pos.symbol} {pos.direction}: position_im={pos.position_im} too small for PnL % calc")

            # Position value and margin
            position_value_mark = pos.size * mark
            initial_margin = Decimal("0")
            if pos.leverage >= MIN_LEVERAGE:
                initial_margin = position_value_mark / pos.leverage
            else:
                logger.warning(f"{pos.symbol} {pos.direction}: leverage={pos.leverage} too small for margin calc")

            # Liquidation ratio
            liq_ratio = 0.0
            if last > 0:
                liq_ratio = float(pos.liq_price) / float(last)

            # Funding snapshot
            funding_snapshot = pos.size * mark * ticker.funding_rate

            # Risk multipliers
            buy_mult, sell_mult, risk_rule = 1.0, 1.0, "none"
            if pos.direction in risk_multipliers:
                buy_mult, sell_mult, risk_rule = risk_multipliers[pos.direction]

            result.positions.append(PositionCalcResult(
                symbol=pos.symbol,
                direction=pos.direction,
                unrealised_pnl_mark=unrealised_mark,
                unrealised_pnl_last=unrealised_last,
                unrealised_pnl_pct_bbu2_mark=pct_bbu2_mark,
                unrealised_pnl_pct_bbu2_last=pct_bbu2_last,
                unrealised_pnl_pct_bybit=pct_bybit,
                position_value_mark=position_value_mark,
                initial_margin=initial_margin,
                liq_ratio=liq_ratio,
                funding_snapshot=funding_snapshot,
                buy_multiplier=buy_mult,
                sell_multiplier=sell_mult,
                risk_rule_triggered=risk_rule,
            ))

    return result
