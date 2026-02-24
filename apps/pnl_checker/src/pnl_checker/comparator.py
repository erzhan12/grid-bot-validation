"""Compare our calculated values against Bybit's reported values."""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from pnl_checker.fetcher import FetchResult, FundingData, PositionData, WalletData
from pnl_checker.calculator import CalculationResult, PositionCalcResult

logger = logging.getLogger(__name__)

# PnL % values are 100x larger than USDT values, so tolerance scales accordingly
PERCENTAGE_TOLERANCE_MULTIPLIER = 100


@dataclass
class FieldComparison:
    """Comparison of a single field between Bybit and our calculation."""

    field_name: str
    bybit_value: Decimal | str
    our_value: Decimal | str
    delta: Decimal | str  # Absolute difference, or "N/A" for display-only
    passed: bool | None  # None = informational (no tolerance check)

    @property
    def is_numeric(self) -> bool:
        return isinstance(self.delta, Decimal)


@dataclass
class PositionComparison:
    """Comparison results for a single position."""

    symbol: str
    direction: str
    fields: list[FieldComparison] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for f in self.fields if f.passed is True)

    @property
    def fail_count(self) -> int:
        return sum(1 for f in self.fields if f.passed is False)

    @property
    def total_checked(self) -> int:
        return sum(1 for f in self.fields if f.passed is not None)


@dataclass
class AccountComparison:
    """Account-level summary fields (display only)."""

    fields: list[FieldComparison] = field(default_factory=list)


@dataclass
class ComparisonResult:
    """Full comparison result."""

    positions: list[PositionComparison] = field(default_factory=list)
    account: AccountComparison = field(default_factory=AccountComparison)
    tolerance: float = 0.01

    @property
    def total_pass(self) -> int:
        return sum(p.pass_count for p in self.positions)

    @property
    def total_fail(self) -> int:
        return sum(p.fail_count for p in self.positions)

    @property
    def all_passed(self) -> bool:
        return self.total_fail == 0


def _compare_field(
    name: str,
    bybit_val: Decimal,
    our_val: Decimal,
    tolerance: float,
) -> FieldComparison:
    """Compare a numeric field with tolerance check."""
    delta = abs(our_val - bybit_val)
    passed = float(delta) <= tolerance
    return FieldComparison(
        field_name=name,
        bybit_value=bybit_val,
        our_value=our_val,
        delta=delta,
        passed=passed,
    )


def _info_field(
    name: str,
    bybit_val: Decimal | str,
    our_val: Decimal | str = "—",
) -> FieldComparison:
    """Create an informational field (no tolerance check)."""
    return FieldComparison(
        field_name=name,
        bybit_value=bybit_val,
        our_value=our_val,
        delta="—",
        passed=None,
    )


def _compare_position(
    pos_data: PositionData,
    calc: PositionCalcResult,
    tolerance: float,
) -> PositionComparison:
    """Compare a single position's values."""
    comp = PositionComparison(symbol=pos_data.symbol, direction=pos_data.direction)

    # --- Checked fields (with tolerance) ---

    # Unrealized PnL (mark price) — primary comparison
    comp.fields.append(_compare_field(
        "Unrealized PnL (mark)",
        pos_data.unrealised_pnl,
        calc.unrealised_pnl_mark,
        tolerance,
    ))

    # Position Value
    comp.fields.append(_compare_field(
        "Position Value",
        pos_data.position_value,
        calc.position_value,
        tolerance,
    ))

    # Initial Margin (informational — Bybit UTA hedge mode reports optimized
    # positionIM that differs from standard positionValue/leverage formula)
    comp.fields.append(_info_field(
        "Initial Margin",
        pos_data.position_im,
        calc.initial_margin,
    ))

    # Unrealized PnL % (Bybit standard)
    # Tolerance is scaled by 100x because ROE values are percentages (0-100+)
    # while the base tolerance is in USDT (e.g., 0.01 USDT → 1.0% threshold)
    bybit_roe = Decimal("0")
    if pos_data.position_im > 0:
        bybit_roe = pos_data.unrealised_pnl / pos_data.position_im * Decimal("100")
    comp.fields.append(_compare_field(
        "Unrealized PnL % (Bybit ROE)",
        bybit_roe,
        calc.unrealised_pnl_pct_bybit,
        tolerance * PERCENTAGE_TOLERANCE_MULTIPLIER,
    ))

    # --- Informational fields (no check) ---

    # Unrealized PnL (last price) — our calc only, Bybit doesn't report this
    comp.fields.append(_info_field(
        "Unrealized PnL (last price)",
        "—",
        calc.unrealised_pnl_last,
    ))

    # PnL % bbu2 formula (mark)
    comp.fields.append(_info_field(
        "PnL % bbu2 (mark)",
        "—",
        calc.unrealised_pnl_pct_bbu2_mark,
    ))

    # PnL % bbu2 formula (last)
    comp.fields.append(_info_field(
        "PnL % bbu2 (last)",
        "—",
        calc.unrealised_pnl_pct_bbu2_last,
    ))

    # Entry price
    comp.fields.append(_info_field("Avg Entry Price", pos_data.avg_price))

    # Mark price
    comp.fields.append(_info_field("Mark Price", pos_data.mark_price))

    # Leverage
    comp.fields.append(_info_field("Leverage", pos_data.leverage))

    # Liquidation price + ratio
    comp.fields.append(_info_field("Liquidation Price", pos_data.liq_price))
    comp.fields.append(_info_field(
        "Liq Ratio",
        "—",
        f"{calc.liq_ratio:.4f}",
    ))

    # Maintenance margin
    comp.fields.append(_info_field("Maintenance Margin", pos_data.position_mm))

    # Realized PnL
    comp.fields.append(_info_field("Cur Realized PnL", pos_data.cur_realised_pnl))
    comp.fields.append(_info_field("Cum Realized PnL", pos_data.cum_realised_pnl))

    # Funding snapshot
    comp.fields.append(_info_field(
        "Funding Snapshot (cur rate)",
        "—",
        calc.funding_snapshot,
    ))

    # Risk multipliers
    comp.fields.append(_info_field(
        "Risk: Buy Multiplier",
        "—",
        f"{calc.buy_multiplier:.1f}",
    ))
    comp.fields.append(_info_field(
        "Risk: Sell Multiplier",
        "—",
        f"{calc.sell_multiplier:.1f}",
    ))
    comp.fields.append(_info_field(
        "Risk Rule Triggered",
        "—",
        calc.risk_rule_triggered,
    ))

    return comp


