"""PnL and risk calculations using our formulas.

Computes values independently from Bybit so they can be compared.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from gridcore.position import Position, RiskConfig, PositionState
from gridcore.pnl import (
    calc_unrealised_pnl,
    calc_unrealised_pnl_pct,
    calc_position_value,
    calc_initial_margin,
    calc_liq_ratio,
    calc_maintenance_margin,
    calc_imr_pct,
    calc_mmr_pct,
    MM_TIERS,
    MM_TIERS_DEFAULT,
)

from pnl_checker.fetcher import FetchResult, PositionData

logger = logging.getLogger(__name__)

# Minimum thresholds based on Bybit's decimal precision (8 decimals).
# PnL% calculation divides unrealised_pnl by position_im; values below 1E-8
# cause Decimal overflow due to the extreme precision required (28+ digits).
# Similarly, calc_initial_margin divides position_value by leverage; near-zero
# leverage produces astronomically large IM values that overflow.
# The calculator logs a warning and returns Decimal("0") instead.
MIN_POSITION_IM = Decimal("1E-8")  # ~$0.00000001 USDT
MIN_LEVERAGE = Decimal("1E-8")  # Effectively zero leverage


@dataclass
class PositionCalcResult:
    """Our calculated values for a single position."""

    symbol: str
    direction: str  # 'long' or 'short'

    # Unrealized PnL (absolute)
    unrealised_pnl_mark: Decimal  # Using mark price
    unrealised_pnl_last: Decimal  # Using last traded price

    # Unrealized PnL % (ROE)
    unrealised_pnl_pct_mark: Decimal  # standard formula, mark price
    unrealised_pnl_pct_last: Decimal  # standard formula, last price
    unrealised_pnl_pct_bybit: Decimal  # Bybit standard: unrealised / IM * 100

    # Position value and margin
    position_value: Decimal  # size * avg_entry_price (matches Bybit positionValue)
    initial_margin: Decimal  # tier-based IM (position_value * imr_rate)
    imr_rate: Decimal  # tier IMR rate used
    maintenance_margin: Decimal  # tier-based MM amount
    mmr_rate: Decimal  # tier MMR rate used

    # Liquidation
    liq_ratio: float  # liq_price / last_price

    # Funding (snapshot)
    funding_snapshot: Decimal  # size * mark_price * funding_rate (current moment)

    # Risk multipliers
    buy_multiplier: float = 1.0
    sell_multiplier: float = 1.0
    risk_rule_triggered: str = "none"

    # Data quality: False when position_im was too small for PnL% calc
    data_quality_ok: bool = True


@dataclass
class AccountCalcResult:
    """Account-level margin rate calculations."""

    imr_pct: Decimal  # our calc: total_IM / margin_balance * 100
    mmr_pct: Decimal  # our calc: total_MM / margin_balance * 100
    total_im: Decimal  # sum of per-position initial margins
    total_mm: Decimal  # sum of per-position maintenance margins


@dataclass
class CalculationResult:
    """All calculated values across positions."""

    positions: list[PositionCalcResult] = field(default_factory=list)
    account: AccountCalcResult | None = None
    data_quality_errors: list[str] = field(default_factory=list)


def _safe_leverage_int(leverage: Decimal) -> int:
    """Convert Decimal leverage to int, defaulting to 1 on conversion errors."""
    try:
        return int(round(leverage))
    except (ValueError, OverflowError):
        logger.warning("Invalid leverage value %s, defaulting to 1", leverage)
        return 1


def _calc_risk_multipliers(
    long_pos: PositionData | None,
    short_pos: PositionData | None,
    last_price: float,
    risk_config: RiskConfig,
    wallet_balance: Decimal,
) -> dict[str, tuple[float, float, str]]:
    """Calculate position risk multipliers using gridcore.Position.

    Args:
        wallet_balance: USDT wallet balance for margin ratio calculation.
            margin = positionValue / walletBalance (matches bbu2 pattern).

    Returns:
        Dict keyed by direction ('long'/'short') with tuple of
        (buy_multiplier, sell_multiplier, risk_rule_triggered)
    """
    long_mgr, short_mgr = Position.create_linked_pair(risk_config)

    def _margin_ratio(pos: PositionData | None) -> Decimal:
        """Compute margin as positionValue / walletBalance (bbu2 pattern)."""
        if pos is None:
            return Decimal("0")
        if wallet_balance <= 0:
            logger.warning("Zero or negative wallet balance in margin calculation")
            return Decimal("0")
        return pos.position_value / wallet_balance

    # Build PositionState for each direction
    long_state = PositionState(
        direction="long",
        size=long_pos.size if long_pos else Decimal("0"),
        entry_price=long_pos.avg_price if long_pos else None,
        margin=_margin_ratio(long_pos),
        liquidation_price=long_pos.liq_price if long_pos else Decimal("0"),
        leverage=_safe_leverage_int(long_pos.leverage) if long_pos else 1,
        position_value=long_pos.position_value if long_pos else Decimal("0"),
    )

    short_state = PositionState(
        direction="short",
        size=short_pos.size if short_pos else Decimal("0"),
        entry_price=short_pos.avg_price if short_pos else None,
        margin=_margin_ratio(short_pos),
        liquidation_price=short_pos.liq_price if short_pos else Decimal("0"),
        leverage=_safe_leverage_int(short_pos.leverage) if short_pos else 1,
        position_value=short_pos.position_value if short_pos else Decimal("0"),
    )

    results = {}

    # Reset only managers with open positions (matches bbu2 pattern).
    # Verified: bbu2 resets multipliers before calculate_amount_multiplier()
    # only when a position exists; skipping reset for absent positions
    # preserves the default multiplier state (1.0).
    if long_pos:
        long_mgr.reset_amount_multiplier()
    if short_pos:
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

    Raises:
        ArithmeticError: If Decimal operations overflow (e.g. division by
            near-zero values that bypass MIN_POSITION_IM / MIN_LEVERAGE guards).
        AttributeError: If fetch_result.wallet is None when wallet data is
            required (e.g. accessing usdt_wallet_balance for margin ratio).
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
            long_pos, short_pos, float(ticker.last_price), risk_config,
            wallet_balance=fetch_result.wallet.usdt_wallet_balance,
        )

        # Calculate per-position values
        for pos in positions:
            mark = pos.mark_price
            last = ticker.last_price

            # Unrealized PnL
            unrealised_mark = calc_unrealised_pnl(pos.direction, pos.avg_price, mark, pos.size)
            unrealised_last = calc_unrealised_pnl(pos.direction, pos.avg_price, last, pos.size)

            # Unrealized PnL % (standard formula)
            pct_mark = calc_unrealised_pnl_pct(pos.direction, pos.avg_price, mark, pos.leverage, symbol=pos.symbol)
            pct_last = calc_unrealised_pnl_pct(pos.direction, pos.avg_price, last, pos.leverage, symbol=pos.symbol)

            # Unrealized PnL % (Bybit standard)
            pct_bybit = Decimal("0")
            dq_ok = True
            if pos.position_im >= MIN_POSITION_IM:
                pct_bybit = unrealised_mark / pos.position_im * Decimal("100")
            else:
                dq_ok = False
                msg = f"{pos.symbol} {pos.direction}: position_im={pos.position_im} too small for PnL %% calc"
                logger.warning(msg, extra={"data_quality_issue": True})
                result.data_quality_errors.append(msg)

            # Position value and initial margin
            position_value = calc_position_value(pos.size, pos.avg_price)
            initial_margin = Decimal("0")
            imr_rate = Decimal("0")
            if pos.leverage >= MIN_LEVERAGE:
                tiers = (
                    symbol_data.risk_limit_tiers
                    if symbol_data.risk_limit_tiers is not None
                    else MM_TIERS.get(pos.symbol, MM_TIERS_DEFAULT)
                )
                initial_margin, imr_rate = calc_initial_margin(
                    position_value, pos.leverage, pos.symbol,
                    tiers=tiers,
                )
            else:
                msg = f"{pos.symbol} {pos.direction}: leverage={pos.leverage} too small for margin calc"
                logger.warning(msg, extra={"data_quality_issue": True})
                result.data_quality_errors.append(msg)

            # Maintenance margin (tier-based, uses dynamic tiers when available)
            maintenance_margin, mmr_rate = calc_maintenance_margin(
                position_value, pos.symbol, tiers=symbol_data.risk_limit_tiers
            )
            if position_value > 0 and (maintenance_margin == 0 or mmr_rate == 0):
                dq_ok = False
                msg = (
                    f"{pos.symbol} {pos.direction}: zero MM for non-zero position "
                    f"(pv={position_value}, mm={maintenance_margin}, mmr={mmr_rate})"
                )
                logger.warning(msg, extra={"data_quality_issue": True})
                result.data_quality_errors.append(msg)

            # Liquidation ratio
            liq_ratio = calc_liq_ratio(pos.liq_price, last) if last > 0 else 0.0

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
                unrealised_pnl_pct_mark=pct_mark,
                unrealised_pnl_pct_last=pct_last,
                unrealised_pnl_pct_bybit=pct_bybit,
                position_value=position_value,
                initial_margin=initial_margin,
                imr_rate=imr_rate,
                maintenance_margin=maintenance_margin,
                mmr_rate=mmr_rate,
                liq_ratio=liq_ratio,
                funding_snapshot=funding_snapshot,
                buy_multiplier=buy_mult,
                sell_multiplier=sell_mult,
                risk_rule_triggered=risk_rule,
                data_quality_ok=dq_ok,
            ))

    # Account-level margin rate calculations
    if fetch_result.wallet:
        total_im = sum(p.initial_margin for p in result.positions)
        total_mm = sum(p.maintenance_margin for p in result.positions)
        margin_balance = fetch_result.wallet.total_margin_balance
        result.account = AccountCalcResult(
            imr_pct=calc_imr_pct(total_im, margin_balance),
            mmr_pct=calc_mmr_pct(total_mm, margin_balance),
            total_im=total_im,
            total_mm=total_mm,
        )

    return result
