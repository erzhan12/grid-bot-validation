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
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from gridbot.writers.grid_state_writer import GridStateWriter
    from bybit_adapter.rest_client import BybitRestClient

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
    extract_client_order_prefix,
    GridStateStore,
    DirectionType,
    calc_position_value,
    calc_margin_ratio,
    create_qty_calculator,
    apply_early_imbalance,
)

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, is_truncate_error
from gridbot.notifier import Notifier
from gridbot.order_link_id import make_order_link_id
from gridbot.truncate_breaker import TruncateBreaker


logger = logging.getLogger(__name__)

# Pre-built Decimal values for known Position multipliers (0.5, 1.0, 1.5, 2.0)
# to avoid float→str→Decimal conversion on every order.
_FLOAT_TO_DECIMAL = {
    0.5: Decimal("0.5"),
    1.0: Decimal("1"),
    1.5: Decimal("1.5"),
    2.0: Decimal("2"),
}

# Time window for SAME ORDER detection: only flag pairs whose exchange_ts
# are within this many seconds. Concurrent grid duplicates (the bug bbu2
# was designed to catch) fill within milliseconds; sequential grid-walk
# replacements at the same price are minutes-to-hours apart and must not
# trigger. See docs/features/0025_PLAN.md.
_SAME_ORDER_TIME_WINDOW_SEC = 5.0

# Dedup TTL for SAME ORDER trigger pairs: once an order_id pair has been
# adjudicated (or is in flight), suppress retriggers on the same pair for
# this window. Sized to comfortably exceed the 3 h 24 min retrigger gap
# observed in the 2026-05-09/10 incident; short enough not to outlive a
# normal operator shift. See docs/features/0031_PLAN.md.
_SAME_ORDER_DEDUP_TTL_SEC = 21600.0  # 6 hours

# REST confirmation window for SAME ORDER adjudication. The detector's trigger
# window is small, but pad the REST slice to tolerate exchange timestamp skew
# while still making pagination completeness meaningful.
_SAME_ORDER_REST_WINDOW_PADDING_SEC = 60.0
_SAME_ORDER_REST_MAX_PAGES = 10

# Throttle window for the `Same-order error active, skipping order placement`
# WARNING in `on_ticker`. While the soft-block stays latched, every ticker
# (~100-400 ms) would otherwise emit a fresh WARNING, flooding the log. We
# emit the first occurrence loudly, suppress within this window, and re-emit
# at most once per window as a heartbeat with `(suppressed N since last)`.
# See docs/features/0046_PLAN.md and issue #94.
_SAME_ORDER_WARN_THROTTLE_SEC = 60.0

# Feature 0064 — emit a WARNING once a direction has had this many consecutive
# WS position-size mismatches while dirty (REST baseline held each time). Signals
# a WS feed stuck beyond the normal recovery window. Re-fires every multiple.
_DIRTY_WS_MISMATCH_ALERT_THRESHOLD = 10


