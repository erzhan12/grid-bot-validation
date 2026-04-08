"""Strategy runner that wraps GridEngine with execution context.

The runner is responsible for:
- Managing a GridEngine instance
- Routing events to the engine
- Executing returned intents (or logging in shadow mode)
- Tracking placed orders in-memory
- Managing Position risk calculations
"""

import logging
import math
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional, Callable

from gridcore import (
    GridEngine,
    GridConfig,
    InstrumentInfo,
    Position,
    PositionState,
    RiskConfig,
    TickerEvent,
    ExecutionEvent,
    OrderUpdateEvent,
    PlaceLimitIntent,
    CancelIntent,
    GridAnchorStore,
    DirectionType,
    calc_position_value,
    calc_margin_ratio,
    create_qty_calculator,
)

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor
from gridbot.notifier import Notifier


logger = logging.getLogger(__name__)

# Pre-built Decimal values for known Position multipliers (0.5, 1.0, 1.5, 2.0)
# to avoid float→str→Decimal conversion on every order.
_FLOAT_TO_DECIMAL = {
    0.5: Decimal("0.5"),
    1.0: Decimal("1"),
    1.5: Decimal("1.5"),
    2.0: Decimal("2"),
}


@dataclass
class TrackedOrder:
    """In-memory tracking of a placed order."""

    client_order_id: str
    order_id: Optional[str] = None
    intent: Optional[PlaceLimitIntent] = None
    status: str = "pending"  # 'pending', 'placed', 'filled', 'cancelled', 'failed'
    placed_ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def mark_placed(self, order_id: str) -> None:
        """Mark order as placed on exchange."""
        self.order_id = order_id
        self.status = "placed"

    def mark_filled(self) -> None:
        """Mark order as filled."""
        self.status = "filled"

    def mark_cancelled(self) -> None:
        """Mark order as cancelled."""
        self.status = "cancelled"

    def mark_failed(self) -> None:
        """Mark order as failed."""
        self.status = "failed"