def _build_account_comparison(wallet: WalletData) -> AccountComparison:
    """Build account-level summary (informational only)."""
    comp = AccountComparison()
    comp.fields = [
        _info_field("Total Equity", wallet.total_equity),
        _info_field("Total Wallet Balance", wallet.total_wallet_balance),
        _info_field("Total Margin Balance", wallet.total_margin_balance),
        _info_field("Total Available Balance", wallet.total_available_balance),
        _info_field("Total Perp Unrealized PnL", wallet.total_perp_upl),
        _info_field("Total Initial Margin", wallet.total_initial_margin),
        _info_field("Total Maintenance Margin", wallet.total_maintenance_margin),
        _info_field("USDT Wallet Balance", wallet.usdt_wallet_balance),
        _info_field("USDT Unrealized PnL", wallet.usdt_unrealised_pnl),
        _info_field("USDT Cum Realized PnL", wallet.usdt_cum_realised_pnl),
    ]
    return comp


def _build_funding_fields(funding: FundingData, funding_max_pages: int) -> list[FieldComparison]:
    """Build funding-related fields for a position comparison.

    Returns a list of FieldComparison entries for funding data,
    including error/truncation warnings when applicable.
    """
    fields: list[FieldComparison] = []

    fields.append(_info_field(
        "Cum Funding (from tx log)",
        funding.cumulative_funding,
    ))
    fields.append(_info_field(
        "Funding Record Count",
        f"{funding.transaction_count} records",
    ))

    if funding.fetch_error:
        fields.append(FieldComparison(
            field_name="Funding Fetch Error",
            bybit_value="—",
            our_value=funding.fetch_error,
            delta="—",
            passed=False,
        ))

    if funding.truncated:
        fields.append(FieldComparison(
            field_name="Funding Data Truncated",
            bybit_value="—",
            our_value=f"Truncated at {funding_max_pages} pages; total may be incomplete",
            delta="—",
            passed=False,
        ))

    return fields


def compare(
    fetch_result: FetchResult,
    calc_result: CalculationResult,
    tolerance: float,
    funding_max_pages: int = 20,
) -> ComparisonResult:
    """Compare fetched Bybit data against our calculations.

    Args:
        fetch_result: Raw data from Bybit
        calc_result: Our calculated values
        tolerance: USDT tolerance for pass/fail
        funding_max_pages: Max pages used for funding pagination (for warning message)

    Returns:
        ComparisonResult with per-position and account-level comparisons
    """
    result = ComparisonResult(tolerance=tolerance)

    # Build position lookup by (symbol, direction)
    calc_by_key = {
        (c.symbol, c.direction): c for c in calc_result.positions
    }

    # Compare each position
    for symbol_data in fetch_result.symbols:
        funding = symbol_data.funding
        funding_attached = False

        for pos_data in symbol_data.positions:
            key = (pos_data.symbol, pos_data.direction)
            calc = calc_by_key.get(key)
            if calc is None:
                logger.warning(f"No calculation found for {key}")
                pos_comp = PositionComparison(
                    symbol=pos_data.symbol,
                    direction=pos_data.direction,
                    fields=[FieldComparison(
                        field_name="Calculation Missing",
                        bybit_value="—",
                        our_value="No matching calculation result",
                        delta="—",
                        passed=False,
                    )],
                )
                result.positions.append(pos_comp)
                continue

            pos_comp = _compare_position(pos_data, calc, tolerance)

            # Funding data is per-symbol, but our output is per-position.
            # In hedge mode a symbol has two positions (long + short); we
            # attach funding to the first one only to avoid duplicating the
            # same cumulative total in both rows.  A separate symbol-level
            # section would be cleaner structurally but adds complexity to
            # the reporter and JSON schema for little benefit.
            if not funding_attached:
                pos_comp.fields.extend(_build_funding_fields(funding, funding_max_pages))
                funding_attached = True

            result.positions.append(pos_comp)

    # Account summary
    if fetch_result.wallet:
        result.account = _build_account_comparison(fetch_result.wallet)

    return result
