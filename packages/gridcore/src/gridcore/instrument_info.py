"""Instrument trading parameters (qty rounding, tick sizing).

Pure data class with no external dependencies.
Provider/fetcher logic lives in app layers (backtest, gridbot).
"""

import logging
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Optional

logger = logging.getLogger(__name__)


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
        if qty_step <= 0:
            raise ValueError(f"qty_step must be positive, got {qty_step}")
        if tick_size <= 0:
            raise ValueError(f"tick_size must be positive, got {tick_size}")
        self.symbol = symbol
        self.qty_step = qty_step
        self.tick_size = tick_size
        self.min_qty = min_qty
        self.max_qty = max_qty

    def round_qty(self, qty: Decimal) -> Decimal:
        """Round quantity up to nearest qty_step (matching bbu2 behavior)."""
        if qty <= 0:
            return Decimal("0")
        steps = (qty / self.qty_step).to_integral_value(rounding=ROUND_UP)
        return steps * self.qty_step

    def round_price(self, price: Decimal) -> Decimal:
        """Round price to nearest tick_size."""
        steps = (price / self.tick_size).to_integral_value(rounding=ROUND_HALF_UP)
        return steps * self.tick_size

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

    @classmethod
    def from_bybit_response(cls, symbol: str, instrument: dict) -> Optional["InstrumentInfo"]:
        """Parse a single instrument entry from Bybit get_instruments_info response.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT").
            instrument: Single instrument dict from response["result"]["list"][0].

        Returns:
            InstrumentInfo if valid, None if params are invalid (zero qty_step/tick_size).
        """
        lot_filter = instrument.get("lotSizeFilter", {})
        price_filter = instrument.get("priceFilter", {})

        qty_step = Decimal(lot_filter.get("qtyStep", "0.001"))
        tick_size = Decimal(price_filter.get("tickSize", "0.1"))

        if qty_step <= 0 or tick_size <= 0:
            logger.warning(
                f"Invalid instrument params for {symbol}: "
                f"qty_step={qty_step}, tick_size={tick_size}"
            )
            return None

        return cls(
            symbol=symbol,
            qty_step=qty_step,
            tick_size=tick_size,
            min_qty=Decimal(lot_filter.get("minOrderQty", "0.001")),
            max_qty=Decimal(lot_filter.get("maxOrderQty", "1000")),
        )
