"""
Order intent models for grid trading strategy.

Intents represent the strategy's desired actions without performing them.
The strategy returns intents, and the execution layer (live trading or backtest)
handles actually placing/canceling orders.

This separation ensures the strategy remains pure and testable.
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib


@dataclass(frozen=True)
class PlaceLimitIntent:
    """
    Intent to place a limit order.

    The strategy emits this intent when it wants to place a new limit order
    at a specific grid level.
    """
    symbol: str
    side: str            # 'Buy' or 'Sell'
    price: Decimal
    qty: Decimal
    reduce_only: bool
    client_order_id: str  # Auto-generated UUID for matching
    grid_level: int       # Grid level index for comparison reports
    direction: str        # 'long' or 'short'

    # Parameters that determine order identity for deduplication
    # Excludes: qty (execution layer determines), reduce_only (order flag), grid_level (tracking only)
    # NOTE: grid_level is NOT part of identity hash - orders survive grid rebalancing
    _IDENTITY_PARAMS = ['symbol', 'side', 'price', 'direction']

    @classmethod
    def create(
        cls,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        grid_level: int,
        direction: str,
        reduce_only: bool = False,
    ) -> "PlaceLimitIntent":
        """
        Factory method to create a PlaceLimitIntent with deterministic client_order_id.

        The client_order_id is generated deterministically from order identity parameters
        (see _IDENTITY_PARAMS), ensuring that duplicate placement attempts at the same
        price produce the same ID. This allows the execution layer to detect and skip
        duplicates.

        IMPORTANT: grid_level is NOT part of the identity hash. Orders are identified
        by (symbol, side, price, direction) only. This means orders survive grid
        rebalancing (center_grid) as long as the price remains in the grid. The
        grid_level field is preserved for tracking and analytics purposes.

        NOTE: When adding new parameters, consider whether they should be part of the
        identity hash. If yes, add to _IDENTITY_PARAMS. If no (like qty, grid_level), don't add.

        Args:
            symbol: Trading pair symbol
            side: 'Buy' or 'Sell'
            price: Limit order price
            qty: Order quantity (NOT part of identity hash)
            grid_level: Grid level index (for tracking/reporting, NOT part of identity hash)
            direction: 'long' or 'short'
            reduce_only: Whether this is a reduce-only order (NOT part of identity hash)

        Returns:
            PlaceLimitIntent with deterministic client_order_id
        """
        # Generate deterministic client_order_id from identity parameters
        # Dynamically build from _IDENTITY_PARAMS to avoid maintenance issues
        params = locals()
        id_components = [str(params[param]) for param in cls._IDENTITY_PARAMS]
        id_string = "_".join(id_components)
        deterministic_id = hashlib.sha256(id_string.encode()).hexdigest()[:16]

        return cls(
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            reduce_only=reduce_only,
            client_order_id=deterministic_id,
            grid_level=grid_level,
            direction=direction,
        )


@dataclass(frozen=True)
class CancelIntent:
    """
    Intent to cancel an existing order.

    The strategy emits this intent when it wants to cancel an order
    (e.g., price outside grid range, side mismatch, grid rebuild).
    """
    symbol: str
    order_id: str
    reason: str  # 'side_mismatch', 'outside_grid', 'rebuild', etc.

    # Optional fields for tracking
    price: Decimal | None = None
    side: str | None = None
