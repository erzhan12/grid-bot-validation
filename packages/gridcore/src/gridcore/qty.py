"""Shared qty calculator factory.

Parses config amount strings and returns a callable that computes
order quantity from intent + wallet balance. Used by backtest, replay,
and live gridbot layers.
"""

from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from gridcore.instrument_info import InstrumentInfo
from gridcore.intents import PlaceLimitIntent

# Type alias for qty calculator callable
QtyCalculator = Callable[[PlaceLimitIntent, Decimal], Decimal]


def create_qty_calculator(
    amount_str: str,
    instrument_info: Optional[InstrumentInfo] = None,
) -> QtyCalculator:
    """Create qty calculator from config amount pattern.

    Amount formats:
    - "x0.001": Fraction of wallet balance (0.1%)
    - "b0.001": Fixed base currency amount (BTC)
    - "100": Fixed USDT amount

    Quantities are rounded up to instrument's qty_step when instrument_info
    is provided (matching bbu2 behavior). Without instrument_info, raw
    (unrounded) quantities are returned.

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

        def qty_from_fraction(intent: PlaceLimitIntent, wallet_balance: Decimal) -> Decimal:
            if intent.price <= 0:
                return Decimal("0")
            return _round(wallet_balance * fraction / intent.price)

        return qty_from_fraction

    elif amount_str.startswith("b"):
        base_qty = _parse_decimal(amount_str[1:])

        def qty_fixed_base(intent: PlaceLimitIntent, wallet_balance: Decimal) -> Decimal:
            return _round(base_qty)

        return qty_fixed_base

    else:
        usdt_amount = _parse_decimal(amount_str)

        def qty_from_usdt(intent: PlaceLimitIntent, wallet_balance: Decimal) -> Decimal:
            if intent.price <= 0:
                return Decimal("0")
            return _round(usdt_amount / intent.price)

        return qty_from_usdt