class StrategyRunner:
    """Runs a single strategy instance.

    Wraps GridEngine with execution context, handling:
    - Event routing to engine
    - Intent execution
    - Order tracking
    - Position risk management

    Example:
        config = StrategyConfig(...)
        executor = IntentExecutor(rest_client)

        runner = StrategyRunner(
            strategy_config=config,
            executor=executor,
        )

        # Process events
        await runner.on_ticker(ticker_event)
        await runner.on_execution(execution_event)
    """

    def __init__(
        self,
        strategy_config: StrategyConfig,
        executor: IntentExecutor,
        instrument_info: Optional[InstrumentInfo] = None,
        anchor_store: Optional[GridAnchorStore] = None,
        on_intent_failed: Optional[Callable[[PlaceLimitIntent | CancelIntent, str], None]] = None,
        notifier: Optional[Notifier] = None,
    ):
        """Initialize strategy runner.

        Args:
            strategy_config: Strategy configuration.
            executor: Intent executor for API calls.
            instrument_info: Instrument info for qty rounding (None uses no rounding).
            anchor_store: Optional anchor store for grid persistence.
            on_intent_failed: Callback when intent execution fails (for retry queue).
            notifier: Alert notifier for same-order error Telegram alerts.
        """
        self._config = strategy_config
        self._executor = executor
        self._anchor_store = anchor_store
        self._on_intent_failed = on_intent_failed
        self._notifier = notifier

        # Qty computation
        self._instrument_info = instrument_info
        self._qty_calculator = create_qty_calculator(
            strategy_config.amount, instrument_info
        )
        self._wallet_balance: Decimal = Decimal("0")

        # Load anchor if available and config matches
        anchor_price = self._load_anchor()

        # Create GridEngine
        grid_config = GridConfig(
            grid_count=strategy_config.grid_count,
            grid_step=strategy_config.grid_step,
        )
        self._engine = GridEngine(
            symbol=strategy_config.symbol,
            tick_size=strategy_config.tick_size,
            config=grid_config,
            strat_id=strategy_config.strat_id,
            anchor_price=anchor_price,
        )

        # Create linked Position managers
        risk_config = RiskConfig(
            min_liq_ratio=strategy_config.min_liq_ratio,
            max_liq_ratio=strategy_config.max_liq_ratio,
            max_margin=strategy_config.max_margin,
            min_total_margin=strategy_config.min_total_margin,
        )
        self._long_position, self._short_position = Position.create_linked_pair(risk_config)

        # Order tracking with O(1) lookups on all access patterns:
        #   _tracked_orders: client_order_id → TrackedOrder (primary dict)
        #   _tracked_by_order_id: exchange order_id → TrackedOrder (secondary index)
        #   _placed_order_signatures: {(price, qty, side, reduce_only)} for O(1)
        #       duplicate detection (replaces O(n) iteration over all tracked orders)
        # All three are kept in sync via _index_as_placed() / _unindex_placed().
        self._tracked_orders: dict[str, TrackedOrder] = {}
        self._tracked_by_order_id: dict[str, TrackedOrder] = {}
        self._placed_order_signatures: set[tuple] = set()

        # Position state
        self._last_position_check: Optional[datetime] = None

        # Execution history for same-order detection (bbu2-style)
        # Keeps last 2 fully-filled executions per direction (long/short)
        # Matches bbu2: _check_same_orders splits buffer per side, takes [:2]
        # Only the most recent pair is compared; one clean fill clears error
        self._recent_executions_long: deque[dict] = deque(maxlen=2)
        self._recent_executions_short: deque[dict] = deque(maxlen=2)
        self._same_order_error: bool = False

    @property
    def strat_id(self) -> str:
        """Strategy identifier."""
        return self._config.strat_id

    @property
    def symbol(self) -> str:
        """Trading symbol."""
        return self._config.symbol

    @property
    def shadow_mode(self) -> bool:
        """Whether running in shadow mode."""
        return self._config.shadow_mode

    @property
    def engine(self) -> GridEngine:
        """Underlying GridEngine."""
        return self._engine

    @property
    def same_order_error(self) -> bool:
        """Whether a same-order error has been detected.

        This indicates that two different orders at the same price
        got filled, which is a grid duplication bug.
        """
        return self._same_order_error

    def reset_same_order_error(self) -> None:
        """Reset same-order error flag and clear execution history.

        Call this after handling the error (e.g., after WebSocket reconnect).
        """
        self._same_order_error = False
        self._recent_executions_long.clear()
        self._recent_executions_short.clear()

    def _load_anchor(self) -> Optional[float]:
        """Load anchor price if config matches."""
        if self._anchor_store is None:
            return None

        anchor_data = self._anchor_store.load(self._config.strat_id)
        if anchor_data is None:
            return None

        # Check if config matches
        if (
            anchor_data.get("grid_step") == self._config.grid_step
            and anchor_data.get("grid_count") == self._config.grid_count
        ):
            logger.info(
                f"{self.strat_id}: Loaded anchor price {anchor_data['anchor_price']} "
                f"(grid_step={anchor_data['grid_step']}, grid_count={anchor_data['grid_count']})"
            )
            return anchor_data["anchor_price"]
        else:
            logger.info(
                f"{self.strat_id}: Config changed, will build fresh grid "
                f"(saved: step={anchor_data.get('grid_step')}, count={anchor_data.get('grid_count')}; "
                f"current: step={self._config.grid_step}, count={self._config.grid_count})"
            )
            return None

    def _save_anchor(self) -> None:
        """Save current anchor price."""
        if self._anchor_store is None:
            return

        anchor_price = self._engine.get_anchor_price()
        if anchor_price is not None:
            self._anchor_store.save(
                strat_id=self._config.strat_id,
                anchor_price=anchor_price,
                grid_step=self._config.grid_step,
                grid_count=self._config.grid_count,
            )

    def get_limit_orders(self) -> dict[str, list[dict]]:
        """Get current limit orders in format expected by GridEngine.

        Returns dict with 'long' and 'short' keys, each containing list of order dicts.
        """
        long_orders = []
        short_orders = []

        for tracked in self._tracked_orders.values():
            if tracked.status not in ("placed",):
                continue
            if tracked.intent is None:
                continue

            order_dict = {
                "orderId": tracked.order_id,
                "orderLinkId": tracked.client_order_id,
                "price": str(tracked.intent.price),
                "qty": str(tracked.intent.qty),
                "side": tracked.intent.side,
                "reduceOnly": tracked.intent.reduce_only,
            }

            if tracked.intent.direction == DirectionType.LONG:
                long_orders.append(order_dict)
            else:
                short_orders.append(order_dict)

        return {"long": long_orders, "short": short_orders}

    async def on_ticker(self, event: TickerEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """Process ticker event.

        Args:
            event: Ticker event with current price.

        Returns:
            List of intents generated (for logging/tracking).

        Raises:
            Exception: Re-raised after logging so orchestrator can handle notification.
        """
        try:
            # Always pass ticker to engine (keeps last_close fresh for risk calcs)
            limit_orders = self.get_limit_orders()
            intents = self._engine.on_event(event, limit_orders)

            # Only execute intents if no same-order error
            if self._same_order_error:
                logger.warning(
                    f"{self.strat_id}: Same-order error active, skipping order placement"
                )
                return intents

            if intents:
                await self._execute_intents(intents)

                # Save anchor after grid changes
                if self._anchor_store and len(self._engine.grid.grid) > 1:
                    self._save_anchor()

            return intents
        except Exception as e:
            logger.error(f"{self.strat_id}: Error in on_ticker: {e}", exc_info=True)
            raise

    async def on_execution(self, event: ExecutionEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """Process execution (fill) event.

        Args:
            event: Execution event with fill details.

        Returns:
            List of intents generated (for logging/tracking).

        Raises:
            Exception: Re-raised after logging so orchestrator can handle notification.
        """
        try:
            # Update tracked order status
            tracked = self._find_tracked_order(event.order_link_id, event.order_id)
            if tracked:
                self._unindex_placed(tracked)
                tracked.mark_filled()
                logger.info(
                    f"{self.strat_id}: Order filled: {event.symbol} {event.side} "
                    f"qty={event.qty} price={event.price}"
                )

            # Check for same-order error (bbu2-style safety check)
            self._check_same_orders(event)

            # Pass to engine (update grid state regardless of error)
            intents = self._engine.on_event(event)

            # Only execute intents if no same-order error
            if intents and not self._same_order_error:
                await self._execute_intents(intents)

            return intents
        except Exception as e:
            logger.error(f"{self.strat_id}: Error in on_execution: {e}", exc_info=True)
            raise

    async def on_order_update(self, event: OrderUpdateEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """Process order update event.

        Args:
            event: Order update event with status change.

        Returns:
            List of intents generated (for logging/tracking).

        Raises:
            Exception: Re-raised after logging so orchestrator can handle notification.
        """
        try:
            # Update tracked order status
            tracked = self._find_tracked_order(event.order_link_id, event.order_id)
            if tracked:
                if event.status in ("Filled",):
                    self._unindex_placed(tracked)
                    tracked.mark_filled()
                elif event.status in ("Cancelled", "Rejected"):
                    self._unindex_placed(tracked)
                    tracked.mark_cancelled()
                elif event.status in ("New", "PartiallyFilled"):
                    if tracked.order_id is None:
                        tracked.mark_placed(event.order_id)
                        self._index_as_placed(tracked)

            # Pass to engine (update state regardless of error)
            intents = self._engine.on_event(event)

            # Only execute intents if no same-order error
            if intents and not self._same_order_error:
                await self._execute_intents(intents)

            return intents
        except Exception as e:
            logger.error(f"{self.strat_id}: Error in on_order_update: {e}", exc_info=True)
            raise

    async def on_position_update(
        self,
        long_position: Optional[dict],
        short_position: Optional[dict],
        wallet_balance: float,
        last_close: float,
    ) -> None:
        """Update position state and recalculate multipliers.

        Args:
            long_position: Long position data from exchange.
            short_position: Short position data from exchange.
            wallet_balance: Current wallet balance.
            last_close: Current price.

        Raises:
            Exception: Re-raised after logging so orchestrator can handle notification.
        """
        try:
            # Store wallet balance for qty computation
            self._wallet_balance = Decimal(str(wallet_balance))

            # Build PositionState objects
            long_state = self._build_position_state(long_position, wallet_balance, DirectionType.LONG)
            short_state = self._build_position_state(short_position, wallet_balance, DirectionType.SHORT)

            # Update position sizes (used by _is_good_to_place)
            self._long_position.size = long_state.size if long_state else Decimal('0')
            self._short_position.size = short_state.size if short_state else Decimal('0')

            # Convert Decimal sizes to float: position_ratio is stored as float (position.py:98)
            # and mixing Decimal/float in division raises TypeError
            long_size = float(long_state.size) if long_state else 0.0
            short_size = float(short_state.size) if short_state else 0.0

            if short_size > 0:
                position_ratio = long_size / short_size
            elif long_size > 0:
                position_ratio = float("inf")
            else:
                position_ratio = 1.0

            # Update position managers
            self._long_position.position_ratio = position_ratio
            self._short_position.position_ratio = position_ratio

            # Reset both positions once before calculating (bbu2 pattern)
            self._long_position.reset_amount_multiplier()
            self._short_position.reset_amount_multiplier()

            # Calculate multipliers per direction.
            # Long is calculated first; cross-position effects on short are preserved
            # because short's reset already happened above.
            if long_state:
                opposite = short_state or PositionState(direction=DirectionType.SHORT)
                self._long_position.calculate_amount_multiplier(
                    long_state, opposite, last_close
                )

            if short_state:
                opposite = long_state or PositionState(direction=DirectionType.LONG)
                self._short_position.calculate_amount_multiplier(
                    short_state, opposite, last_close
                )

            self._last_position_check = datetime.now(UTC)

            long_mult = self._long_position.get_amount_multiplier()
            short_mult = self._short_position.get_amount_multiplier()
            logger.info(
                f"{self.strat_id}: Position update - ratio={position_ratio:.2f}, "
                f"long_mult=Buy:{long_mult['Buy']:.2f}/Sell:{long_mult['Sell']:.2f}, "
                f"short_mult=Buy:{short_mult['Buy']:.2f}/Sell:{short_mult['Sell']:.2f}"
            )
        except Exception as e:
            logger.error(f"{self.strat_id}: Error in on_position_update: {e}", exc_info=True)
            raise

    def get_amount_multiplier(self, direction: str, side: str) -> float:
        """Get amount multiplier for a given direction and side.

        Matches bbu2 pattern: position[direction].get_amount_multiplier()[side]

        Args:
            direction: 'long' or 'short'
            side: 'Buy' or 'Sell'

        Returns:
            Multiplier value (default 1.0).
        """
        if direction == DirectionType.LONG:
            return self._long_position.get_amount_multiplier()[side]
        else:
            return self._short_position.get_amount_multiplier()[side]

    def _resolve_qty(self, intent: PlaceLimitIntent) -> PlaceLimitIntent:
        """Resolve qty=0 intent to actual order quantity.

        Composes base qty (from amount config) with risk multiplier,
        then re-rounds to qty_step so the result is exchange-valid.

        Returns a new intent with resolved qty, or the original if qty > 0.
        """
        if intent.qty > 0:
            return intent

        base_qty = self._qty_calculator(intent, self._wallet_balance)
        mult_float = self.get_amount_multiplier(intent.direction, intent.side)
        if not math.isfinite(mult_float):
            logger.error(
                f"{self.strat_id}: Invalid multiplier {mult_float} for "
                f"{intent.side} {intent.direction}"
            )
            return replace(intent, qty=Decimal("0"))
        multiplier = _FLOAT_TO_DECIMAL.get(mult_float, Decimal(str(mult_float)))
        resolved_qty = base_qty * multiplier

        # Re-round after multiplier to ensure qty aligns with exchange qty_step
        if self._instrument_info and resolved_qty > 0:
            resolved_qty = self._instrument_info.round_qty(resolved_qty)
            if resolved_qty < self._instrument_info.min_qty:
                logger.warning(
                    f"{self.strat_id}: Resolved qty {resolved_qty} below min_qty "
                    f"{self._instrument_info.min_qty} for {intent.side} {intent.direction} "
                    f"at {intent.price}"
                )
                return replace(intent, qty=Decimal("0"))
            if resolved_qty > self._instrument_info.max_qty:
                logger.warning(
                    f"{self.strat_id}: Resolved qty {resolved_qty} exceeds max_qty "
                    f"{self._instrument_info.max_qty} for {intent.side} {intent.direction} "
                    f"at {intent.price}, clamping"
                )
                resolved_qty = self._instrument_info.max_qty

        if resolved_qty <= 0:
            log_level = logging.DEBUG if self._wallet_balance == 0 else logging.WARNING
            logger.log(
                log_level,
                f"{self.strat_id}: Resolved qty=0 for {intent.side} {intent.direction} "
                f"at {intent.price} (base={base_qty}, mult={multiplier}, "
                f"wallet={self._wallet_balance})",
            )

        return replace(intent, qty=resolved_qty)

    def _build_position_state(
        self, position_data: Optional[dict], wallet_balance: float, direction: str = DirectionType.LONG
    ) -> Optional[PositionState]:
        """Build PositionState from exchange position data."""
        if position_data is None:
            return None

        size = float(position_data.get("size", 0))
        if size == 0:
            return None

        entry_price = float(position_data.get("avgPrice", 0) or position_data.get("entryPrice", 0))
        liq_price = float(position_data.get("liqPrice", 0) or 0)
        unrealized_pnl = float(position_data.get("unrealisedPnl", 0) or 0)

        # Calculate margin
        size_d = Decimal(str(size))
        entry_d = Decimal(str(entry_price))
        position_value = calc_position_value(size_d, entry_d) if entry_price > 0 else Decimal("0")
        margin = calc_margin_ratio(position_value, Decimal(str(wallet_balance)))

        return PositionState(
            direction=direction,
            size=size_d,
            entry_price=entry_d if entry_price else None,
            unrealized_pnl=Decimal(str(unrealized_pnl)),
            margin=margin,
            liquidation_price=Decimal(str(liq_price)),
            position_value=position_value,
        )

    async def _execute_intents(self, intents: list[PlaceLimitIntent | CancelIntent]) -> None:
        """Execute a list of intents."""
        if self._executor.auth_cooldown:
            logger.debug(f"{self.strat_id}: Auth cooldown active, skipping {len(intents)} intents")
            return

        for intent in intents:
            if self._executor.auth_cooldown:
                logger.debug(f"{self.strat_id}: Auth cooldown activated mid-batch, skipping remaining intents")
                break
            if isinstance(intent, PlaceLimitIntent):
                await self._execute_place_intent(intent)
            elif isinstance(intent, CancelIntent):
                await self._execute_cancel_intent(intent)

    @staticmethod
    def _derive_direction_from_order(side: str, reduce_only: bool) -> Optional[DirectionType]:
        """Derive grid direction from Bybit order side and reduceOnly flag.

        Returns None for unrecognized side values.
        """
        if side == "Buy":
            return DirectionType.SHORT if reduce_only else DirectionType.LONG
        elif side == "Sell":
            return DirectionType.LONG if reduce_only else DirectionType.SHORT
        return None

    @staticmethod
    def _order_signature(intent: PlaceLimitIntent) -> tuple:
        """Build signature tuple for duplicate detection."""
        return (intent.price, intent.qty, intent.side, intent.reduce_only)

    def _index_as_placed(self, tracked: TrackedOrder) -> None:
        """Add a tracked order to secondary indexes (order_id + signature)."""
        if tracked.order_id:
            self._tracked_by_order_id[tracked.order_id] = tracked
        if tracked.intent:
            self._placed_order_signatures.add(self._order_signature(tracked.intent))

    def _rebuild_signature_set(self) -> None:
        """Rebuild _placed_order_signatures from _tracked_orders (defensive recovery)."""
        self._placed_order_signatures = {
            self._order_signature(t.intent)
            for t in self._tracked_orders.values()
            if t.status == "placed" and t.intent
        }

    def _unindex_placed(self, tracked: TrackedOrder) -> None:
        """Remove a tracked order from secondary indexes."""
        if tracked.order_id:
            self._tracked_by_order_id.pop(tracked.order_id, None)
        if tracked.intent:
            sig = self._order_signature(tracked.intent)
            if sig not in self._placed_order_signatures:
                logger.error(
                    f"{self.strat_id}: Signature not found when unindexing "
                    f"order {tracked.client_order_id} — rebuilding signature set"
                )
                self._rebuild_signature_set()
                return
            self._placed_order_signatures.discard(sig)

    def _find_tracked_order(
        self, order_link_id: Optional[str], order_id: Optional[str]
    ) -> Optional[TrackedOrder]:
        """Find a tracked order by client_order_id or order_id. Both O(1).

        Tries _tracked_orders (keyed by client_order_id) first, then
        _tracked_by_order_id (secondary index on exchange order_id).
        """
        if order_link_id and order_link_id in self._tracked_orders:
            return self._tracked_orders[order_link_id]
        if order_id:
            return self._tracked_by_order_id.get(order_id)
        return None

    def _is_good_to_place(self, intent: PlaceLimitIntent) -> bool:
        """Check if order can be placed (exact-duplicate + reduce-only checks).

        1. Exact-duplicate check: if a placed order with same (price, qty, side,
           reduce_only) already exists, reject. Matches bbu2 bybit_api_usdt.py:296-300.
        2. For reduce_only orders, checks that total reduce-only qty on the book
           + new order qty doesn't exceed position size.

        Reference: bbu_reference/bbu2-master/bybit_api_usdt.py:295-313
        """
        # Exact-duplicate check (bbu2-style) — O(1) via signature set
        sig = self._order_signature(intent)
        if sig in self._placed_order_signatures:
            logger.debug(
                f"{self.strat_id}: Rejecting exact duplicate order at "
                f"price={intent.price} qty={intent.qty} side={intent.side}"
            )
            return False

        if not intent.reduce_only:
            return True

        direction = intent.direction
        position_size = (self._long_position.size if direction == DirectionType.LONG
                         else self._short_position.size)

        if position_size == Decimal('0'):
            logger.debug(
                f"{self.strat_id}: Rejecting reduce-only order at {intent.price} - "
                f"position size is zero (position update may not have arrived yet)"
            )
            return False

        close_side_map = {DirectionType.LONG: 'Sell', DirectionType.SHORT: 'Buy'}
        close_side = close_side_map[direction]

        total_reduce_qty = intent.qty
        for tracked in self._tracked_orders.values():
            if tracked.status != "placed":
                continue
            if tracked.intent is None:
                continue
            if (tracked.intent.reduce_only
                    and tracked.intent.side == close_side
                    and tracked.intent.direction == direction):
                total_reduce_qty += tracked.intent.qty

        return position_size > total_reduce_qty

    async def _execute_place_intent(self, intent: PlaceLimitIntent) -> None:
        """Execute a place order intent."""
        # Resolve qty (engine emits qty=0, we fill it in)
        intent = self._resolve_qty(intent)
        if intent.qty <= 0:
            logger.debug(f"{self.strat_id}: Skipping order with qty<=0 at {intent.price}")
            return

        # Check duplicate and reduce-only constraints (bbu2 _is_good_to_place)
        if not self._is_good_to_place(intent):
            logger.debug(
                f"{self.strat_id}: Skipping order at {intent.price} - "
                f"rejected by _is_good_to_place"
            )
            return

        # Check for duplicate
        if intent.client_order_id in self._tracked_orders:
            tracked = self._tracked_orders[intent.client_order_id]
            if tracked.status in ("pending", "placed"):
                logger.debug(
                    f"{self.strat_id}: Skipping duplicate order {intent.client_order_id}"
                )
                return

        # Track order
        tracked = TrackedOrder(
            client_order_id=intent.client_order_id,
            intent=intent,
            status="pending",
        )
        self._tracked_orders[intent.client_order_id] = tracked

        # Execute
        result = self._executor.execute_place(intent)

        if result.success:
            tracked.mark_placed(result.order_id)
            self._index_as_placed(tracked)
        else:
            tracked.mark_failed()
            if self._on_intent_failed:
                self._on_intent_failed(intent, result.error)

    async def _execute_cancel_intent(self, intent: CancelIntent) -> None:
        """Execute a cancel order intent."""
        result = self._executor.execute_cancel(intent)

        # Update tracked order if we have it (O(1) via secondary index)
        tracked = self._tracked_by_order_id.get(intent.order_id)
        if tracked and result.success:
            self._unindex_placed(tracked)
            tracked.mark_cancelled()

        if not result.success and self._on_intent_failed:
            self._on_intent_failed(intent, result.error)

    def inject_open_orders(self, orders: list[dict]) -> None:
        """Inject open orders from exchange (for reconciliation on startup).

        Each order is stored in _tracked_orders keyed by client_order_id
        (orderLinkId if present, otherwise orderId). Orders without orderId
        are skipped. The _tracked_by_order_id secondary index provides O(1)
        lookup by exchange order_id.

        Args:
            orders: List of order dicts from exchange.
        """
        injected = 0
        for order in orders:
            order_id = order.get("orderId", "")
            if not order_id:
                continue

            order_link_id = order.get("orderLinkId", "")

            # Build a PlaceLimitIntent for duplicate/reduce-only checking.
            # Derive direction from side+reduceOnly (Bybit orders don't carry direction).
            # Buy+not reduce = opening long, Buy+reduce = closing short,
            # Sell+not reduce = opening short, Sell+reduce = closing long.
            price = order.get("price")
            qty = order.get("qty")
            side = order.get("side")
            reduce_only = order.get("reduceOnly", False)

            if not (price and qty and side):
                logger.warning(
                    f"{self.strat_id}: Skipping injected order {order_id} "
                    f"with missing price/qty/side"
                )
                continue

            direction = self._derive_direction_from_order(side, reduce_only)
            if direction is None:
                logger.warning(
                    f"{self.strat_id}: Skipping injected order {order_id} "
                    f"with unrecognized side={side!r}"
                )
                continue

            try:
                dec_price = Decimal(str(price))
                dec_qty = Decimal(str(qty))
            except Exception:
                logger.warning(
                    f"{self.strat_id}: Skipping injected order {order_id} "
                    f"with invalid price={price!r} or qty={qty!r}"
                )
                continue

            intent = PlaceLimitIntent.create(
                symbol=order.get("symbol", self._config.symbol),
                side=side,
                price=dec_price,
                qty=dec_qty,
                grid_level=0,
                direction=direction,
                reduce_only=reduce_only,
            )

            # Prefer orderLinkId as key for backward compatibility: old orders
            # (placed before this change) still have orderLinkId on the exchange,
            # so events for those orders arrive with order_link_id set. New orders
            # won't have one, so we fall back to orderId.
            client_id = order_link_id or order_id
            tracked = TrackedOrder(
                client_order_id=client_id,
                order_id=order_id,
                intent=intent,
                status="placed",
            )
            self._tracked_orders[client_id] = tracked
            self._index_as_placed(tracked)
            injected += 1

        logger.info(f"{self.strat_id}: Injected {injected} open orders")

    def mark_order_cancelled_by_order_id(self, order_id: str) -> None:
        """Mark a tracked order as cancelled by its exchange order_id.

        Used by reconciler when exchange state diverges from in-memory state.
        """
        tracked = self._tracked_by_order_id.get(order_id)
        if tracked:
            self._unindex_placed(tracked)
            tracked.mark_cancelled()

    def get_placed_order_ids(self) -> set[str]:
        """Get exchange order_ids of all placed (active) orders.

        Used by reconciler to compare in-memory state with exchange state.
        """
        return {
            oid for oid, t in self._tracked_by_order_id.items()
            if t.status == "placed"
        }

    def get_tracked_order_count(self) -> dict[str, int]:
        """Get count of tracked orders by status."""
        counts = {"pending": 0, "placed": 0, "filled": 0, "cancelled": 0, "failed": 0}
        for tracked in self._tracked_orders.values():
            if tracked.status in counts:
                counts[tracked.status] += 1
        return counts

    def _check_same_orders(self, event: ExecutionEvent) -> None:
        """Check for duplicate orders at same price (bbu2-style safety check).

        Detects if two DIFFERENT orders at the SAME price got filled,
        which indicates a grid duplication bug.

        Matches bbu2 _check_same_orders: ALWAYS checks BOTH buffers
        (long first, then short). This prevents an unrelated fill on one
        side from clearing an error detected on the other side.

        Args:
            event: Execution event to check.
        """
        # Only track fully filled orders (leavesQty == 0), matching bbu2
        # handle_execution filter: x['leavesQty'] == '0'
        # Partial fills would dilute the buffer without adding detection value
        if event.leaves_qty != 0:
            return

        # Determine direction based on side and whether it's closing position
        # closed_size != 0 means this execution closed (reduced) a position
        # Uses closedSize (not closedPnl) to avoid break-even edge case
        # Buy + not closing = opening long → long buffer
        # Sell + closing = closing long → long buffer
        # Buy + closing = closing short → short buffer
        # Sell + not closing = opening short → short buffer
        is_closing = event.closed_size != 0

        if (event.side == "Buy" and not is_closing) or (event.side == "Sell" and is_closing):
            buffer = self._recent_executions_long
        else:
            buffer = self._recent_executions_short

        # Add to buffer (newest first)
        exec_record = {
            "order_id": event.order_id,
            "price": event.price,
            "side": event.side,
            "exchange_ts": event.exchange_ts,
        }
        buffer.appendleft(exec_record)

        # Check BOTH buffers (matches bbu2: check long first, short second)
        # _check_same_orders_side resets the flag before evaluating, so we
        # must check both to avoid one side clearing the other's error.
        self._check_same_orders_side(self._recent_executions_long)
        if self._same_order_error:
            return
        self._check_same_orders_side(self._recent_executions_short)

    def _check_same_orders_side(self, executions: deque) -> None:
        """Check execution buffer for same-price duplicates.

        Re-evaluates on every call (bbu2-style): resets error flag first,
        then checks. Error auto-clears when new fills push the problematic
        pair out of the buffer.

        Compares consecutive executions. If same price and side but different
        order_id, this indicates a duplicate order was placed at the same
        price level - a grid bug.

        Args:
            executions: Deque of recent execution records for one direction.
        """
        # Reset before re-evaluation (matches bbu2 _check_same_orders_side line 159)
        self._same_order_error = False

        if len(executions) < 2:
            return

        exec_list = list(executions)
        for current, previous in zip(exec_list, exec_list[1:]):
            if current["price"] == previous["price"] and current["side"] == previous["side"]:
                if current["order_id"] == previous["order_id"]:
                    # Same order ID (partial fills) - OK
                    return
                # Different order IDs at same price = DUPLICATE ERROR
                logger.error(
                    f"{self.strat_id}: SAME ORDER ERROR - Two different orders filled "
                    f"at same price {current['price']} side={current['side']} "
                    f"(order_ids: {current['order_id']}, {previous['order_id']})"
                )
                self._same_order_error = True
                if self._notifier:
                    self._notifier.alert(
                        f"SAME ORDER ERROR: {self.strat_id} - Two different orders filled "
                        f"at price {current['price']} side={current['side']}. "
                        f"Order placement BLOCKED.",
                        error_key=f"same_order_{self.strat_id}",
                    )
                return
