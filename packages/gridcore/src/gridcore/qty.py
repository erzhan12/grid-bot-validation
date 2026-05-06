"""Shared qty calculator factory.

Parses config amount strings and returns a callable that computes
order quantity from intent + wallet balance. Used by backtest, replay,
and live gridbot layers.
"""

from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from gridcore.instrument_info import InstrumentInfo
from gridcore.intents import PlaceLimitIntent
from gridcore.position import Position

# Type alias for qty calculator callable
QtyCalculator = Callable[[PlaceLimitIntent, Decimal], Decimal]

# bbu2 hardcodes a $5 USDT minimum notional in __get_amount; matches
# `min_amount_usdt = 5` at bbu_reference/bbu2-master/bybit_api_usdt.py:491.
_MIN_NOTIONAL_USDT = Decimal("5")

# Early-imbalance trigger band — bbu2 reference
# `bbu_reference/bbu2-master/bybit_api_usdt.py:257-261`. Asymmetric:
# fires only when long.size / short.size lands in this open band.
_EARLY_IMBALANCE_MIN_RATIO = 1.1
_EARLY_IMBALANCE_MAX_RATIO = 10.0


def apply_early_imbalance(
    qty: Decimal,
    long_position: Position,
    short_position: Position,
    multiplier: float,
) -> Decimal:
    """Apply bbu2 early-imbalance multiplier to a resolved qty.

    Mirrors bbu2 bybit_api_usdt.py:257-261 — multiplies the next order's
    qty by `multiplier` when:
      - `_EARLY_IMBALANCE_MIN_RATIO < size_ratio < _EARLY_IMBALANCE_MAX_RATIO`
        (asymmetric: long must dominate short; no short-dominant mirror)
      - both positions are pre-liquidation (`liquidation_price == 0`)

    bbu2 uses a SIZE-based ratio (`long.size / short.size`). Reads
    `Position.size` directly. Do NOT use `Position.position_ratio`,
    which is overwritten with margin-based ratio inside
    `Position.calculate_amount_multiplier` (`gridcore/position.py:231`).

    Args:
        qty: The qty already composed with `amount_multiplier`, before
            instrument-step rounding (matches bbu2 ordering: multiplier
            is applied before `__round_amount`).
        long_position: Long-direction Position with current `size` and
            `liquidation_price` cached.
        short_position: Short-direction Position with same.
        multiplier: User-configured `early_imbalance_multiplier`. The
            common no-op case (== 1.0) short-circuits without any of
            the size/liq reads.

    Returns:
        `qty * multiplier` if the bbu2 trigger fires, else `qty`
        unchanged.
    """
    if multiplier == 1.0:
        return qty
    long_size = float(long_position.size)
    short_size = float(short_position.size)
    if short_size > 0:
        size_ratio = long_size / short_size
    elif long_size > 0:
        size_ratio = float("inf")  # short empty — out of band by `< MAX`
    else:
        size_ratio = 1.0
    if (_EARLY_IMBALANCE_MIN_RATIO < size_ratio < _EARLY_IMBALANCE_MAX_RATIO
            and long_position.liquidation_price == 0
            and short_position.liquidation_price == 0):
        return qty * Decimal(str(multiplier))
    return qty


def _apply_min_notional(raw_qty: Decimal, price: Decimal) -> Decimal:
    """Bump qty up to the $5 USDT notional floor when below it.

    Mirrors bbu2 bybit_api_usdt.py:520-522:
        min_amount = min_amount_usdt / price
        if amount < min_amount:
            amount = self.__round_amount(min_amount)
    Strict `<` (boundary at exactly $5 passes through unchanged).
    """
    if price <= 0:
        return raw_qty
    if raw_qty * price < _MIN_NOTIONAL_USDT:
        return _MIN_NOTIONAL_USDT / price
    return raw_qty


def create_qty_calculator(
    amount_str: str,
    instrument_info: Optional[InstrumentInfo] = None,
) -> QtyCalculator:
    """Create qty calculator from config amount pattern.

    Amount formats:
    - "x0.001": Fraction of wallet balance (0.1%)
    - "100": Fixed USDT amount

    Quantities are rounded up to instrument's qty_step when instrument_info
    is provided (matching bbu2 behavior). Without instrument_info, raw
    (unrounded) quantities are returned. A $5 USDT min-notional floor is
    applied before rounding (bbu2 parity).

    Args:
        amount_str: Amount pattern from config.
        instrument_info: Optional instrument info for qty rounding.

    Returns:
        Callable that takes (intent, wallet_balance) and returns qty.

    Raises:
        ValueError: If amount_str is empty or unparseable.
    """
    if not amount_str:
        raise ValueError("amount string must not be empty")

    def _round(raw_qty: Decimal) -> Decimal:
        return instrument_info.round_qty(raw_qty) if instrument_info else raw_qty

    def _parse_decimal(s: str) -> Decimal:
        try:
            return Decimal(s)
        except InvalidOperation:
            raise ValueError(f"invalid amount string: {amount_str!r}")

    if amount_str.startswith("x"):
        fraction = _parse_decimal(amount_str[1:])

        # Fraction mode short-circuits on wallet_balance <= 0: a wallet-
        # fraction config logically has zero base when there is no wallet,
        # and emitting the $5 floor in that state would put orders on the
        # exchange that cannot be margined. Asymmetric vs USDT mode below,
        # which is explicit and not derived from wallet — there a non-zero
        # absolute amount is still meaningful even at wallet=0 (e.g., for
        # restored-state edge cases where positions exist before the wallet
        # snapshot lands). Documented divergence, not a bug.
        def qty_from_fraction(intent: PlaceLimitIntent, wallet_balance: Decimal) -> Decimal:
            if intent.price <= 0 or wallet_balance <= 0:
                return Decimal("0")
            raw = wallet_balance * fraction / intent.price
            raw = _apply_min_notional(raw, intent.price)
            return _round(raw)

        return qty_from_fraction

    else:
        usdt_amount = _parse_decimal(amount_str)

        def qty_from_usdt(intent: PlaceLimitIntent, wallet_balance: Decimal) -> Decimal:
            # No wallet_balance check: USDT mode is wallet-independent
            # (see asymmetry note in qty_from_fraction above).
            if intent.price <= 0:
                return Decimal("0")
            raw = usdt_amount / intent.price
            raw = _apply_min_notional(raw, intent.price)
            return _round(raw)

        return qty_from_usdt
