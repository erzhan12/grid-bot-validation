"""Simulated order book for backtest.

Manages limit orders in simulation, checks for fills, and generates execution events.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, overload

from gridcore import ExecutionEvent, EventType, TickerEvent

from backtest.fill_simulator import TradeThroughFillSimulator


logger = logging.getLogger(__name__)


@dataclass
class SimulatedOrder:
    """Order in the simulated order book."""

    order_id: str
    client_order_id: str
    symbol: str
    side: str  # 'Buy' or 'Sell'
    price: Decimal
    qty: Decimal
    direction: str  # 'long' or 'short'
    grid_level: int
    status: str = "pending"  # 'pending', 'filled', 'cancelled'
    created_ts: datetime = field(default_factory=datetime.now)
    filled_ts: Optional[datetime] = None
    reduce_only: bool = False


class BacktestOrderManager:
    """Manages simulated orders during backtest.

    Tracks active and filled orders, checks for fills on each tick,
    and generates ExecutionEvent when orders fill.
    """

    def __init__(
        self,
        fill_simulator: TradeThroughFillSimulator,
        commission_rate: Decimal = Decimal("0.0002"),
    ):
        """Initialize order manager.

        Args:
            fill_simulator: Fill logic implementation
            commission_rate: Commission rate for fee calculation
        """
        self.fill_simulator = fill_simulator
        self.commission_rate = commission_rate

        # Order tracking
        self.active_orders: dict[str, SimulatedOrder] = {}  # order_id -> order
        self.filled_orders: list[SimulatedOrder] = []
        self.cancelled_orders: list[SimulatedOrder] = []

        # Client order ID tracking for deduplication
        self._client_order_ids: set[str] = set()

        # Order ID counter for generating unique IDs
        self._order_counter = 0

        # event_follower (feature 0072): count of recorded executions whose
        # exec_qty exceeded the replay order's remaining placed qty (capped
        # at placed qty; excess is intent-set / sizing divergence).
        self.qty_excess_divergence_count = 0

    def place_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        direction: str,
        grid_level: int,
        timestamp: datetime,
        reduce_only: bool = False,
    ) -> Optional[SimulatedOrder]:
        """Place order in simulated order book.

        Args:
            client_order_id: Client-provided order ID for deduplication
            symbol: Trading symbol
            side: 'Buy' or 'Sell'
            price: Limit price
            qty: Order quantity
            direction: 'long' or 'short'
            grid_level: Grid level index
            timestamp: Order creation time
            reduce_only: Whether this is a reduce-only order

        Returns:
            Created order, or None if duplicate client_order_id
        """
        # Check for duplicate (deduplication by client_order_id)
        if client_order_id in self._client_order_ids:
            return None

        self._order_counter += 1
        order_id = f"sim_{self._order_counter:08d}"

        order = SimulatedOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            direction=direction,
            grid_level=grid_level,
            status="pending",
            created_ts=timestamp,
            reduce_only=reduce_only,
        )

        self.active_orders[order_id] = order
        self._client_order_ids.add(client_order_id)
        return order

    def seed_active_orders(self, orders) -> None:
        """Register pre-existing live orders as active simulated orders.

        Used by the replay engine (feature 0029) to seed the order book
        with orders that were live on the exchange at ``seed.at_ts``.
        Each seed becomes a ``SimulatedOrder`` with status ``"pending"``
        (the active-state marker the simulator uses) and is inserted
        into ``self.active_orders`` keyed by ``exchange_order_id``;
        ``client_id`` is recorded in ``self._client_order_ids`` for
        deduplication.

        Subsequent ``check_fills`` calls iterate ``self.active_orders``
        and run the fill simulator on every order;
        ``TradeThroughFillSimulator`` reads the configured market input
        according to its fill mode (strict cross by default), so seeded
        orders are eligible for fills under the same semantics as newly
        placed simulated orders.

        ``grid_level`` is set to ``0`` for every seeded order: live
        active orders carry no level metadata, and the simulator does
        not consult ``grid_level`` for fill eligibility — the field
        exists only for grid-internal accounting that does not apply
        to externally-seeded orders.

        Args:
            orders: Iterable of ``ActiveOrderSeed`` from
                ``apps/replay/src/replay/snapshot_loader.py``. Accepted
                as duck-typed objects with attributes ``client_id``,
                ``exchange_order_id``, ``symbol``, ``side``, ``direction``,
                ``price``, ``remaining_qty``, ``reduce_only``,
                ``exchange_ts``.
        """
        for seed in orders:
            # 0029 PR #68 P1: defense-in-depth against a malformed seed
            # batch with duplicate exchange_order_id (would otherwise
            # silently overwrite the first entry).
            if seed.exchange_order_id in self.active_orders:
                logger.warning(
                    "Duplicate exchange_order_id %r in seed batch; skipping "
                    "second occurrence",
                    seed.exchange_order_id,
                )
                continue
            order = SimulatedOrder(
                order_id=seed.exchange_order_id,
                client_order_id=seed.client_id,
                symbol=seed.symbol,
                side=seed.side,
                price=seed.price,
                qty=seed.remaining_qty,
                direction=seed.direction,
                grid_level=0,
                status="pending",
                created_ts=seed.exchange_ts,
                reduce_only=seed.reduce_only,
            )
            self.active_orders[seed.exchange_order_id] = order
            self._client_order_ids.add(seed.client_id)

    def cancel_order(self, order_id: str, timestamp: datetime) -> bool:
        """Cancel order from simulated order book.

        Args:
            order_id: Order ID to cancel
            timestamp: Cancellation time

        Returns:
            True if order was cancelled, False if not found
        """
        if order_id not in self.active_orders:
            return False

        order = self.active_orders.pop(order_id)
        order.status = "cancelled"
        self.cancelled_orders.append(order)
        # Allow client_order_id to be reused
        self._client_order_ids.discard(order.client_order_id)
        return True

    def cancel_by_client_order_id(self, client_order_id: str, timestamp: datetime) -> bool:
        """Cancel order by client order ID.

        Args:
            client_order_id: Client order ID to cancel
            timestamp: Cancellation time

        Returns:
            True if order was cancelled, False if not found
        """
        for order_id, order in list(self.active_orders.items()):
            if order.client_order_id == client_order_id:
                return self.cancel_order(order_id, timestamp)
        return False

    @overload
    def check_fills(
        self,
        market: TickerEvent,
        timestamp: Optional[datetime] = None,
        symbol: Optional[str] = None,
    ) -> list[ExecutionEvent]: ...

    @overload
    def check_fills(
        self,
        market: Decimal,
        timestamp: datetime,
        symbol: Optional[str] = None,
    ) -> list[ExecutionEvent]: ...

    @overload
    def check_fills(
        self,
        *,
        current_price: Decimal,
        timestamp: datetime,
        symbol: Optional[str] = None,
    ) -> list[ExecutionEvent]: ...

    def check_fills(
        self,
        market: TickerEvent | Decimal | None = None,
        timestamp: Optional[datetime] = None,
        symbol: Optional[str] = None,
        *,
        current_price: Optional[Decimal] = None,
    ) -> list[ExecutionEvent]:
        """Check all active orders for fills.

        Args:
            market: Current ticker event, or legacy bare market price.
            timestamp: Fill timestamp. Defaults to ``market.exchange_ts`` for
                TickerEvent input; required for legacy Decimal input.
            symbol: Optional symbol filter. TickerEvent input is always scoped
                to ``market.symbol``; Decimal input preserves the legacy
                all-symbol scan when symbol is omitted.
            current_price: Deprecated keyword-only alias for legacy bare
                Decimal callers. Prefer ``market`` for new code.

        Returns:
            List of ExecutionEvent for filled orders
        """
        if market is None:
            if current_price is None:
                raise ValueError(
                    "Either 'market' (TickerEvent or Decimal) or 'current_price' "
                    "keyword argument is required"
                )
            market = current_price

        if isinstance(market, TickerEvent):
            # Advance per-tick simulator state for LAST_CROSS before the
            # per-order loop. Runs unconditionally on every TickerEvent so
            # the T -> T+1 transition signal is never lost on orderless
            # ticks (grid fully filled one side, gaps between cascades).
            # Legacy bare-Decimal path below intentionally never calls
            # advance_market (no symbol/exchange_ts available).
            self.fill_simulator.advance_market(market)
            fill_timestamp = timestamp or market.exchange_ts
            symbol_filter = market.symbol
        else:
            if timestamp is None:
                raise ValueError(
                    "timestamp is required when checking fills with a bare Decimal price"
                )
            fill_timestamp = timestamp
            symbol_filter = symbol

        fills: list[ExecutionEvent] = []

        for order_id, order in list(self.active_orders.items()):
            # Skip if symbol filter specified and doesn't match
            if symbol_filter is not None and order.symbol != symbol_filter:
                continue

            fill_result = self.fill_simulator.check_fill(order, market)

            if fill_result.should_fill:
                # Move from active to filled
                self.active_orders.pop(order_id)
                order.status = "filled"
                order.filled_ts = fill_timestamp
                self.filled_orders.append(order)
                # Allow client_order_id to be reused
                self._client_order_ids.discard(order.client_order_id)

                # Calculate commission
                fee = order.qty * fill_result.fill_price * self.commission_rate

                # Create ExecutionEvent
                exec_event = ExecutionEvent(
                    event_type=EventType.EXECUTION,
                    symbol=order.symbol,
                    exchange_ts=fill_timestamp,
                    local_ts=fill_timestamp,
                    exec_id=f"exec_{uuid.uuid4().hex[:8]}",
                    order_id=order.order_id,
                    order_link_id=order.client_order_id,
                    side=order.side,
                    price=fill_result.fill_price,
                    qty=order.qty,
                    fee=fee,
                    closed_pnl=Decimal("0"),  # Will be calculated by position tracker
                    closed_size=Decimal("0"),  # Not used in backtest (live bot uses for same-order detection)
                    leaves_qty=Decimal("0"),  # Fully filled
                )
                fills.append(exec_event)

        return fills

    def apply_recorded_fill(
        self,
        replay_order_id: str,
        exec_price: Decimal,
        exec_qty: Decimal,
        exec_fee: Decimal,
        closed_pnl: Decimal,
        timestamp: datetime,
        exec_id: Optional[str] = None,
    ) -> tuple[Optional[ExecutionEvent], bool]:
        """Apply an externally-decided (recorded live) fill to an active order.

        event_follower mode (feature 0072): the fill decision comes from the
        recorded ``private_executions`` stream, not the fill simulator. The
        recorded values are applied as-is — never recomputed — subject to the
        placed-qty invariant: application never exceeds what replay actually
        placed.

        Args:
            replay_order_id: The replay ``SimulatedOrder.order_id`` (``sim_*``,
                or exchange id for seeded orders) — NOT the recorded live
                Bybit order_id.
            exec_price: Recorded execution price.
            exec_qty: Recorded execution qty (may exceed remaining placed qty
                on intent-set divergence — capped, WARN, counted).
            exec_fee: Recorded fee for the full row (pro-rated to the applied
                slice when capped).
            closed_pnl: Recorded closed PnL for the full row (pro-rated like
                the fee).
            timestamp: Execution exchange_ts.
            exec_id: Recorded exec_id for traceability; generated when absent.

        Returns:
            ``(ExecutionEvent | None, is_fully_filled)``. ``(None, False)``
            when the order is unknown or already fully consumed
            (``apply_qty == 0``) — caller treats the row as unmatched
            (``live_only``). The event covers ONLY the applied slice:
            ``qty=apply_qty``, pro-rated fee/pnl, ``leaves_qty`` = remaining
            placed qty after this partial.
        """
        order = self.active_orders.get(replay_order_id)
        if order is None:
            return None, False

        apply_qty = min(exec_qty, order.qty)
        if apply_qty <= 0:
            return None, False

        if exec_qty > order.qty:
            self.qty_excess_divergence_count += 1
            logger.warning(
                "qty_excess_divergence: recorded exec_qty=%s exceeds remaining "
                "placed qty=%s on replay order %s (client_order_id=%s); "
                "applying only %s — excess is intent-set/sizing divergence",
                exec_qty, order.qty, replay_order_id,
                order.client_order_id, apply_qty,
            )

        # Pro-rate recorded fee/pnl to the applied slice.
        if apply_qty == exec_qty:
            apply_fee = exec_fee
            apply_pnl = closed_pnl
        else:
            ratio = apply_qty / exec_qty
            apply_fee = exec_fee * ratio
            apply_pnl = closed_pnl * ratio

        is_fully_filled = apply_qty == order.qty
        if is_fully_filled:
            # Same pop path as the should_fill branch of check_fills.
            self.active_orders.pop(replay_order_id)
            order.status = "filled"
            order.filled_ts = timestamp
            self.filled_orders.append(order)
            self._client_order_ids.discard(order.client_order_id)
            leaves_qty = Decimal("0")
        else:
            # Partial: decrement remaining qty, keep active; client_order_id
            # stays reserved until full fill.
            order.qty -= apply_qty
            leaves_qty = order.qty

        return (
            ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol=order.symbol,
                exchange_ts=timestamp,
                local_ts=timestamp,
                exec_id=exec_id or f"exec_{uuid.uuid4().hex[:8]}",
                order_id=order.order_id,
                order_link_id=order.client_order_id,
                side=order.side,
                price=exec_price,
                qty=apply_qty,
                fee=apply_fee,
                closed_pnl=apply_pnl,
                closed_size=Decimal("0"),  # Not used in backtest
                leaves_qty=leaves_qty,
            ),
            is_fully_filled,
        )

    def get_limit_orders(self) -> dict[str, list[dict]]:
        """Get orders in format expected by GridEngine.

        Returns dict with 'long' and 'short' keys, each containing
        list of order dicts with price, qty, side, orderId, orderLinkId.

        Note: Uses camelCase keys to match GridEngine expectations
        (matching Bybit API response format).
        """
        result: dict[str, list[dict]] = {"long": [], "short": []}

        for order in self.active_orders.values():
            order_dict = {
                "price": str(order.price),  # String like Bybit API
                "qty": str(order.qty),
                "side": order.side,
                "orderId": order.order_id,  # camelCase for GridEngine
                "orderLinkId": order.client_order_id,
            }
            result[order.direction].append(order_dict)

        return result

    def get_order_by_id(self, order_id: str) -> Optional[SimulatedOrder]:
        """Get active order by ID."""
        return self.active_orders.get(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> Optional[SimulatedOrder]:
        """Get order by client order ID (searches active and filled orders)."""
        for order in self.active_orders.values():
            if order.client_order_id == client_order_id:
                return order
        # Also check filled orders (for recently filled)
        for order in self.filled_orders:
            if order.client_order_id == client_order_id:
                return order
        return None

    @property
    def total_active_orders(self) -> int:
        """Get count of active orders."""
        return len(self.active_orders)

    @property
    def total_filled_orders(self) -> int:
        """Get count of filled orders."""
        return len(self.filled_orders)
