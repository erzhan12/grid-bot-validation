"""Instrument trading parameters (qty rounding, tick sizing).

Pure data class with no external dependencies.
Provider/fetcher logic lives in app layers (backtest, gridbot).
"""

import math
from decimal import Decimal


class InstrumentInfo:
    """Instrument trading parameters."""

    def __init__(
        self,
        symbol: str,
        qty_step: Decimal,
        tick_size: Decimal,
        min_qty: Decimal,
        max_qty: Decimal,
    ):
        self.symbol = symbol
        self.qty_step = qty_step
        self.tick_size = tick_size
        self.min_qty = min_qty
        self.max_qty = max_qty

    def round_qty(self, qty: Decimal) -> Decimal:
        """Round quantity up to nearest qty_step (matching bbu2 behavior)."""
        steps = math.ceil(float(qty) / float(self.qty_step))
        return Decimal(str(steps)) * self.qty_step

    def round_price(self, price: Decimal) -> Decimal:
        """Round price to nearest tick_size."""
        steps = round(float(price) / float(self.tick_size))
        return Decimal(str(steps)) * self.tick_size

    def to_dict(self) -> dict:
        """Convert to dictionary for caching."""
        return {
            "symbol": self.symbol,
            "qty_step": str(self.qty_step),
            "tick_size": str(self.tick_size),
            "min_qty": str(self.min_qty),
            "max_qty": str(self.max_qty),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InstrumentInfo":
        """Create from cached dictionary."""
        return cls(
            symbol=data["symbol"],
            qty_step=Decimal(data["qty_step"]),
            tick_size=Decimal(data["tick_size"]),
            min_qty=Decimal(data["min_qty"]),
            max_qty=Decimal(data["max_qty"]),
        )