@dataclass
class _SameOrderDedupEntry:
    """SAME ORDER pair adjudication record kept in `_same_order_dedup_cache`.

    Stores all SAME ORDER pair adjudications, not only phantom ones — the
    name is deliberately neutral so future readers do not "optimise" by
    skipping non-phantom entries (the regression that REAL_DUPLICATE-aware
    handling exists to prevent).
    """

    first_seen_ts: datetime
    last_seen_ts: datetime
    verdict: str  # "WS_GLITCH_SUSPECTED" | "REAL_DUPLICATE" | "UNKNOWN"


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
        runner.on_ticker(ticker_event)
        runner.on_execution(execution_event)
    """

    # Sentinel default for ``account_id``: avoids forcing every existing
    # test/fixture (~40 sites) to pass a real UUID while still letting the
    # constructor reject the dummy if a DB writer is actually wired —
    # otherwise snapshots would silently land under the zero UUID and be
    # invisible to replay's real account scope (feature 0047).
    _ACCOUNT_ID_DEFAULT = "00000000-0000-0000-0000-000000000000"

    def __init__(
        self,
        strategy_config: StrategyConfig,
        executor: IntentExecutor,
        account_id: str = _ACCOUNT_ID_DEFAULT,
        instrument_info: Optional[InstrumentInfo] = None,
        state_store: Optional[GridStateStore] = None,
        grid_state_writer: Optional["GridStateWriter"] = None,
        on_intent_failed: Optional[Callable[[PlaceLimitIntent | CancelIntent, str], None]] = None,
        on_unknown_order: Optional[Callable[[str], None]] = None,
        notifier: Optional[Notifier] = None,
        on_retry_cancel_for_prefix: Optional[Callable[[str], int]] = None,
        rest_client: Optional["BybitRestClient"] = None,
        truncate_breaker: Optional[TruncateBreaker] = None,
        on_truncate_breaker_tripped: Optional[Callable[[str, str], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        """Initialize strategy runner.

        Args:
            strategy_config: Strategy configuration.
            executor: Intent executor for API calls.
            account_id: UUID5-derived account identifier; matches the value
                ``orchestrator.py:1156-1162`` computes from the account name.
                Used by the DB grid-state writer (feature 0047) so snapshots
                FK-match ``runs.account_id``.
            instrument_info: Instrument info for qty rounding (None uses no rounding).
            state_store: Optional grid state store for persistence.
            grid_state_writer: Optional DB writer for ``grid_state_snapshots``
                (feature 0047). When provided, ``_on_grid_change`` writes a
                second snapshot path in parallel with ``state_store``.
            on_intent_failed: Callback when intent execution fails (for retry queue).
            on_unknown_order: Callback when WS reports a New order we don't track
                (for fast-tracking the next order-sync sweep).
            notifier: Alert notifier for same-order error Telegram alerts.
            on_retry_cancel_for_prefix: Optional callback to cancel queued
                placement retries after reconcile proves the order was accepted.
            rest_client: Optional Bybit REST client for the dirty-mirror
                position refresh (feature 0064). When None (unit tests/dry-run)
                the dirty path logs a WARNING and falls back to the in-memory
                mirror; the circuit-breaker still bounds the storm.
            truncate_breaker: Optional 110017 circuit-breaker (feature 0064).
                When None, one is constructed from ``strategy_config``'s
                ``truncate_breaker_*`` fields (owned per-runner).
            on_truncate_breaker_tripped: Optional callback fired once per breaker
                trip (args: strat_id, direction) — wired to the orchestrator's
                forced position+order reconcile.
            clock: Monotonic clock for breaker windows / dirty-refresh throttle.
                Defaults to ``time.monotonic``; injectable for tests.
        """
        # 0047: if a DB writer is wired, account_id MUST be explicitly set —
        # the dummy default would FK-mismatch with replay's account scope
        # (and `runs.account_id` does not catch this because the writer's
        # FK is to `runs.run_id`). Orchestrator passes the real UUID5; any
        # other caller wiring the writer must do the same.
        if (
            grid_state_writer is not None
            and account_id == self._ACCOUNT_ID_DEFAULT
        ):
            raise ValueError(
                "StrategyRunner.account_id must be set explicitly when "
                "grid_state_writer is provided; got the placeholder default. "
                "Orchestrator derives it via grid_db.identity.account_id_for("
                "account_name) (uuid5(NAMESPACE, 'account:<name>'))."
            )

        self._config = strategy_config
        self._executor = executor
        self._account_id = account_id
        self._state_store = state_store
        self._grid_state_writer = grid_state_writer
        self._on_intent_failed = on_intent_failed
        self._on_unknown_order = on_unknown_order
        self._notifier = notifier
        self._on_retry_cancel_for_prefix = on_retry_cancel_for_prefix

        # Feature 0064 — 110017 retry-storm self-heal + circuit-breaker (#149).
        self._rest_client = rest_client
        self._clock = clock or time.monotonic
        self._on_truncate_breaker_tripped = on_truncate_breaker_tripped
        self._truncate_breaker = truncate_breaker or TruncateBreaker(
            max_consecutive=strategy_config.truncate_breaker_max_consecutive,
            window_seconds=strategy_config.truncate_breaker_window_seconds,
            cooldown_seconds=strategy_config.truncate_breaker_cooldown_seconds,
        )
        # Per-direction divergence flag: set on the first 110017 for a
        # direction; while True the mirror size is REST-refreshed before the
        # guard. Cleared by a positive health signal (successful place, forced
        # reconcile, or a WS size that matches the last REST read).
        self._position_dirty: dict[str, bool] = {
            DirectionType.LONG: False,
            DirectionType.SHORT: False,
        }
        # REST refresh throttle per direction. None = never refreshed → the
        # first dirty refresh bypasses the throttle (clock-independent: an init
        # of 0.0 was brittle under fake clocks where now=0).
        self._last_dirty_rest_at: dict[str, Optional[float]] = {
            DirectionType.LONG: None,
            DirectionType.SHORT: None,
        }
        # Size written by the most recent REST refresh per direction. None = no
        # baseline yet; the WS dirty-gate only activates once a baseline exists.
        self._last_rest_position_size: dict[str, Optional[Decimal]] = {
            DirectionType.LONG: None,
            DirectionType.SHORT: None,
        }
        # Monotonic count of breaker trips that fired a forced reconcile.
        self._truncate_breaker_reconcile_count: int = 0
        # Observability (review v3): monotonic count of dirty REST refreshes that
        # failed (get_positions raised / unparseable size) — surfaced by the
        # health sweep so persistent REST issues blocking self-heal are visible.
        self._dirty_rest_refresh_failure_count: int = 0
        # Per-direction streak of consecutive WS size mismatches while dirty
        # (reset on a match / episode clear). Drives a threshold WARNING.
        self._dirty_ws_mismatch_streak: dict[str, int] = {
            DirectionType.LONG: 0,
            DirectionType.SHORT: 0,
        }

        # Qty computation
        self._instrument_info = instrument_info
        self._qty_calculator = create_qty_calculator(
            strategy_config.amount, instrument_info
        )
        self._wallet_balance: Decimal = Decimal("0")

        # Restore full grid if available and config matches
        restored_grid = self._load_grid_state()

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
            restored_grid=restored_grid,
            on_grid_change=self._on_grid_change,
        )

        # Create linked Position managers
        risk_config = RiskConfig(
            min_liq_ratio=strategy_config.min_liq_ratio,
            max_liq_ratio=strategy_config.max_liq_ratio,
            max_margin=strategy_config.max_margin,
            min_total_margin=strategy_config.min_total_margin,
            increase_same_position_on_low_margin=(
                strategy_config.increase_same_position_on_low_margin
            ),
        )
        self._long_position, self._short_position = Position.create_linked_pair(risk_config)

        # Order tracking: client_order_id → TrackedOrder.
        # Lookups by exchange order_id use linear scan (~20-40 orders).
        self._tracked_orders: dict[str, TrackedOrder] = {}

        # Position state
        self._last_position_check: Optional[datetime] = None

        # Execution history for same-order detection (bbu2-style)
        # Keeps last 2 fully-filled executions per direction (long/short)
        # Matches bbu2: _check_same_orders splits buffer per side, takes [:2]
        # Only the most recent pair is compared; one clean fill clears error
        self._recent_executions_long: deque[dict] = deque(maxlen=2)
        self._recent_executions_short: deque[dict] = deque(maxlen=2)
        self._same_order_error: bool = False
        # Throttle state for the `Same-order error active` WARNING in
        # `on_ticker`. `_same_order_warn_last_ts` is the timestamp of the
        # most recent emitted WARNING for the current latched block; None
        # while not blocked or right after a clear. `_same_order_warn_suppressed`
        # counts WARNINGs suppressed since the last emission within the
        # current block. See feature 0046 / issue #94.
        self._same_order_warn_last_ts: Optional[datetime] = None
        self._same_order_warn_suppressed: int = 0
        # Verdict-aware dedup of SAME ORDER trigger pairs. Keyed by
        # frozenset of the two exchange order_ids in the pair. Used both to
        # rate-limit the REST cross-check and to suppress retriggers within
        # _SAME_ORDER_DEDUP_TTL_SEC (with verdict-specific handling of the
        # soft-block flag — see _check_same_orders_side and
        # _diagnostic_rest_check_executions). Lazily expired on lookup.
        self._same_order_dedup_cache: dict[frozenset[str], _SameOrderDedupEntry] = {}
        # One-shot flag: set inside `_diagnostic_rest_check_executions` when
        # the current ExecutionEvent has been REST-classified as a phantom
        # (verdict=WS_GLITCH_SUSPECTED). `on_execution` resets it at entry
        # and, when set, returns BEFORE feeding the event to
        # `self._engine.on_event(...)` and BEFORE `_execute_intents(...)`.
        # Reasoning: REST has authoritatively confirmed this fill did not
        # happen on the exchange, so the engine must not mutate grid state
        # as if it had — otherwise downstream ticks/executions re-derive
        # intents from a position view that diverges from reality.
        self._drop_phantom_event_for_current_call: bool = False

    @property
    def strat_id(self) -> str:
        """Strategy identifier."""
        return self._config.strat_id

    @property
    def truncate_breaker_reconcile_count(self) -> int:
        """Number of 110017 breaker trips that fired a forced reconcile.

        Monotonic for the runner lifetime (resets only on process restart).
        Read by ``Orchestrator._health_check_once`` for an operator/metrics
        signal without per-occurrence ERROR spam.
        """
        return self._truncate_breaker_reconcile_count

    @property
    def dirty_rest_refresh_failure_count(self) -> int:
        """Number of dirty REST refreshes that failed (review v3 #1).

        Incremented when ``get_positions`` raises or returns an unparseable
        size during a dirty refresh. Monotonic for the runner lifetime; surfaced
        by the health sweep so a persistent REST outage that blocks self-heal is
        visible without per-occurrence ERROR spam.
        """
        return self._dirty_rest_refresh_failure_count

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

    def reset_same_order_error(self, emit_recovery_info: bool = True) -> None:
        """Reset same-order error flag and clear execution history.

        Call this after handling the error (e.g., after WebSocket reconnect).

        Single owner of "clear flag + clear execution buffers + clear throttle
        state". On a True→False transition, optionally emits the recovery INFO
        summarising the suppressed-WARNING count (feature 0046).

        Args:
            emit_recovery_info: When True (default), emit the throttle-summary
                INFO via ``_emit_clear_recovery_if_needed`` on a True→False
                transition (only if at least one WARNING fired during the
                latched period). The REST WS-glitch auto-clear path passes
                False because it has already emitted its own combined
                verdict + suppressed-count INFO and we must not log two
                recovery lines on a single clear.
        """
        was_set = self._same_order_error
        self._same_order_error = False
        self._recent_executions_long.clear()
        self._recent_executions_short.clear()
        if was_set and emit_recovery_info:
            self._emit_clear_recovery_if_needed()
        else:
            # Always reset throttle state on any reset call — keeps state
            # consistent even when no recovery INFO is emitted (silent latch
            # + silent clear, or caller-suppressed INFO).
            self._same_order_warn_last_ts = None
            self._same_order_warn_suppressed = 0

    def _emit_clear_recovery_if_needed(self) -> None:
        """Emit the throttle-summary recovery INFO and reset throttle state.

        Only emits when ``_same_order_warn_last_ts is not None`` — i.e. at
        least one ``Same-order error active`` WARNING was emitted during the
        latched period. Silent-latch / silent-clear cycles (block latched
        and cleared without ``on_ticker`` ever entering the placement gate)
        produce no recovery INFO because there is no warning flood to close.

        Does not touch ``_same_order_error`` or the execution buffers — the
        caller owns flag/buffer lifecycle. Idempotent on repeat calls.
        """
        if self._same_order_warn_last_ts is not None:
            logger.info(
                f"{self.strat_id}: Same-order error cleared "
                f"(suppressed {self._same_order_warn_suppressed} WARNINGs since)"
            )
        self._same_order_warn_last_ts = None
        self._same_order_warn_suppressed = 0

    def _load_grid_state(self) -> Optional[list[dict]]:
        """Load saved grid if config matches.

        Returns the saved `grid` list, or None if no store configured, no entry
        for this strat_id, the entry is in the legacy anchor-only format, or
        the saved grid_step/grid_count differ from the current config.
        """
        if self._state_store is None:
            return None

        saved = self._state_store.load(self._config.strat_id)
        if saved is None:
            return None

        if (
            saved.get("grid_step") == self._config.grid_step
            and saved.get("grid_count") == self._config.grid_count
        ):
            logger.info(
                f"{self.strat_id}: Loaded saved grid ({len(saved['grid'])} levels, "
                f"grid_step={saved['grid_step']}, grid_count={saved['grid_count']})"
            )
            return saved["grid"]
        else:
            logger.info(
                f"{self.strat_id}: Config changed, will build fresh grid "
                f"(saved: step={saved.get('grid_step')}, count={saved.get('grid_count')}; "
                f"current: step={self._config.grid_step}, count={self._config.grid_count})"
            )
            return None

    def _on_grid_change(self, grid: list[dict], exchange_ts: Optional[datetime]) -> None:
        """Persist grid mutations triggered from inside Grid.build_grid /
        Grid.update_grid. Skips writes for the just-built single-WAIT case
        and for empty grids (a restored grid that failed validation).

        Backends are independent guards (0047): file path is timestamp-agnostic
        and runs whenever ``_state_store`` is configured; DB path requires
        ``_grid_state_writer`` AND a non-None ``exchange_ts`` (constructor-time
        restore_grid carries no triggering event and would falsify
        ``at_or_before`` lookups).
        """
        if len(grid) <= 1:
            return

        if self._state_store is not None:
            self._state_store.save(
                strat_id=self._config.strat_id,
                grid=grid,
                grid_step=self._config.grid_step,
                grid_count=self._config.grid_count,
            )

        if self._grid_state_writer is not None and exchange_ts is not None:
            self._grid_state_writer.write(
                strat_id=self._config.strat_id,
                grid=grid,
                grid_step=self._config.grid_step,
                grid_count=self._config.grid_count,
                account_id=self._account_id,
                symbol=self._config.symbol,
                exchange_ts=exchange_ts,
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

    def on_ticker(self, event: TickerEvent) -> list[PlaceLimitIntent | CancelIntent]:
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

            # Only execute intents if no same-order error.
            # WARNING is throttled (feature 0046 / issue #94): loud-first,
            # then suppress within _SAME_ORDER_WARN_THROTTLE_SEC, then a
            # single heartbeat re-emit with `(suppressed N since last)`.
            if self._same_order_error:
                now = datetime.now(UTC)
                if self._same_order_warn_last_ts is None:
                    logger.warning(
                        f"{self.strat_id}: Same-order error active, skipping order placement"
                    )
                    self._same_order_warn_last_ts = now
                else:
                    elapsed_sec = (now - self._same_order_warn_last_ts).total_seconds()
                    if elapsed_sec >= _SAME_ORDER_WARN_THROTTLE_SEC:
                        logger.warning(
                            f"{self.strat_id}: Same-order error active, skipping order placement "
                            f"(suppressed {self._same_order_warn_suppressed} since last)"
                        )
                        self._same_order_warn_last_ts = now
                        self._same_order_warn_suppressed = 0
                    else:
                        self._same_order_warn_suppressed += 1
                return intents

            if intents:
                self._execute_intents(intents, limit_orders)

            return intents
        except Exception as e:
            logger.error(f"{self.strat_id}: Error in on_ticker: {e}", exc_info=True)
            raise

    def on_execution(self, event: ExecutionEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """Process execution (fill) event.

        Args:
            event: Execution event with fill details.

        Returns:
            List of intents generated (for logging/tracking).

        Raises:
            Exception: Re-raised after logging so orchestrator can handle notification.
        """
        try:
            # Reset the per-event phantom drop flag. _check_same_orders may
            # set it to True via _diagnostic_rest_check_executions when this
            # event is the WS_GLITCH-confirmed phantom side of a SAME ORDER
            # pair; in that case we drop the event before it reaches the
            # engine AND before mutating tracked-order status (see below).
            self._drop_phantom_event_for_current_call = False

            # Look up tracked order WITHOUT mutating its status yet. The
            # mark_filled() call is deferred until after _check_same_orders
            # has had a chance to classify the event. If REST proves this is
            # a phantom (WS_GLITCH_SUSPECTED), the underlying exchange order
            # is still resting — we must not flip the tracked status to
            # "filled", or get_limit_orders() would drop it from the live
            # in-memory view (it filters status not in {"placed"}; runner.py:324)
            # and the reconciler would see a stale local book.
            tracked = self._find_tracked_order(event.order_link_id, event.order_id)
            if tracked is None:
                logger.debug(
                    f"{self.strat_id}: Received execution for untracked order "
                    f"order_id={event.order_id} order_link_id={event.order_link_id!r}"
                )

            # Check for same-order error (bbu2-style safety check). May
            # auto-clear `_same_order_error` and set
            # `_drop_phantom_event_for_current_call` if REST classifies the
            # pair as WS_GLITCH_SUSPECTED.
            self._check_same_orders(event)

            # Drop confirmed phantom events before they reach the engine
            # AND before mutating tracked-order state (feature 0031). REST
            # has authoritatively confirmed the fill did not happen on the
            # exchange; passing it to `self._engine.on_event(event)` would
            # corrupt grid/position state, and `tracked.mark_filled()`
            # would corrupt the local open-order view. The "update grid
            # state regardless of error" convention does not apply here —
            # we have a verdict, not just an error.
            if self._drop_phantom_event_for_current_call:
                logger.debug(
                    f"{self.strat_id}: dropping phantom ExecutionEvent "
                    f"order_id={event.order_id} (WS_GLITCH_SUSPECTED) "
                    f"before engine.on_event and mark_filled"
                )
                return []

            # Verdict was not WS_GLITCH_SUSPECTED — proceed normally.
            if tracked is not None:
                tracked.mark_filled()
                logger.info(
                    f"{self.strat_id}: Order filled: {event.symbol} {event.side} "
                    f"qty={event.qty} price={event.price}"
                )

            # Pass to engine (update grid state regardless of error)
            intents = self._engine.on_event(event)

            # Only execute intents if no same-order error.
            if intents and not self._same_order_error:
                limit_orders = self.get_limit_orders()
                self._execute_intents(intents, limit_orders)

            return intents
        except Exception as e:
            logger.error(f"{self.strat_id}: Error in on_execution: {e}", exc_info=True)
            raise

    def on_order_update(self, event: OrderUpdateEvent) -> list[PlaceLimitIntent | CancelIntent]:
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
                    tracked.mark_filled()
                elif event.status in ("Cancelled", "Rejected"):
                    tracked.mark_cancelled()
                elif event.status in ("New", "PartiallyFilled"):
                    if tracked.order_id is None:
                        tracked.mark_placed(event.order_id)
            else:
                logger.debug(
                    f"{self.strat_id}: Received order_update for untracked order "
                    f"order_id={event.order_id} order_link_id={event.order_link_id!r}"
                )
                # Fast-track the next order-sync sweep so the reconciler picks
                # up this manual/unknown order and the next tick can cancel it
                # if it's off-grid. Only on `New` — `Cancelled`/`Filled` tail
                # events for orders we never tracked are harmless.
                if event.status == "New" and self._on_unknown_order is not None:
                    self._on_unknown_order(self.strat_id)

            # Pass to engine (update state regardless of error)
            intents = self._engine.on_event(event)

            # Only execute intents if no same-order error
            if intents and not self._same_order_error:
                limit_orders = self.get_limit_orders()
                self._execute_intents(intents, limit_orders)

            return intents
        except Exception as e:
            logger.error(f"{self.strat_id}: Error in on_order_update: {e}", exc_info=True)
            raise

    def on_position_update(
        self,
        long_position: Optional[dict],
        short_position: Optional[dict],
        wallet_balance: float,
        last_close: Optional[float],
    ) -> None:
        """Update position state and recalculate multipliers.

        Args:
            long_position: Long position data from exchange.
            short_position: Short position data from exchange.
            wallet_balance: Current wallet balance.
            last_close: Current price, or None when no ticker has been seen yet.

        Raises:
            Exception: Re-raised after logging so orchestrator can handle notification.
        """
        try:
            # Store wallet balance for qty computation
            self._wallet_balance = Decimal(str(wallet_balance))

            # Build PositionState objects
            long_state = self._build_position_state(long_position, wallet_balance, DirectionType.LONG)
            short_state = self._build_position_state(short_position, wallet_balance, DirectionType.SHORT)

            # Update position sizes (used by _is_good_to_place). While a
            # direction is dirty (feature 0064), the size write is gated so a
            # stale/mis-ordered WS frame cannot clobber the REST-authoritative
            # mirror and reopen the 110017 storm. Only the `.size` field is
            # gated; the position_ratio / liquidation / multiplier calcs below
            # read the freshly-built WS long_state / short_state directly.
            long_ws_size = long_state.size if long_state else Decimal('0')
            short_ws_size = short_state.size if short_state else Decimal('0')
            self._long_position.size = self._apply_dirty_ws_size_gate(
                DirectionType.LONG, long_ws_size
            )
            self._short_position.size = self._apply_dirty_ws_size_gate(
                DirectionType.SHORT, short_ws_size
            )

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
            self._long_position.liquidation_price = (
                long_state.liquidation_price if long_state else Decimal('0')
            )
            self._short_position.liquidation_price = (
                short_state.liquidation_price if short_state else Decimal('0')
            )

            has_position = long_state is not None or short_state is not None
            has_valid_price = (
                last_close is not None
                and math.isfinite(float(last_close))
                and float(last_close) > 0
            )
            if has_position and not has_valid_price:
                logger.warning(
                    "%s: Position update has no valid market price; "
                    "updated sizes/wallet but left risk multipliers unchanged",
                    self.strat_id,
                )
                self._last_position_check = datetime.now(UTC)
                return
            price = float(last_close) if last_close is not None else 0.0

            # Reset both positions once before calculating (bbu2 pattern)
            self._long_position.reset_amount_multiplier()
            self._short_position.reset_amount_multiplier()

            # Calculate multipliers per direction.
            # Long is calculated first; cross-position effects on short are preserved
            # because short's reset already happened above.
            if long_state:
                opposite = short_state or PositionState(direction=DirectionType.SHORT)
                self._long_position.calculate_amount_multiplier(
                    long_state, opposite, price
                )

            if short_state:
                opposite = long_state or PositionState(direction=DirectionType.LONG)
                self._short_position.calculate_amount_multiplier(
                    short_state, opposite, price
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

        # bbu2 early-imbalance multiplier — applied before round_qty to mirror
        # bbu2 __round_amount(amount * mult). See gridcore.qty.apply_early_imbalance
        # for the full semantic (including the size-vs-margin ratio invariant).
        resolved_qty = apply_early_imbalance(
            resolved_qty,
            self._long_position,
            self._short_position,
            self._config.early_imbalance_multiplier,
        )

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
        # cumRealisedPnl: lifetime cumulative realised PnL (does NOT reset per
        # cycle; ~80x the UI value). curRealisedPnl: current-cycle realised PnL
        # = the Bybit Web UI "Realized" column.
        cum_realized_pnl = float(position_data.get("cumRealisedPnl", 0) or 0)
        cur_realized_pnl = float(position_data.get("curRealisedPnl", 0) or 0)

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
            cum_realized_pnl=Decimal(str(cum_realized_pnl)),
            cur_realized_pnl=Decimal(str(cur_realized_pnl)),
        )

    def _execute_intents(
        self,
        intents: list[PlaceLimitIntent | CancelIntent],
        limits: dict[str, list[dict]],
    ) -> None:
        """Execute a list of intents.

        Cancels run before places so margin and the per-symbol active-order
        slots held by stale orders are freed before new placements try to
        consume them. Within each group the engine's nearest-to-farthest
        ordering is preserved.
        """
        if self._executor.auth_cooldown:
            logger.debug(f"{self.strat_id}: Auth cooldown active, skipping {len(intents)} intents")
            return

        cancels = [i for i in intents if isinstance(i, CancelIntent)]
        places = [i for i in intents if isinstance(i, PlaceLimitIntent)]

        for intent in cancels:
            if self._executor.auth_cooldown:
                logger.debug(f"{self.strat_id}: Auth cooldown activated mid-batch, skipping remaining intents")
                return
            self._execute_cancel_intent(intent)

        if cancels:
            limits = self.get_limit_orders()

        for intent in places:
            if self._executor.auth_cooldown:
                logger.debug(f"{self.strat_id}: Auth cooldown activated mid-batch, skipping remaining intents")
                return
            self._execute_place_intent(intent, limits)
            # Refresh limits after each placement so _is_good_to_place
            # sees newly placed orders. Without this, multiple reduce-only
            # intents in the same batch can over-cover the position because
            # they all check against the same stale snapshot.
            limits = self.get_limit_orders()

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

    def _find_tracked_order(
        self, order_link_id: Optional[str], order_id: Optional[str]
    ) -> Optional[TrackedOrder]:
        """Find a tracked order by client_order_id or exchange order_id.

        Tries _tracked_orders (keyed by client_order_id) first, then scans
        values for matching exchange order_id. Strips the post-2026-05-08
        `-{millis}` suffix from order_link_id so wire-form events match
        the deterministic prefix used as the dict key.
        """
        prefix = extract_client_order_prefix(order_link_id)
        if prefix and prefix in self._tracked_orders:
            return self._tracked_orders[prefix]
        if order_id:
            for t in self._tracked_orders.values():
                if t.order_id == order_id:
                    return t
        return None

    def _clear_dirty(self, direction: str) -> None:
        """Clear a direction's dirty episode and its episode-scoped state (0064).

        Invariant: ``_last_dirty_rest_at[d]`` and ``_last_rest_position_size[d]``
        are meaningful ONLY while ``_position_dirty[d]`` is True. Every
        dirty-clear path (successful reduce-only close, WS-size match, forced
        reconcile) resets all three so a stale baseline/throttle from one episode
        never leaks into the next (F3): a fresh episode always refreshes on its
        first placement and establishes its own baseline.
        """
        self._position_dirty[direction] = False
        self._last_dirty_rest_at[direction] = None
        self._last_rest_position_size[direction] = None
        self._dirty_ws_mismatch_streak[direction] = 0

    def _apply_dirty_ws_size_gate(self, direction: str, ws_size: Decimal) -> Decimal:
        """Gate a WS position-size write while a direction is dirty (0064).

        Steady state (not dirty) → write the WS size unchanged. While dirty and
        a REST baseline exists: an exact match clears dirty (WS recovered and
        agrees with REST); a non-match keeps the REST-authoritative size and
        leaves dirty set (a stale WS frame must not reopen the storm). While
        dirty but no baseline yet (no refresh has run — `rest_client=None` or
        `dirty_refresh_enabled=False`) → write the WS size normally; the gate
        only protects a value REST has actually established.
        """
        if not self._position_dirty.get(direction, False):
            return ws_size
        baseline = self._last_rest_position_size.get(direction)
        if baseline is None:
            return ws_size
        if ws_size == baseline:
            self._clear_dirty(direction)
            logger.info(
                "%s: WS size %s matches last REST read for %s — clearing dirty",
                self.strat_id, ws_size, direction,
            )
            return ws_size
        # Non-match while dirty: keep the REST-authoritative size and track the
        # consecutive-mismatch streak (review v3 #2). A WARNING at each threshold
        # multiple flags a WS feed stuck beyond the normal recovery window.
        streak = self._dirty_ws_mismatch_streak[direction] + 1
        self._dirty_ws_mismatch_streak[direction] = streak
        if streak >= _DIRTY_WS_MISMATCH_ALERT_THRESHOLD and streak % _DIRTY_WS_MISMATCH_ALERT_THRESHOLD == 0:
            logger.warning(
                "%s: %d consecutive WS position-size mismatches for dirty %s "
                "(REST baseline %s held) — WS feed may be stuck beyond the "
                "normal recovery window",
                self.strat_id, streak, direction, baseline,
            )
        else:
            logger.debug(
                "%s: ignoring non-matching WS size %s for dirty %s "
                "(REST baseline %s stays authoritative, streak=%d)",
                self.strat_id, ws_size, direction, baseline, streak,
            )
        return baseline

    def _refresh_position_size_from_rest(
        self, direction: str, *, force: bool = False
    ) -> None:
        """Refresh ONE direction's mirror ``size`` from a fresh REST read (0064).

        Used on the dirty path before the guard, and by the forced reconcile
        (``force=True``). Filters the flat ``get_positions`` list by
        ``positionIdx`` (hedge mode: 1=long, 2=short) itself — unlike
        ``on_position_update``, which receives already-split long/short dicts.
        A missing/zero entry sets the mirror to ``0`` (position now flat),
        mirroring ``_build_position_state``/``on_position_update``. Only the
        ``size`` field is touched (multipliers/liq are out of scope).
        """
        # Throttle the ATTEMPT (success or failure) so a persistently failing
        # get_positions / rest_client=None cannot re-fire the refresh on every
        # tick (F2). The force path bypasses the throttle and resets the whole
        # episode below.
        if not force:
            self._last_dirty_rest_at[direction] = self._clock()

        if self._rest_client is None:
            logger.warning(
                "%s: dirty REST refresh for %s requested but no rest_client — "
                "falling back to in-memory mirror",
                self.strat_id, direction,
            )
            return

        position_idx = 1 if direction == DirectionType.LONG else 2
        try:
            positions = self._rest_client.get_positions(self._config.symbol)
        except Exception as e:
            self._dirty_rest_refresh_failure_count += 1
            logger.warning(
                "%s: dirty REST refresh get_positions failed for %s: %s: %s — "
                "keeping in-memory mirror",
                self.strat_id, direction, type(e).__name__, e,
            )
            return

        def _matches(p: dict) -> bool:
            try:
                return int(p.get("positionIdx", -1)) == position_idx
            except (TypeError, ValueError):
                # Malformed positionIdx in a (possibly partial) entry: skip it
                # rather than crash the hot path — degrade to the mirror.
                return False

        entry = next((p for p in positions if _matches(p)), None)
        if entry is None:
            new_size = Decimal("0")
        else:
            try:
                new_size = Decimal(str(entry.get("size", 0) or 0))
            except (ArithmeticError, ValueError, TypeError) as e:
                self._dirty_rest_refresh_failure_count += 1
                logger.warning(
                    "%s: dirty REST refresh got unparseable size %r (%s) for %s — "
                    "keeping in-memory mirror",
                    self.strat_id, entry.get("size"), type(e).__name__, direction,
                )
                return

        if direction == DirectionType.LONG:
            old = self._long_position.size
            self._long_position.size = new_size
        else:
            old = self._short_position.size
            self._short_position.size = new_size

        logger.info(
            "%s: dirty REST position refresh %s size %s -> %s (force=%s)",
            self.strat_id, direction, old, new_size, force,
        )
        if force:
            # Forced reconcile is a positive health signal: clear the whole
            # episode (dirty + throttle + baseline) — the mirror written above
            # is now authoritative and dirty is over.
            self._clear_dirty(direction)
        else:
            # Establish the REST baseline the WS gate consults for THIS episode.
            self._last_rest_position_size[direction] = new_size

    def _is_good_to_place(self, intent: PlaceLimitIntent, limits: dict[str, list[dict]]) -> bool:
        """Check if order can be placed (exact-duplicate + reduce-only checks).

        1. Exact-duplicate check: if a placed order with same (price, qty, side,
           reduce_only) already exists, reject. Matches bbu2 bybit_api_usdt.py:296-300.
        2. For reduce_only orders, checks that total reduce-only qty on the book
           + new order qty doesn't exceed position size.

        Args:
            intent: The order intent to validate.
            limits: Current limit orders from get_limit_orders() or exchange.
                Format: {'long': [order_dicts], 'short': [order_dicts]}.

        Reference: bbu_reference/bbu2-master/bybit_api_usdt.py:295-313
        """
        close_side_map = {DirectionType.LONG: 'Sell', DirectionType.SHORT: 'Buy'}
        close_side = close_side_map.get(intent.direction, '')
        reduce_only_qty = Decimal("0")

        all_orders = limits.get('long', []) + limits.get('short', [])
        for order in all_orders:
            # Exact duplicate check (bbu2 style)
            if (Decimal(str(order['price'])) == intent.price
                    and Decimal(str(order['qty'])) == intent.qty
                    and order['side'] == intent.side
                    and order['reduceOnly'] == intent.reduce_only):
                logger.debug(
                    f"{self.strat_id}: Rejecting exact duplicate order at "
                    f"price={intent.price} qty={intent.qty} side={intent.side}"
                )
                return False
            # Sum reduce_only qty for position-size check
            if order['reduceOnly'] and order['side'] == close_side:
                reduce_only_qty += Decimal(str(order['qty']))

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

        return position_size > (intent.qty + reduce_only_qty)

    @staticmethod
    def _assign_wire_link_id(
        intent: PlaceLimitIntent,
        *,
        existing_order_link_id: Optional[str] = None,
    ) -> PlaceLimitIntent:
        """Return an intent carrying the wire orderLinkId for this placement."""
        if intent.order_link_id is not None:
            return intent
        wire_id = existing_order_link_id or make_order_link_id(intent.client_order_id)
        return replace(intent, order_link_id=wire_id)

    def _execute_place_intent(self, intent: PlaceLimitIntent, limits: dict[str, list[dict]]) -> None:
        """Execute a place order intent.

        Pipeline (feature 0064 — explicit order, do not reorder):
        1. resolve qty → qty<=0 early return.
        2. breaker ``is_blocked`` → early return (no REST, no guard, no submit
           while a scope key is in cooldown).
        3. dirty-mirror REST refresh (reduce-only only, throttled) BEFORE the
           guard, so step 4 evaluates fresh size.
        4. ``_is_good_to_place`` → unchanged guard (now reads the freshened
           mirror); oversized reduce-only is rejected here, nothing submitted.
        5. duplicate-track + wire-link-id (existing).
        6. ``execute_place`` → post-submit breaker bookkeeping.
        """
        # Step 1 — resolve qty (engine emits qty=0, we fill it in)
        intent = self._resolve_qty(intent)
        if intent.qty <= 0:
            logger.debug(f"{self.strat_id}: Skipping order with qty<=0 at {intent.price}")
            return

        now = self._clock()

        # Step 2 — circuit-breaker: drop while this scope key is in cooldown.
        # Sits first so a tripped scope never triggers a REST refresh on
        # per-ticker re-emission during cooldown.
        if self._truncate_breaker.is_blocked(intent.side, intent.price, now):
            logger.debug(
                f"{self.strat_id}: 110017 breaker tripped — dropping "
                f"{intent.side} @ {intent.price}"
            )
            return

        # Step 3 — dirty-mirror REST refresh before the guard (reduce-only only).
        # `dirty_refresh_enabled` is the FIRST term so flipping it off is a true
        # kill-switch. intent.direction is the position being closed (a
        # reduce-only Sell → LONG), so no side→direction inversion here.
        if (
            self._config.dirty_refresh_enabled
            and intent.reduce_only
            and self._position_dirty.get(intent.direction, False)
        ):
            last = self._last_dirty_rest_at.get(intent.direction)
            interval = self._config.dirty_rest_refresh_min_interval_seconds
            if last is None or (now - last) >= interval:
                self._refresh_position_size_from_rest(intent.direction)
            else:
                logger.debug(
                    f"{self.strat_id}: dirty REST refresh throttled for "
                    f"{intent.direction} (last={last}, now={now})"
                )

        # Step 4 — duplicate + reduce-only guard (bbu2 _is_good_to_place,
        # unchanged body; now reading the freshened-when-dirty mirror).
        if not self._is_good_to_place(intent, limits):
            logger.debug(
                f"{self.strat_id}: Skipping order at {intent.price} - "
                f"rejected by _is_good_to_place"
            )
            return

        # Step 5 — duplicate-track + wire-link-id assignment (existing).
        reusable_order_link_id = None
        if intent.client_order_id in self._tracked_orders:
            tracked = self._tracked_orders[intent.client_order_id]
            if tracked.status in ("pending", "placed"):
                logger.debug(
                    f"{self.strat_id}: Skipping duplicate order {intent.client_order_id}"
                )
                return
            if (
                tracked.status == "failed"
                and tracked.intent is not None
                and tracked.intent.order_link_id is not None
            ):
                reusable_order_link_id = tracked.intent.order_link_id

        assigned = self._assign_wire_link_id(
            intent,
            existing_order_link_id=reusable_order_link_id,
        )

        # Track order
        tracked = TrackedOrder(
            client_order_id=assigned.client_order_id,
            intent=assigned,
            status="pending",
        )
        self._tracked_orders[assigned.client_order_id] = tracked

        # Step 6 — execute + breaker bookkeeping
        result = self._executor.execute_place(assigned)

        if result.success:
            tracked.mark_placed(result.order_id)
            self._truncate_breaker.record_success(intent.side, intent.price)
            # Only a successful reduce-only CLOSE proves position >= qty (it
            # exercised the guard against fresh-enough state and Bybit accepted
            # it). A successful OPEN on the same direction proves nothing about
            # position-size divergence, so it must NOT clear dirty (F1) — else
            # interleaved opens re-arm one 110017 per open until the breaker
            # trips, defeating the silent self-heal.
            if intent.reduce_only:
                self._clear_dirty(intent.direction)  # divergence healed
            return

        tracked.mark_failed()

        if not is_truncate_error(result.error):
            # Non-110017 failure → existing retry-queue behaviour (feature 0032
            # wire-id reuse preserved via the failed-tracked-order path above).
            if self._on_intent_failed:
                self._on_intent_failed(assigned, result.error)
            return

        # --- 110017 (orderQty truncated to zero) handling ---
        # Mark the direction dirty so the NEXT placement REST-refreshes the
        # mirror before the guard (self-heal), even before the breaker trips.
        self._position_dirty[intent.direction] = True

        tripped = self._truncate_breaker.record_110017(intent.side, intent.price, now)

        # Drop the wire-id reuse for 110017: the order was never created on
        # Bybit (hard reject), so the feature-0032 reuse rationale (avoid a
        # duplicate on an *ambiguous* outcome) does not apply. Reusing it could
        # surface as 110072 — which the breaker does not count and the retry
        # queue does not exclude — partially bypassing the backstop. Clearing
        # the tracked intent's order_link_id forces a fresh id next emission.
        tracked.intent = replace(tracked.intent, order_link_id=None)

        # Do NOT enqueue 110017 to the retry queue — retrying is pointless
        # (same qty, same clamp) and is part of the storm.

        if tripped:
            logger.warning(
                "%s: 110017 circuit-breaker tripped for %s @ %s — dropping "
                "intents for %.0fs (trip #%d)",
                self.strat_id, intent.side, intent.price,
                self._config.truncate_breaker_cooldown_seconds,
                self._truncate_breaker_reconcile_count + 1,
            )
            self._truncate_breaker_reconcile_count += 1
            if self._config.truncate_breaker_reconcile and self._on_truncate_breaker_tripped:
                self._on_truncate_breaker_tripped(self.strat_id, intent.direction)

    def _execute_cancel_intent(self, intent: CancelIntent) -> None:
        """Execute a cancel order intent."""
        result = self._executor.execute_cancel(intent)

        tracked = self._find_tracked_order(None, intent.order_id)
        if tracked and result.success:
            tracked.mark_cancelled()

        if not result.success and self._on_intent_failed:
            self._on_intent_failed(intent, result.error)

    def inject_open_orders(self, orders: list[dict]) -> None:
        """Inject open orders from exchange (for reconciliation on startup).

        Each order is stored in _tracked_orders keyed by client_order_id
        (orderLinkId if present, otherwise orderId). Orders without orderId
        are skipped.

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
            except Exception as e:
                logger.warning(
                    f"{self.strat_id}: Skipping injected order {order_id} "
                    f"with invalid price={price!r} or qty={qty!r}: {e}"
                )
                continue

            order_symbol = order.get("symbol")
            if order_symbol and order_symbol != self._config.symbol:
                logger.warning(
                    f"{self.strat_id}: Skipping injected order {order_id} — "
                    f"symbol mismatch (expected {self._config.symbol}, "
                    f"got {order_symbol})"
                )
                continue

            intent = PlaceLimitIntent.create(
                symbol=self._config.symbol,
                side=side,
                price=dec_price,
                qty=dec_qty,
                grid_level=0,
                direction=direction,
                reduce_only=reduce_only,
            )

            # Prefer orderLinkId prefix as key for backward compatibility:
            # old orders (placed before this change) still have orderLinkId on
            # the exchange, so events for those orders arrive with
            # order_link_id set. Post-2026-05-08 orderLinkIds carry a
            # `-{millis}` suffix on the wire — strip it so the dict stays
            # keyed by the deterministic client_order_id prefix. Empty/None
            # falls back to orderId.
            link_prefix = extract_client_order_prefix(order_link_id)
            client_id = link_prefix or order_id

            # Guard against key collisions (e.g., orderLinkId equal to some
            # existing orderId, or duplicate injection of the same order).
            if client_id in self._tracked_orders:
                existing = self._tracked_orders[client_id]
                if existing.status in ("pending", "failed"):
                    if existing.intent is None:
                        logger.warning(
                            f"{self.strat_id}: open-order upgrade skipped: "
                            f"tracked order has no intent (prefix={client_id})"
                        )
                        continue

                    # Bybit should echo our wire id; fallback keeps the prior
                    # assigned id if an older/direct exchange payload omits it.
                    exchange_link_id = order_link_id or existing.intent.order_link_id
                    upgraded_intent = replace(
                        existing.intent,
                        order_link_id=exchange_link_id,
                    )
                    prev_state = existing.status
                    existing.intent = upgraded_intent
                    existing.mark_placed(order_id)
                    removed = (
                        self._on_retry_cancel_for_prefix(client_id)
                        if self._on_retry_cancel_for_prefix
                        else 0
                    )
                    logger.info(
                        f"{self.strat_id}: tracked order upgraded from {prev_state} "
                        f"via reconcile (prefix={client_id}, order_id={order_id}, "
                        f"link_id={exchange_link_id}, retry_cancelled={removed})"
                    )
                    injected += 1
                    continue
                logger.warning(
                    f"{self.strat_id}: Skipping injected order {order_id} — "
                    f"key collision on client_id={client_id}"
                )
                continue

            tracked = TrackedOrder(
                client_order_id=client_id,
                order_id=order_id,
                intent=intent,
                status="placed",
            )
            self._tracked_orders[client_id] = tracked
            injected += 1

        skipped = len(orders) - injected
        if skipped > 0:
            logger.warning(
                f"{self.strat_id}: Skipped {skipped} orders during injection "
                f"(see warnings above)"
            )
        logger.info(f"{self.strat_id}: Injected {injected}/{len(orders)} open orders")

    def mark_order_cancelled_by_order_id(self, order_id: str) -> None:
        """Mark a tracked order as cancelled by its exchange order_id.

        Used by reconciler when exchange state diverges from in-memory state.
        """
        tracked = self._find_tracked_order(None, order_id)
        if tracked:
            tracked.mark_cancelled()

    def get_placed_order_ids(self) -> set[str]:
        """Get exchange order_ids of all placed (active) orders.

        Used by reconciler to compare in-memory state with exchange state.
        """
        return {
            t.order_id for t in self._tracked_orders.values()
            if t.status == "placed" and t.order_id
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

        Owns the lifecycle of ``_same_order_error``: resets the flag
        once before evaluation, then lets ``_check_same_orders_side``
        set it to True on detection. The error auto-clears when new
        fills push the problematic pair out of the buffer, because
        each execution re-enters here and starts from a clean reset.

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
            buffer_label = "long"
        else:
            buffer = self._recent_executions_short
            buffer_label = "short"

        # Add to buffer (newest first). Diagnostic fields (closed_size,
        # order_link_id, buffer_label) are kept on the record so SAME ORDER
        # ERROR can dump the actual classification path — needed to tell apart
        # a real grid-duplicate (both fills truly the same intent) from a
        # hedge-mode misclassification (classifier put two distinct hedge-pair
        # fills into the same buffer because closedSize was reported as 0 by
        # Bybit on a reduce-only fill).
        exec_record = {
            "order_id": event.order_id,
            "order_link_id": event.order_link_id,
            "price": event.price,
            "side": event.side,
            "closed_size": event.closed_size,
            "buffer": buffer_label,
            "exchange_ts": event.exchange_ts,
        }
        buffer.appendleft(exec_record)

        # Reset the flag once, then check both buffers (long first, short
        # second, matching bbu2). The side-check is set-on-error only; we
        # own the reset here so an unrelated fill on short can't clear an
        # error detected on long. Early-return after long keeps the work
        # bounded when a duplicate was already found.
        # Snapshot `was_set` so a True→False net transition (clean-fill
        # auto-clear: existing block, no duplicate re-established) routes
        # through the recovery-INFO helper. Does NOT clear execution
        # buffers — buffers are the substrate of the auto-clear mechanism
        # itself. See feature 0046 / issue #94.
        was_set = self._same_order_error
        self._same_order_error = False
        self._check_same_orders_side(self._recent_executions_long)
        if self._same_order_error:
            return
        self._check_same_orders_side(self._recent_executions_short)
        if was_set and not self._same_order_error:
            self._emit_clear_recovery_if_needed()

    def _check_same_orders_side(self, executions: deque) -> None:
        """Check execution buffer for same-price duplicates within a time window.

        Compares consecutive executions. If same price and side but different
        order_id AND the two fills happened within ``_SAME_ORDER_TIME_WINDOW_SEC``,
        this indicates a duplicate order was placed at the same price level -
        a grid bug.

        Time-window rationale (feature 0025): bbu2's detector was designed
        for *concurrent* grid duplication — two intents emitted for the same
        slot in the same tick, so both fills land within milliseconds. Our
        engine instead exhibits *sequential* grid-walk replacement: as price
        walks up/down, an old level is rebuilt later at the same price,
        minutes-to-hours after the original fill. Without a time-window
        guard, every legitimate grid replacement looks like a duplicate. The
        5-second window is comfortably above worst-case hedge-pair
        concurrent-fill latency (<500 ms) and far below any plausible
        grid-replacement gap.

        Set-on-error only: this method sets ``_same_order_error`` to
        True on detection and never clears it. The caller
        (``_check_same_orders``) is responsible for resetting the flag
        once before invoking this across both buffers.

        Args:
            executions: Deque of recent execution records for one direction.
        """
        if len(executions) < 2:
            return

        exec_list = list(executions)
        for current, previous in zip(exec_list, exec_list[1:]):
            if current["price"] == previous["price"] and current["side"] == previous["side"]:
                if current["order_id"] == previous["order_id"]:
                    # Same order ID (partial fills) - OK
                    return
                cur_tracked = self._find_tracked_order(
                    current.get("order_link_id"), current.get("order_id")
                )
                prev_tracked = self._find_tracked_order(
                    previous.get("order_link_id"), previous.get("order_id")
                )

                # Time-window guard: when both orders are tracked, compare
                # placement times rather than fill times. Concurrent duplicate
                # orders can fill slowly in thin markets; legitimate sequential
                # grid-walk replacements are placed far apart because the later
                # order is emitted only after the earlier one filled.
                if cur_tracked is not None and prev_tracked is not None:
                    delta_sec = abs(
                        (cur_tracked.placed_ts - prev_tracked.placed_ts).total_seconds()
                    )
                    if delta_sec > _SAME_ORDER_TIME_WINDOW_SEC:
                        return
                else:
                    # Fallback for tests, older/incomplete tracking, and
                    # external events where placement time is unavailable.
                    # Buffer is maxlen=2, so a single mismatched pair means no
                    # duplicate exists in the buffer at all — `return` is correct.
                    # If the buffer ever grows past 2, switch to `continue` so
                    # other pairs in the buffer can still be evaluated.
                    cur_ts = current.get("exchange_ts")
                    prev_ts = previous.get("exchange_ts")
                    if cur_ts is not None and prev_ts is not None:
                        delta_sec = abs((cur_ts - prev_ts).total_seconds())
                        if delta_sec > _SAME_ORDER_TIME_WINDOW_SEC:
                            return
                # Different order IDs at same price = DUPLICATE candidate.
                # Compute the dedup pair key early and gate the loud path on
                # verdict-aware cache lookup (feature 0031): the same pair
                # within _SAME_ORDER_DEDUP_TTL_SEC is suppressed to a single
                # DEBUG line; soft-block state for REAL_DUPLICATE entries is
                # explicitly re-established because _check_same_orders just
                # reset _same_order_error to False at the top of this call.
                pair_key = frozenset((current["order_id"], previous["order_id"]))
                now = datetime.now(UTC)

                # Lazy expiry of stale entries; keeps the cache bounded.
                self._same_order_dedup_cache = {
                    k: e for k, e in self._same_order_dedup_cache.items()
                    if (now - e.last_seen_ts).total_seconds() < _SAME_ORDER_DEDUP_TTL_SEC
                }

                existing = self._same_order_dedup_cache.get(pair_key)
                if existing is not None and existing.verdict in (
                    "WS_GLITCH_SUSPECTED",
                    "REAL_DUPLICATE",
                ):
                    age_sec = (now - existing.first_seen_ts).total_seconds()
                    logger.debug(
                        f"{self.strat_id}: duplicate SAME ORDER pair suppressed "
                        f"within TTL — pair=({current['order_id']}, {previous['order_id']}) "
                        f"price={current['price']} side={current['side']} "
                        f"age={age_sec:.0f}s verdict={existing.verdict}"
                    )
                    existing.last_seen_ts = now
                    if existing.verdict == "WS_GLITCH_SUSPECTED":
                        # Cache-hit on a known phantom: signal `on_execution`
                        # to drop the current event before mark_filled and
                        # engine.on_event. Without this, the dedup branch
                        # would silence the alert but still let the engine
                        # and tracked-order state mutate on a fill REST has
                        # already proved fake.
                        self._drop_phantom_event_for_current_call = True
                    elif existing.verdict == "REAL_DUPLICATE":
                        # Re-establish the soft-block. _check_same_orders
                        # unconditionally resets _same_order_error to False
                        # at the top of every call; without this re-set the
                        # dedup gate would silently lift a legitimately-
                        # latched block on each retrigger. Do NOT set the
                        # phantom-drop flag — the event is real (REST saw
                        # the fill); mark_filled and engine.on_event must
                        # run normally.
                        self._same_order_error = True
                    return
                # UNKNOWN cache entry (REST cross-check earlier failed and never
                # produced a verdict) falls through to the full first-trigger
                # path below — the operator should get a fresh, loud ALERT and
                # a fresh cross-check attempt.

                # First-trigger path (cache miss or UNKNOWN fall-through).
                # Diagnostic dump: include closed_size, order_link_id, and
                # tracked-order reduce_only (looked up via _tracked_orders)
                # for both fills. This determines whether it's a real
                # grid-duplicate (same reduce_only, both tracked) or a
                # hedge-mode misclassification (different reduce_only or one
                # untracked).
                cur_reduce_only = (
                    cur_tracked.intent.reduce_only
                    if cur_tracked and cur_tracked.intent else "unknown"
                )
                prev_reduce_only = (
                    prev_tracked.intent.reduce_only
                    if prev_tracked and prev_tracked.intent else "unknown"
                )
                logger.error(
                    f"{self.strat_id}: SAME ORDER ERROR - Two different orders filled "
                    f"at same price {current['price']} side={current['side']} "
                    f"(order_ids: {current['order_id']}, {previous['order_id']}) "
                    f"[diagnostic: buffer={current.get('buffer','?')}, "
                    f"current closed_size={current.get('closed_size','?')} "
                    f"order_link_id={current.get('order_link_id','')!r} "
                    f"reduce_only={cur_reduce_only}, "
                    f"previous closed_size={previous.get('closed_size','?')} "
                    f"order_link_id={previous.get('order_link_id','')!r} "
                    f"reduce_only={prev_reduce_only}]"
                )
                self._same_order_error = True
                if self._notifier:
                    self._notifier.alert(
                        f"SAME ORDER ERROR: {self.strat_id} - Two different orders filled "
                        f"at price {current['price']} side={current['side']}. "
                        f"Order placement BLOCKED.",
                        error_key=f"same_order_{self.strat_id}",
                    )

                # Insert (or refresh) cache entry as UNKNOWN before the REST
                # cross-check so a same-event burst (incident saw 5 ERRORs in 1
                # second on the same pair) is suppressed by the dedup gate on
                # events 2..N. The cross-check writes the resolved verdict back.
                self._same_order_dedup_cache[pair_key] = _SameOrderDedupEntry(
                    first_seen_ts=existing.first_seen_ts if existing is not None else now,
                    last_seen_ts=now,
                    verdict="UNKNOWN",
                )

                # Diagnostic REST cross-check verifies whether Bybit-side
                # actually has two distinct fills, or the WS stream duplicated
                # a single fill event. The latter would mean SAME ORDER is a
                # WS-glitch false positive, not a real grid duplicate; in that
                # case the cross-check auto-clears the soft-block.
                self._diagnostic_rest_check_executions(current, previous)
                return

    def _diagnostic_rest_check_executions(self, current: dict, previous: dict) -> None:
        """Cross-check SAME ORDER trigger against Bybit REST execution history.

        Hypothesis being tested: WS execution stream may emit duplicate events
        for a single Bybit-side fill, making `_check_same_orders` see "two
        different orders" when there's actually one. REST `get_executions` is
        the authoritative source — it returns Bybit's stored execution list.

        For each of the two order_ids from the SAME ORDER pair, fetch a
        paginated, time-bounded REST slice and count actual fills. If REST
        says 1 per orderId → buffer was correct, real duplicate. If a complete
        REST slice sees exactly one order_id → the missing side is treated as
        a WS phantom. Truncated/empty coverage is inconclusive and leaves the
        block latched.

        Failures here are logged as ERROR but never raised — diagnostic is
        best-effort, never breaks the main loop.
        """
        pair_key = frozenset((current["order_id"], previous["order_id"]))
        try:
            # Access the rest_client through the executor. This is a
            # diagnostic-only path; a proper API addition can come later.
            rest_client = self._executor._client
            current_ts = current.get("exchange_ts")
            previous_ts = previous.get("exchange_ts")
            if not isinstance(current_ts, datetime) or not isinstance(previous_ts, datetime):
                raise ValueError("SAME ORDER REST cross-check requires exchange_ts")

            window_start = min(current_ts, previous_ts) - timedelta(
                seconds=_SAME_ORDER_REST_WINDOW_PADDING_SEC
            )
            window_end = max(current_ts, previous_ts) + timedelta(
                seconds=_SAME_ORDER_REST_WINDOW_PADDING_SEC
            )
            start_ms = int(window_start.timestamp() * 1000)
            end_ms = int(window_end.timestamp() * 1000)
            executions, truncated = rest_client.get_executions_all(
                symbol=self.symbol,
                start_time=start_ms,
                end_time=end_ms,
                max_pages=_SAME_ORDER_REST_MAX_PAGES,
                return_truncated=True,
            )
            cur_id = current["order_id"]
            prev_id = previous["order_id"]
            cur_matches = [e for e in executions if e.get("orderId") == cur_id]
            prev_matches = [e for e in executions if e.get("orderId") == prev_id]
            if truncated:
                verdict = "UNKNOWN"
            elif len(cur_matches) >= 1 and len(prev_matches) >= 1 and cur_id != prev_id:
                verdict = "REAL_DUPLICATE"
            elif (len(cur_matches) >= 1) != (len(prev_matches) >= 1):
                verdict = "WS_GLITCH_SUSPECTED"
            else:
                verdict = "UNKNOWN"
            logger.error(
                f"{self.strat_id}: SAME ORDER REST cross-check — verdict={verdict} "
                f"(REST returned {len(executions)} executions for {self.symbol} "
                f"from {window_start.isoformat()} to {window_end.isoformat()}, "
                f"truncated={truncated}). "
                f"current order_id={cur_id} → {len(cur_matches)} REST matches "
                f"[{[(m.get('execId'), m.get('execPrice'), m.get('execQty'), m.get('orderLinkId'), m.get('execType')) for m in cur_matches[:3]]}]; "
                f"previous order_id={prev_id} → {len(prev_matches)} REST matches "
                f"[{[(m.get('execId'), m.get('execPrice'), m.get('execQty'), m.get('orderLinkId'), m.get('execType')) for m in prev_matches[:3]]}]"
            )

            # Write resolved verdict back into the dedup cache so subsequent
            # retriggers on the same pair hit the verdict-aware gate (feature
            # 0031). The first-trigger path inserted an UNKNOWN entry; if the
            # entry is missing (defensive: should not happen), insert a fresh
            # one.
            now = datetime.now(UTC)
            existing = self._same_order_dedup_cache.get(pair_key)
            if existing is not None:
                existing.verdict = verdict
                existing.last_seen_ts = now
            else:
                self._same_order_dedup_cache[pair_key] = _SameOrderDedupEntry(
                    first_seen_ts=now, last_seen_ts=now, verdict=verdict,
                )

            # Auto-clear the soft-block when REST confirms there is no real
            # duplicate. INFO-level only, no notifier — Notifier.alert always
            # logs at ERROR (notifier.py:67) and a recovery line must not be
            # ERROR-level. REAL_DUPLICATE leaves the block latched.
            # Single combined INFO carrying verdict + throttle-summary
            # context (feature 0046): the suppressed-count suffix is appended
            # when at least one `Same-order error active` WARNING was
            # emitted during the latched period. `reset_same_order_error`
            # is called with `emit_recovery_info=False` so it does the
            # state cleanup without emitting a second recovery INFO.
            if verdict == "WS_GLITCH_SUSPECTED":
                suffix = ""
                if self._same_order_warn_last_ts is not None:
                    suffix = (
                        f"; suppressed {self._same_order_warn_suppressed} "
                        f"WARNINGs since"
                    )
                logger.info(
                    f"{self.strat_id}: SAME ORDER soft-block auto-cleared "
                    f"(WS glitch confirmed by REST cross-check; verdict={verdict})"
                    f"{suffix}"
                )
                self.reset_same_order_error(emit_recovery_info=False)
                # Mark the in-flight ExecutionEvent as a confirmed phantom so
                # `on_execution` returns BEFORE `engine.on_event(event)`. The
                # auto-clear would otherwise both un-gate placement AND let
                # the engine record a fake fill, contaminating downstream
                # tick/execution decisions.
                self._drop_phantom_event_for_current_call = True
        except Exception as e:
            # Cross-check failed — leave the cache entry as UNKNOWN so a
            # subsequent occurrence falls through to the full first-trigger
            # path. The soft-block stays latched (safer than auto-clearing
            # without a verdict).
            logger.error(
                f"{self.strat_id}: SAME ORDER REST cross-check failed "
                f"(diagnostic only, ignoring): {e}", exc_info=True,
            )
