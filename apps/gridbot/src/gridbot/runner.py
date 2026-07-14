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
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, UTC, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from gridbot.writers.grid_state_writer import GridStateWriter
    from bybit_adapter.rest_client import BybitRestClient
    # Annotation-only: importing position_fetcher at runtime would be circular
    # (position_fetcher imports StrategyRunner).
    from gridbot.position_fetcher import WalletSnapshot

# Provider returns (snapshot, age_seconds) or None — the non-blocking
# peek_wallet_snapshot reader injected for the Phase-4 preflight (feature 0066).
WalletProvider = Callable[[], "Optional[tuple[WalletSnapshot, float]]"]

from gridcore import (  # noqa: E402
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

from gridbot.config import StrategyConfig  # noqa: E402
from gridbot.executor import (  # noqa: E402
    IntentExecutor,
    OrderResult,
    is_duplicate_link_error,
    is_insufficient_balance,
    is_network_error,
    is_truncate_error,
)
from gridbot.notifier import Notifier  # noqa: E402
from gridbot.order_link_id import make_order_link_id  # noqa: E402
from gridbot.safety_caps import SafetyCaps  # noqa: E402
from gridbot.truncate_breaker import TruncateBreaker  # noqa: E402


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

# Feature 0079 (issue #182) — throttle the per-reason safety-cap rejection
# WARNING to at most one per this many seconds per reason, so a cap held at its
# threshold (e.g. notional pinned at C1) does not flood the log every tick.
_SAFETY_CAP_WARN_THROTTLE_SEC = 60.0

# Feature 0083 (issue #202) — cap on the processed-exec_id FIFO dedup cache in
# `on_execution`. Bybit exec_ids are globally unique, so entries never need
# time-based expiry (a TTL would let a late WS replay be reprocessed); the cap
# only bounds memory. 4096 entries ≈ 18 h of fills at the observed ~230
# fills/h — orders of magnitude beyond the seconds-to-minutes window in which
# Bybit replays executions after a reconnect. Raise if fill rate grows
# (~100 bytes/entry incl. dict-node overhead; 4096 ≈ 400 KB per strat).
_EXEC_DEDUP_MAX_ENTRIES = 4096


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
        on_retry_queue_clear: Optional[Callable[[], int]] = None,
        rest_client: Optional["BybitRestClient"] = None,
        truncate_breaker: Optional[TruncateBreaker] = None,
        on_truncate_breaker_tripped: Optional[Callable[[str, str], None]] = None,
        on_divergence_failure_mix: Optional[Callable[[str, str, int], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
        wallet_provider: Optional["WalletProvider"] = None,
        wallet_ws_max_age_seconds: float = 45.0,
        safety_caps: Optional[SafetyCaps] = None,
    ):
        """Initialize strategy runner.

        Args:
            strategy_config: Strategy configuration.
            executor: Intent executor for API calls.
            account_id: UUID5-derived account identifier; matches the value
                ``orchestrator.py:1156-1162`` computes from the account name.
                Used by the DB grid-state writer (feature 0047) so snapshots
                FK-match ``runs.account_id``.
            instrument_info: Exchange instrument params. Primary source of the
                grid engine's tick_size (0090) and qty rounding. When None, the
                engine falls back to ``strategy_config.tick_size`` and applies no
                qty rounding — a bare-runner path for tests/tools, not live.
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
            on_retry_queue_clear: Optional callback to drain the retry queue
                on C3 session-loss trip (mirrors auth-cooldown queue clear).
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
            wallet_provider: Optional non-blocking reader returning
                ``(WalletSnapshot, age_seconds)`` or ``None`` — the orchestrator
                wires ``position_fetcher.peek_wallet_snapshot`` here (feature 0066
                Phase 4). When set, the preflight reads a FRESH, age-bounded free
                margin from it (authoritative even at 0; stale/None/raise →
                fail-open). When ``None`` (unit tests / ``wallet_ws_enabled=False``)
                the preflight uses the position-cadence ``_available_balance``.
            wallet_ws_max_age_seconds: Max age (s) of a provider peek the preflight
                will trust before failing open (feature 0066 Phase 4).
            safety_caps: Optional shared ``SafetyCaps`` instance (feature 0079 /
                issue #182). The runner ACCEPTS it (the orchestrator constructs
                ONE per strat and passes the SAME object to this runner and the
                IntentExecutor so C4's window and C1/C2/C3 share one source of
                truth). When None (direct/test callers) every cap call is
                short-circuited to allow / no-op; SafetyCaps itself also
                short-circuits internally when its config.enabled is False.
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
        self._on_retry_queue_clear = on_retry_queue_clear

        # Feature 0064 — 110017 retry-storm self-heal + circuit-breaker (#149).
        self._rest_client = rest_client
        self._clock = clock or time.monotonic
        self._on_truncate_breaker_tripped = on_truncate_breaker_tripped
        # Feature 0069 (issue #151) signal 1 — callback invoked when the rolling
        # placement-failure UNION window crosses its threshold.
        self._on_divergence_failure_mix = on_divergence_failure_mix
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
        # Feature 0082 (issue #185) — monotonic process-lifetime count of genuine
        # preflight skips (distinct from the resettable _skip_window summary dict).
        self._preflight_skip_count: int = 0
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
        # Feature 0069 (issue #151) signal 1 — rolling window of monotonic
        # timestamps of placement failures in the UNION {110017, 110072,
        # network}. Stamped/evicted via self._clock() (injectable) so tests
        # drive the window deterministically; cleared the instant the threshold
        # fires (see _record_placement_failure).
        self._placement_failure_window: deque[float] = deque()

        # Qty computation
        self._instrument_info = instrument_info
        self._qty_calculator = create_qty_calculator(
            strategy_config.amount, instrument_info
        )
        self._wallet_balance: Decimal = Decimal("0")

        # Feature 0066 (issue #159) — account-level balance/margin signal for
        # the low-balance preflight + chase-close. Populated by
        # on_position_update from the wallet snapshot; 0 = no data yet (the
        # preflight fails open). `_available_balance` is the free-margin figure
        # the preflight checks; the other two are observability.
        self._available_balance: Decimal = Decimal("0")
        self._total_available_balance: Decimal = Decimal("0")
        self._total_maintenance_margin: Decimal = Decimal("0")

        # Feature 0079 (issue #182) — production safety caps. `_safety_caps` is
        # the shared SafetyCaps (or None for direct/test callers → caps inert).
        # `_long_position_value`/`_short_position_value` cache the latest
        # per-direction notional from on_position_update so the place-path C1
        # read is O(1). `_safety_cap_warn_last` throttles the per-reason
        # rejection WARNING (one per reason per _SAFETY_CAP_WARN_THROTTLE_SEC)
        # so a notional-at-cap storm does not flood the log (cf. feature 0067).
        self._safety_caps = safety_caps
        self._long_position_value: Decimal = Decimal("0")
        self._short_position_value: Decimal = Decimal("0")
        self._safety_cap_warn_last: dict[str, float] = {}
        # Raw low-balance predicate (single source of truth, recomputed each
        # on_position_update), shared by the moderate_liq_risk fix (3a) and
        # chase-close (3b). Each consumer applies its own kill-switch.
        self._low_balance: bool = False
        # Chase-close state machine (feature 0066 / issue #159, default OFF).
        # The decision runs in on_position_update (which holds no `limits`
        # snapshot and never dispatches) and only appends intents to
        # `_pending_chase_intents`; they are drained on the next dispatch tick.
        self._chase_state: str = "IDLE"  # "IDLE" | "CHASING"
        self._chase_direction: Optional[str] = None
        self._chase_order: Optional[dict] = None  # {client_order_id, price, side}
        self._pending_chase_intents: list[PlaceLimitIntent | CancelIntent] = []
        # Per-direction live exchange leverage, captured in
        # _build_position_state and used by the preflight's
        # est_cost = qty*price/leverage. Kept SEPARATE from
        # PositionState.leverage (which feeds the risk-multiplier upnl calc and
        # must stay at its default to preserve backtest parity).
        self._leverage: dict[str, float] = {}

        # Feature 0066 Phase 4 — real-time wallet provider for the preflight.
        # `_wallet_provider` is the non-blocking peek_wallet_snapshot reader (or
        # None on the legacy / kill-switched path); `_wallet_ws_max_age_seconds`
        # bounds how fresh a peek must be to be trusted (else fail-open).
        self._wallet_provider = wallet_provider
        self._wallet_ws_max_age_seconds = wallet_ws_max_age_seconds
        # Monotonic timestamp of the last provider-error WARNING (throttle so a
        # persistently-raising provider can't flood the log on the hot path).
        self._last_wallet_provider_error_log: float = 0.0

        # Feature 0067 (issue #164) — suppress LowBalanceSkip log spam. Under a
        # sustained low-balance regime the stateless preflight re-rejects the
        # same N grid levels every tick (~10/s), so the per-intent DEBUG line
        # floods the log (~2.1M/day). Instead we log only the ENTER/EXIT edges
        # of the regime (per (direction, side)) at INFO and a periodic summary.
        # Plain dict ops on the single-threaded dispatch path: no Bybit calls,
        # no asyncio task (the summary + edge reconcile flush on the existing
        # dispatch cadence via _drain_pending_chase_intents using self._clock()).
        # `_skip_state`: per-key sustained-regime record (survives across ticks).
        # `_skip_tick_seen`: per-sample scratch (keys evaluated with FRESH
        #   balance this dispatch; written only when transition logging is on,
        #   cleared unconditionally each reconcile).
        # `_skip_window` (+ avail band): per-summary-window skip counts,
        #   incremented unconditionally on every genuine skip.
        self._skip_state: dict[tuple[str, str], dict] = {}
        self._skip_tick_seen: dict[tuple[str, str], dict] = {}
        self._skip_window: dict[tuple[str, str], int] = {}
        self._skip_window_avail_min: Optional[float] = None
        self._skip_window_avail_max: Optional[float] = None
        # Baseline the first summary window to construction time, NOT 0.0:
        # production self._clock is time.monotonic (a large value), so a 0.0
        # baseline would make `now - last_emit >= interval` true on the very
        # first skip and flush a mislabeled "<interval>s window" after one tick.
        self._skip_summary_last_emit: float = self._clock()

        # Restore full grid if available and config matches
        restored_grid = self._load_grid_state()

        # Create GridEngine
        grid_config = GridConfig(
            grid_count=strategy_config.grid_count,
            grid_step=strategy_config.grid_step,
        )
        # Feature 0090: grid tick comes from the exchange InstrumentInfo (the
        # orchestrator's fail-closed fetch guarantees a valid one before the
        # live runner is built). The YAML tick_size branch is only for tests/
        # tools that construct a bare runner without instrument_info — it is NOT
        # a live fallback.
        if self._instrument_info is not None:
            tick_size = self._instrument_info.tick_size
        elif strategy_config.tick_size is not None:
            tick_size = strategy_config.tick_size
        else:
            raise ValueError(
                f"{strategy_config.strat_id}: no tick_size source "
                "(instrument_info and strategy_config.tick_size are both None)"
            )
        self._engine = GridEngine(
            symbol=strategy_config.symbol,
            tick_size=tick_size,
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
        # Feature 0083 — insertion-ordered set of processed exec_ids (values
        # unused; OrderedDict gives O(1) FIFO eviction). Guards on_execution
        # against WS resync redelivery bursts: a repeat exec_id is dropped
        # before it can touch tracked-order state, the SAME ORDER buffers,
        # or the engine. Bounded by _EXEC_DEDUP_MAX_ENTRIES, no TTL.
        self._processed_exec_ids: OrderedDict[str, None] = OrderedDict()

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
    def preflight_skip_count(self) -> int:
        """Monotonic count of genuine preflight (low-balance) skips for the runner
        lifetime (feature 0082). Distinct from the per-summary-window `_skip_window`
        dict (which resets each flush); read/summed by the health sweep."""
        return self._preflight_skip_count

    @property
    def net_position_size(self) -> float:
        """Signed net position size (long - short) as a float, for the health
        snapshot gauge (feature 0082). Read-only; no behavior change."""
        return float(self._long_position.size - self._short_position.size)

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
            # Feature 0066 — drain any buffered chase-close intents first (they
            # were decided in on_position_update, which holds no limits snapshot).
            self._drain_pending_chase_intents()

            # Always pass ticker to engine (keeps last_close fresh for risk calcs)
            limit_orders = self.get_limit_orders()
            intents = self._engine.on_event(event, limit_orders)

            # Placement is suppressed while SAME ORDER is latched, but healing
            # CancelIntents (feature 0087 duplicate cleanup) still execute.
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

            self._execute_generated_intents(intents, limit_orders)

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
            # Feature 0066 — drain buffered chase-close intents first.
            self._drain_pending_chase_intents()

            # Reset the per-event phantom drop flag. _check_same_orders may
            # set it to True via _diagnostic_rest_check_executions when this
            # event is the WS_GLITCH-confirmed phantom side of a SAME ORDER
            # pair; in that case we drop the event before it reaches the
            # engine AND before mutating tracked-order status (see below).
            self._drop_phantom_event_for_current_call = False

            # Feature 0083 — execution-identity idempotency guard. A private
            # WS resync can redeliver the same execution many times in one
            # burst (issue #202). Drop repeats here, BEFORE
            # _check_same_orders: its maxlen=2 buffers and error-flag reset
            # must only ever see first sightings, or a redelivery burst
            # would evict a genuine different-oid pair and spuriously clear
            # a latched SAME ORDER error.
            if self._seen_exec_id(event.exec_id):
                logger.debug(
                    f"{self.strat_id}: dropping redelivered ExecutionEvent "
                    f"exec_id={event.exec_id} order_id={event.order_id} "
                    f"(WS resync)"
                )
                return []

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

            if intents:
                limit_orders = self.get_limit_orders()
                self._execute_generated_intents(intents, limit_orders)

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
            # Feature 0066 — drain buffered chase-close intents first.
            self._drain_pending_chase_intents()

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

            if intents:
                limit_orders = self.get_limit_orders()
                self._execute_generated_intents(intents, limit_orders)

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
        available_balance: Optional[float] = None,
        total_available_balance: Optional[float] = None,
        total_maintenance_margin: Optional[float] = None,
    ) -> None:
        """Update position state and recalculate multipliers.

        Args:
            long_position: Long position data from exchange.
            short_position: Short position data from exchange.
            wallet_balance: Current wallet balance.
            last_close: Current price, or None when no ticker has been seen yet.
            available_balance: USDT free balance for new orders (feature 0066);
                None (default) leaves the stored value unchanged so existing
                callers/tests keep working and the preflight fails open.
            total_available_balance: Account-level free margin (observability).
            total_maintenance_margin: Account-level maintenance margin
                (observability).

        Raises:
            Exception: Re-raised after logging so orchestrator can handle notification.
        """
        try:
            # Store wallet balance for qty computation
            self._wallet_balance = Decimal(str(wallet_balance))

            # Feature 0066 — store the account-level balance signal when the
            # caller provides it (None defaults preserve old callers/tests).
            if available_balance is not None:
                self._available_balance = Decimal(str(available_balance))
            if total_available_balance is not None:
                self._total_available_balance = Decimal(str(total_available_balance))
            if total_maintenance_margin is not None:
                self._total_maintenance_margin = Decimal(str(total_maintenance_margin))

            # Build PositionState objects
            long_state = self._build_position_state(long_position, wallet_balance, DirectionType.LONG)
            short_state = self._build_position_state(short_position, wallet_balance, DirectionType.SHORT)

            # Feature 0079 (issue #182) — cache per-direction notional for the
            # place-path C1 read, and evaluate the C3 session-loss breaker.
            # Realized PnL is price-independent, so this runs BEFORE the
            # no-valid-price early-return below.
            self._long_position_value = (
                long_state.position_value if long_state else Decimal("0")
            )
            self._short_position_value = (
                short_state.position_value if short_state else Decimal("0")
            )
            self._evaluate_loss_breaker(long_position, short_position)

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

            # Feature 0066 (issue #159) — low-balance predicate (single source of
            # truth, shared by the moderate_liq_risk fix and chase-close). The 3a
            # fix is gated by its own kill-switch here so that when disabled the
            # multiplier calc is byte-for-byte the pre-0066 behavior (and backtest
            # parity holds, since the backtest path never feeds available_balance).
            total_position_value = Decimal("0")
            if long_state:
                total_position_value += long_state.position_value
            if short_state:
                total_position_value += short_state.position_value
            self._low_balance = self._is_low_balance(total_position_value)
            moderate_low_balance = (
                self._low_balance
                and self._config.moderate_liq_low_balance_fix_enabled
            )

            # Reset both positions once before calculating (bbu2 pattern)
            self._long_position.reset_amount_multiplier()
            self._short_position.reset_amount_multiplier()

            # Calculate multipliers per direction.
            # Long is calculated first; cross-position effects on short are preserved
            # because short's reset already happened above.
            if long_state:
                opposite = short_state or PositionState(direction=DirectionType.SHORT)
                self._long_position.calculate_amount_multiplier(
                    long_state, opposite, price, low_balance=moderate_low_balance
                )

            if short_state:
                opposite = long_state or PositionState(direction=DirectionType.LONG)
                self._short_position.calculate_amount_multiplier(
                    short_state, opposite, price, low_balance=moderate_low_balance
                )

            self._last_position_check = datetime.now(UTC)

            long_mult = self._long_position.get_amount_multiplier()
            short_mult = self._short_position.get_amount_multiplier()
            logger.info(
                f"{self.strat_id}: Position update - ratio={position_ratio:.2f}, "
                f"long_mult=Buy:{long_mult['Buy']:.2f}/Sell:{long_mult['Sell']:.2f}, "
                f"short_mult=Buy:{short_mult['Buy']:.2f}/Sell:{short_mult['Sell']:.2f}, "
                f"avail={self._available_balance:.2f} "
                f"total_avail={self._total_available_balance:.2f} "
                f"total_mm={self._total_maintenance_margin:.2f}"
            )

            # Feature 0066 (issue #159) — chase-close decision (default OFF).
            # Buffers intents only; the next dispatch tick drains them. Runs
            # last so it sees the freshest ratio/balance/sizes.
            self._evaluate_chase(price, position_ratio)
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
        # Bybit positionIM / positionMM: per-position Initial / Maintenance
        # Margin in USDT (dollar amounts) — feature 0066 / issue #159.
        initial_margin = Decimal(str(position_data.get("positionIM", 0) or 0))
        maintenance_margin = Decimal(str(position_data.get("positionMM", 0) or 0))

        # Capture live exchange leverage for the preflight est_cost (feature
        # 0066). Stored OUT of PositionState.leverage so the risk-multiplier
        # upnl calc (position.py) is unaffected and backtest parity holds.
        lev_raw = position_data.get("leverage")
        if lev_raw not in (None, ""):
            try:
                self._leverage[direction] = float(lev_raw)
            except (TypeError, ValueError):
                pass

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
            initial_margin=initial_margin,
            maintenance_margin=maintenance_margin,
            cum_realized_pnl=Decimal(str(cum_realized_pnl)),
            cur_realized_pnl=Decimal(str(cur_realized_pnl)),
        )

    def _execute_generated_intents(
        self,
        intents: list[PlaceLimitIntent | CancelIntent],
        limits: dict[str, list[dict]],
    ) -> None:
        """Execute engine intents, suppressing all but duplicate-healing cancels
        when SAME ORDER is latched.

        Feature 0087 duplicate-healing emits CancelIntent(reason='duplicate') on
        the tick path; those must still reach the exchange while the soft-block
        is active. Everything else (placements AND other-reason cancels such as
        rebuild/side_mismatch/outside_grid) stays suppressed — a non-duplicate
        cancel executed without its paired placement would thin out the grid
        while the latch state is already suspect.
        """
        if not intents:
            return
        if self._same_order_error:
            intents = [
                i for i in intents
                if isinstance(i, CancelIntent) and i.reason == "duplicate"
            ]
            if not intents:
                return
        self._execute_intents(intents, limits)

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

    def _find_failed_tracked_by_order_identity(
        self,
        price: Decimal,
        qty: Decimal,
        side: str,
        reduce_only: bool,
    ) -> tuple[Optional[str], Optional[TrackedOrder]]:
        """Find a failed tracked placement matching exchange order identity.

        Feature 0080 salts ``client_order_id`` by ``strat_id``, but pre-0080
        resting orders on the exchange still carry the unsalted wire prefix.
        When reconcile adopts such an order after an ambiguous failed place,
        the dict keys differ even though it is the same grid level — upgrade
        by (price, qty, side, reduce_only) instead of prefix alone.
        """
        for key, tracked in self._tracked_orders.items():
            if tracked.status != "failed" or tracked.intent is None:
                continue
            intent = tracked.intent
            if (
                intent.price == price
                and intent.qty == qty
                and intent.side == side
                and intent.reduce_only == reduce_only
            ):
                return key, tracked
        return None, None

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

    def rest_position_size(self, direction: str) -> Optional[Decimal]:
        """PURE REST read of one direction's position size (feature 0069 sig 3).

        Factors ONLY the ``positionIdx``/``get_positions`` read out of
        ``_refresh_position_size_from_rest`` WITHOUT any side effects: it does
        NOT write ``_last_dirty_rest_at``, does NOT bump
        ``_dirty_rest_refresh_failure_count``, and does NOT mutate the
        ``_long_position``/``_short_position`` mirrors. Used by the divergence
        size-delta sweep, which must not perturb the dirty-refresh throttle or
        the failure-counter observability that signal 2 reads.

        Returns the parsed exchange size as ``Decimal`` (``Decimal("0")`` when no
        entry for the direction), or ``None`` on ``rest_client is None`` or any
        ``get_positions``/parse error — the caller SKIPS the comparison for that
        direction (no fire) rather than counting a phantom delta. A ``None``
        return is logged at DEBUG so REST flakiness stays observable.
        """
        if self._rest_client is None:
            logger.debug(
                "%s: rest_position_size for %s skipped — no rest_client",
                self.strat_id, direction,
            )
            return None
        position_idx = 1 if direction == DirectionType.LONG else 2
        try:
            positions = self._rest_client.get_positions(self._config.symbol)
        except Exception as e:
            logger.debug(
                "%s: rest_position_size get_positions failed for %s: %s: %s",
                self.strat_id, direction, type(e).__name__, e,
            )
            return None

        def _matches(p: dict) -> bool:
            try:
                return int(p.get("positionIdx", -1)) == position_idx
            except (TypeError, ValueError):
                return False

        entry = next((p for p in positions if _matches(p)), None)
        if entry is None:
            return Decimal("0")
        try:
            return Decimal(str(entry.get("size", 0) or 0))
        except (ArithmeticError, ValueError, TypeError):
            logger.debug(
                "%s: rest_position_size unparseable size %r for %s",
                self.strat_id, entry.get("size"), direction,
            )
            return None

    def clear_dedup_cache(self) -> None:
        """Clear the verdict-aware SAME ORDER dedup cache (feature 0069).

        Called by the orchestrator's divergence wrapper AFTER a real forced
        reconcile so stale orderLinkId adjudication state cannot suppress fresh
        retriggers against the now-resynced grid. Runs synchronously on the main
        tick thread (signals 1/2/3 fire there; signal 4 drains there), so there
        is no concurrent reader of a half-cleared cache. The cache re-populates
        naturally as new SAME ORDER pairs are adjudicated on later ticks.
        """
        self._same_order_dedup_cache.clear()

    def _record_placement_failure(self, error: Optional[str]) -> None:
        """Feed one placement failure into the divergence UNION window (sig 1).

        Invoked from BOTH ``_execute_place_intent`` failure exits (110017 branch
        and the non-110017 early-return that carries 110072/network). Applies the
        UNION-membership test itself ({110017, 110072, network}; 110007 is
        excluded) so both call sites pass the raw ``result.error``. Stamps/evicts
        with ``self._clock()`` (injectable) so the window is deterministic under
        a fake clock. When the count reaches the threshold the window is CLEARED
        immediately and the ``on_divergence_failure_mix`` callback is invoked —
        "a fire" means threshold-reached, regardless of whether the downstream
        wrapper then reconciles or suppresses (so a cooldown-suppressed fire does
        not leave the window full and re-trigger on every subsequent failure).
        Inert when the detector is disabled.
        """
        if not self._config.divergence_detector_enabled:
            return
        if not (
            is_truncate_error(error)
            or is_duplicate_link_error(error)
            or is_network_error(error)
        ):
            return
        now = self._clock()
        window = self._placement_failure_window
        window.append(now)
        cutoff = now - self._config.divergence_failure_mix_window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._config.divergence_failure_mix_threshold:
            count = len(window)
            window.clear()
            if self._on_divergence_failure_mix is not None:
                self._on_divergence_failure_mix(
                    self.strat_id, "rest_failure_mix", count
                )

    def _predicate_available_balance(self) -> tuple[Decimal, bool]:
        """Free margin for the low-balance PREDICATE (3a moderate_liq + 3b chase).

        review F1 / Phase 4: align the predicate's freshness with the preflight by
        reading the same non-blocking ``wallet_provider`` when wired, so chase and
        preflight share one freshness (plan rollout §3 prerequisite). Returns
        ``(available, authoritative)``:

        - ``authoritative=True`` — a FRESH provider peek (``age <
          wallet_ws_max_age_seconds``); its value is real even at 0 (no free
          margin), so the caller treats 0 as the most extreme low-balance state.
        - ``authoritative=False`` — the position-cadence ``_available_balance``
          latch (provider absent / stale / raised); a 0 here means "no data yet"
          and the caller must NOT trigger defenses on it.

        Additive: no provider (unit tests / backtest / ``wallet_ws_enabled=False``)
        returns the latch with ``authoritative=False`` — exact pre-Phase-4 behavior.
        Never raises (a predicate read must not abort ``on_position_update``).
        Note this is INTENTIONALLY different from the preflight's stale handling:
        the preflight fails OPEN on a stale/None peek, whereas the predicate falls
        back to the latch (its best-available position-cadence value).
        """
        if self._wallet_provider is not None:
            try:
                peek = self._wallet_provider()
            except Exception:
                peek = None  # provider error → fall back to the latch
            if peek is not None:
                snapshot, age = peek
                if age < self._wallet_ws_max_age_seconds:
                    return Decimal(str(snapshot.available_balance)), True
        return self._available_balance, False

    def _is_low_balance(self, total_position_value: Decimal) -> bool:
        """Low-balance predicate (feature 0066 / issue #159).

        Single source of truth shared by the moderate_liq_risk fix (3a) and
        chase-close (3b). True when free margin is small relative to total
        position value (``available < total_position_value *
        low_balance_fraction``). The balance source shares the preflight's
        freshness via ``_predicate_available_balance`` (review F1).

        A FRESH provider 0 (genuinely no free margin) is the most extreme
        low-balance state → True. A latch 0 (provider absent/stale) means "no
        data yet" → False, so neither defense acts on stale/absent data. No
        position to measure against → False.
        """
        avail, authoritative = self._predicate_available_balance()
        if total_position_value <= 0:
            return False
        if avail <= 0:
            return authoritative  # fresh 0 = real (low); latch 0 = no data (not)
        frac = Decimal(str(self._config.low_balance_fraction))
        return avail < total_position_value * frac

    def _effective_leverage(self, direction: str) -> Decimal:
        """Leverage for the preflight est_cost (feature 0066 / issue #159).

        Prefer the live exchange leverage captured per-direction in
        ``_build_position_state``; fall back to the conservative
        ``assumed_leverage`` config when none has been observed. Bias LOW:
        under-estimating leverage only over-rejects affordable opens, it never
        lets an unaffordable one through.
        """
        lev = self._leverage.get(direction)
        if lev is None or lev <= 0:
            lev = self._config.assumed_leverage
        return Decimal(str(lev))

    def _preflight_available_balance(self, intent: PlaceLimitIntent) -> Optional[Decimal]:
        """Resolve the free-margin figure the open-order preflight checks.

        Returns the Decimal available balance to test against, or ``None`` to
        FAIL OPEN (skip the check). Two paths (feature 0066 Phase 2 base + P4):

        - **Provider wired (Phase 4):** read the non-blocking peek. A FRESH peek
          (``age < wallet_ws_max_age_seconds``) is AUTHORITATIVE even at 0 (a
          real "no free margin" signal that must block every open); a stale /
          ``None`` / raising peek → fail-open. Never falls back to
          ``_available_balance`` — the latch reads the same WS/REST caches and is
          never fresher, so a stale peek means the latch is stale too. A raising
          provider is caught (throttled WARNING) and must never abort dispatch.
        - **No provider (legacy / ``wallet_ws_enabled=False``):** use the
          position-cadence ``_available_balance``; ``> 0`` means data (check it),
          ``<= 0`` means no data yet → fail-open.
        """
        if self._wallet_provider is not None:
            try:
                peek = self._wallet_provider()
            except Exception as e:
                now = self._clock()
                if now - self._last_wallet_provider_error_log >= 60.0:
                    self._last_wallet_provider_error_log = now
                    logger.warning(
                        "%s: wallet_provider raised (%s); preflight fails open",
                        self.strat_id, e,
                    )
                return None  # a balance read must never abort order dispatch
            if peek is None:
                return None  # no cached snapshot → fail-open
            snapshot, age = peek
            if age >= self._wallet_ws_max_age_seconds:
                # Stale → fail-open. Do NOT trust the stale peek and do NOT fall
                # back to the (equally-stale) _available_balance latch.
                return None
            # Fresh: authoritative, even when 0 (no free margin → blocks opens).
            return Decimal(str(snapshot.available_balance))

        # No provider: legacy position-cadence latch; 0 = no data yet → fail-open.
        avail = self._available_balance
        if avail <= 0:
            return None
        return avail

    def _preflight_blocks_open(self, intent: PlaceLimitIntent) -> bool:
        """True if this OPEN order is unaffordable and must be blocked locally.

        Caller gates on ``not intent.reduce_only`` and
        ``preflight_balance_check_enabled``. Resolves the balance source via
        ``_preflight_available_balance`` (fail-open → ``None`` → not blocked),
        then applies the leverage-adjusted ``est_cost`` + buffer affordability
        test (``est_cost = qty*price/leverage``; over-conservative leverage only
        ever over-rejects, never lets an unaffordable open through).
        """
        # Feature 0067 — the affordability DECISION below is unchanged; this
        # method now also records per-(direction, side) skip evidence so the
        # sustained-skip regime can be logged at its edges (ENTER/EXIT) instead
        # of one DEBUG line per blocked grid level per tick. THREE return points,
        # all preserved:
        avail = self._preflight_available_balance(intent)
        if avail is None:
            # (0) Fail-open: no fresh balance data (no provider + non-positive
            # latch, provider raised, or a stale WS peek). The order is allowed
            # through, so this is NOT a skip: state-neutral AND evidence-neutral
            # — no _skip_state mutation, no _skip_window increment, and no
            # _skip_tick_seen write (a fail-open carries no affordability verdict,
            # so it must neither create nor satisfy an EXIT for the key). Leaving
            # the key absent from the scratch is what preserves a sibling key's
            # genuine fresh skip recorded in the same sample (Medium-3).
            logger.debug(
                "%s: preflight skipped (no fresh balance data) for %s %s",
                self.strat_id, intent.direction, intent.side,
            )
            return False
        leverage = max(self._effective_leverage(intent.direction), Decimal("1"))
        est_cost = (intent.qty * intent.price) / leverage
        buffer = Decimal(str(self._config.preflight_balance_buffer))
        key = (intent.direction, intent.side)
        if avail < est_cost * (Decimal("1") + buffer):
            # (1) Unaffordable → a genuine fresh skip; block (return True).
            avail_f = float(avail)
            # Phase-2 window counter: incremented unconditionally on every
            # genuine skip, INDEPENDENT of the transition flag, so the periodic
            # summary works even when transition logging is off (M1). Reset only
            # by the summary flush itself.
            self._skip_window[key] = self._skip_window.get(key, 0) + 1
            self._preflight_skip_count += 1
            self._skip_window_avail_min = (
                avail_f if self._skip_window_avail_min is None
                else min(self._skip_window_avail_min, avail_f))
            self._skip_window_avail_max = (
                avail_f if self._skip_window_avail_max is None
                else max(self._skip_window_avail_max, avail_f))
            if self._config.low_balance_skip_transition_logs_enabled:
                # Per-sample scratch upsert (written ONLY while transition logging
                # is on → no count accrual / False→True poisoning when off). The
                # ENTER/EXIT edges + the 60s summary carry the signal, so the
                # per-intent DEBUG line is suppressed here.
                s = self._skip_tick_seen.get(key)
                if s is None:
                    s = {"blocked": False, "count": 0, "avail_min": avail_f,
                         "avail_max": avail_f, "first_blocked_price": 0.0}
                    self._skip_tick_seen[key] = s
                if not s["blocked"]:
                    # first BLOCKED level for this key this sample (sticky-True).
                    s["blocked"] = True
                    s["first_blocked_price"] = float(intent.price)
                s["count"] += 1
                s["avail_min"] = min(s["avail_min"], avail_f)
                s["avail_max"] = max(s["avail_max"], avail_f)
            else:
                # Transition logging OFF → emit the per-intent DEBUG line exactly
                # as before (byte-for-byte current behavior). DEBUG, not INFO:
                # one line per blocked open on the hot path; the low-balance
                # state also stays observable at INFO via the per-tick
                # `Position update` heartbeat (avail= / total_avail=).
                logger.debug(
                    "%s: LowBalanceSkip %s %s qty=%s price=%s "
                    "est_cost=%.4f avail=%.4f buffer=%s",
                    self.strat_id, intent.direction, intent.side,
                    intent.qty, intent.price, float(est_cost),
                    avail_f, buffer,
                )
            return True
        # (2) Genuinely affordable → allow (return False). Mark the key
        # evaluated-fresh-affordable for this sample so Part B can drive an EXIT;
        # a sibling level already recorded as blocked stays blocked (sticky).
        if self._config.low_balance_skip_transition_logs_enabled:
            if key not in self._skip_tick_seen:
                self._skip_tick_seen[key] = {
                    "blocked": False, "count": 0, "avail_min": float(avail),
                    "avail_max": float(avail), "first_blocked_price": 0.0}
        return False

    # ---- Feature 0067 (issue #164) — LowBalanceSkip edge logging + summary ----

    def _reconcile_skip_edges(self) -> None:
        """Resolve LowBalanceSkip ENTER/EXIT edges at the SAMPLE boundary (once
        per dispatch, via _drain_pending_chase_intents). Edges are decided on the
        WHOLE sample's view of a key — never inline per intent — so a cheap
        affordable level and a pricier blocked level of the same key in one tick
        resolve to a single "blocked" verdict instead of EXIT/ENTER flutter.

        Reconciles ONLY keys present in `_skip_tick_seen` (the keys actually
        evaluated with FRESH balance in the just-finished sample). A key absent
        from the scratch (no open intents for it, or only fail-open reads) carries
        no evidence and is left untouched by steps 1-2 — this is what prevents an
        interleaved `on_order_update` (which evaluates zero open preflights) from
        manufacturing a spurious EXIT then re-ENTER on the next `on_ticker`.

        Edge LOGGING (steps 1-2) is gated on the transition kill-switch; the
        scratch CLEAR (step 3) is UNCONDITIONAL so a True→False→True runtime flag
        flip cannot strand scratch written in the last enabled dispatch and replay
        it as stale evidence on re-enable.
        """
        cfg = self._config
        if cfg.low_balance_skip_transition_logs_enabled:
            now = self._clock()
            # Step 1 — per-key edges for keys evaluated fresh this sample.
            for key, s in self._skip_tick_seen.items():
                direction, side = key
                st = self._skip_state.get(key)
                if s["blocked"]:
                    if st is None or not st["active"]:
                        # ENTER — start (or restart) the sustained-skip regime.
                        self._skip_state[key] = {
                            "active": True, "count": s["count"],
                            "avail_min": s["avail_min"], "avail_max": s["avail_max"],
                            "first_blocked_price": s["first_blocked_price"],
                            "last_blocked_clock": now,
                        }
                        logger.info(
                            "%s: LowBalanceSkip ENTER direction=%s side=%s "
                            "first_blocked_price=%.4f avail_min=%.4f avail_max=%.4f",
                            self.strat_id, direction, side,
                            s["first_blocked_price"], s["avail_min"], s["avail_max"],
                        )
                    else:
                        # Sustained — accumulate, widen band, refresh idle clock.
                        st["count"] += s["count"]
                        st["avail_min"] = min(st["avail_min"], s["avail_min"])
                        st["avail_max"] = max(st["avail_max"], s["avail_max"])
                        st["last_blocked_clock"] = now
                else:
                    # Evaluated fresh and zero blocked levels for this key.
                    if st is not None and st["active"]:
                        # EXIT — the recovery edge.
                        logger.info(
                            "%s: LowBalanceSkip EXIT direction=%s side=%s after "
                            "%d skips, avail_min=%.4f avail_max=%.4f",
                            self.strat_id, direction, side, st["count"],
                            st["avail_min"], st["avail_max"],
                        )
                        st["active"] = False
                        st["count"] = 0
                    # else: affordable and already idle → no-op.
            # Step 2 — idle-timeout sweep over ALL active keys (not just scratch
            # keys) to EXIT a key removed from the grid mid-storm with no recovery
            # EXIT. During a live storm the key is re-blocked every tick (~10/s)
            # so last_blocked_clock refreshes and the timeout never fires — it
            # only fires after a SUSTAINED absence of blocks (a genuinely ended
            # episode), so it does not reintroduce a single-event false EXIT.
            idle = cfg.low_balance_skip_exit_idle_seconds
            if idle > 0:
                for key, st in self._skip_state.items():
                    if st["active"] and now - st["last_blocked_clock"] >= idle:
                        direction, side = key
                        logger.info(
                            "%s: LowBalanceSkip EXIT direction=%s side=%s after %d "
                            "skips (idle %ss, no affordable confirmation)",
                            self.strat_id, direction, side, st["count"], idle,
                        )
                        st["active"] = False
                        st["count"] = 0
        # Step 3 — UNCONDITIONAL scratch clear for the next sample.
        self._skip_tick_seen = {}

    def _emit_skip_summary(self) -> None:
        """Periodic INFO summary of LowBalanceSkip activity, flushed on the
        dispatch cadence (no asyncio task) using the injectable self._clock().
        Per-runner (per-strat); the window counter accumulates independently of
        the transition flag, so the summary works even with edge logging off."""
        cfg = self._config
        if not cfg.low_balance_skip_summary_enabled:
            return
        now = self._clock()
        if now - self._skip_summary_last_emit < cfg.low_balance_skip_summary_interval_sec:
            return
        total = sum(self._skip_window.values())
        if total == 0:
            # No activity this window — advance the clock, emit nothing (no
            # empty-window spam).
            self._skip_summary_last_emit = now
            return
        parts = " ".join(
            f"{direction}.{side}={n}"
            for (direction, side), n in self._skip_window.items()
        )
        logger.info(
            "%s: LowBalanceSkip %ss window: %s(%s) total=%d avail_min=%.4f avail_max=%.4f",
            self.strat_id, cfg.low_balance_skip_summary_interval_sec,
            self.strat_id, parts, total,
            self._skip_window_avail_min if self._skip_window_avail_min is not None else 0.0,
            self._skip_window_avail_max if self._skip_window_avail_max is not None else 0.0,
        )
        self._skip_window = {}
        self._skip_window_avail_min = None
        self._skip_window_avail_max = None
        self._skip_summary_last_emit = now

    # ---- Feature 0066 (issue #159) — chase-close active defense (default OFF) ----

    def _drain_pending_chase_intents(self) -> None:
        """Dispatch chase intents buffered by on_position_update.

        Called at the top of on_ticker / on_execution / on_order_update — the
        only sites that hold a `limits` snapshot and call _execute_intents. The
        buffer is reused through the existing dispatch path (cancel-before-place,
        per-place limits refresh, reduce-only guard) with zero new dispatch code.
        """
        # Feature 0067 — flush the LowBalanceSkip edge reconcile + periodic
        # summary on the existing dispatch cadence. These MUST run BEFORE the
        # early-return guard below: the guard returns on the common path
        # (chase_close_enabled=False default → empty chase buffer almost always),
        # so a call placed after it would never fire during a low-balance storm.
        # The reconcile observes the scratch written by the PREVIOUS dispatch's
        # _execute_intents (one evaluated-sample latency, harmless for logging).
        self._reconcile_skip_edges()
        self._emit_skip_summary()

        if not self._pending_chase_intents:
            return
        buffered = self._pending_chase_intents
        self._pending_chase_intents = []
        self._execute_intents(buffered, self.get_limit_orders())

    def _dominant_size(self, direction: str) -> Decimal:
        return (self._long_position.size if direction == DirectionType.LONG
                else self._short_position.size)

    def _evaluate_chase(self, price: float, position_ratio: float) -> None:
        """Chase-close decision. Mutates chase state + appends intents to the
        buffer ONLY (never dispatches — on_position_update has no limits)."""
        if not self._config.chase_close_enabled or price <= 0:
            return
        thr = self._config.chase_position_ratio_threshold
        if thr <= 0:
            return
        hyst = self._config.chase_close_hysteresis

        dominant: Optional[str] = None
        if self._low_balance:
            if position_ratio > thr:
                dominant = DirectionType.LONG
            elif position_ratio < (1.0 / thr):
                dominant = DirectionType.SHORT

        if self._chase_state == "IDLE":
            if dominant is not None:
                self._enter_chase(dominant, price)
            return

        # CHASING — exit if conditions cleared, else re-peg on drift.
        if self._should_exit_chase(position_ratio, thr, hyst):
            self._exit_chase()
            return
        self._repeg_chase_if_drifted(price)

    def _build_chase_order(
        self, direction: str, close_side: str, price: float, size: Decimal
    ) -> Optional[PlaceLimitIntent]:
        """Build a reduce-only, post-only close near the touch for the dominant
        side. Maker-safe pricing (Sell above / Buy below) so post-only never
        crosses/rejects. Qty is half the dominant size (rounded to qty_step),
        which stays strictly below position_size so the reduce-only guard passes."""
        offset = Decimal(str(self._config.chase_offset_pct))
        p = Decimal(str(price))
        chase_price = (p * (Decimal("1") + offset) if close_side == "Sell"
                       else p * (Decimal("1") - offset))
        qty = size / Decimal("2")
        if self._instrument_info:
            chase_price = self._instrument_info.round_price(chase_price)
            qty = self._instrument_info.round_qty(qty)
            if qty < self._instrument_info.min_qty:
                return None
        if qty <= 0 or qty >= size:
            return None
        return PlaceLimitIntent.create(
            symbol=self._config.symbol, side=close_side, price=chase_price,
            qty=qty, grid_level=-1, direction=direction,
            reduce_only=True, post_only=True,
            strat_id=self._config.strat_id,
        )

    def _enter_chase(self, direction: str, price: float) -> None:
        size = self._dominant_size(direction)
        if size <= 0:
            return  # nothing to trim
        chase = self._build_chase_order(
            direction, "Sell" if direction == DirectionType.LONG else "Buy", price, size
        )
        if chase is None:
            return  # position too small to trim safely
        # Cancel resting grow-side opens for the dominant direction (stop adding
        # to the side we are trying to shrink). This is a deliberate,
        # balance-driven cancel — distinct from the forward-only multiplier rule.
        grow_side = "Buy" if direction == DirectionType.LONG else "Sell"
        for tracked in list(self._tracked_orders.values()):
            ti = tracked.intent
            if (tracked.status == "placed" and tracked.order_id and ti is not None
                    and not ti.reduce_only and ti.direction == direction
                    and ti.side == grow_side):
                self._pending_chase_intents.append(CancelIntent(
                    symbol=self._config.symbol, order_id=tracked.order_id,
                    reason="chase_close", price=ti.price, side=ti.side,
                ))
        self._pending_chase_intents.append(chase)
        self._chase_state = "CHASING"
        self._chase_direction = direction
        self._chase_order = {
            "client_order_id": chase.client_order_id,
            "price": chase.price,
            "side": chase.side,
        }
        logger.info(
            "%s: chase-close ENTER %s close=%s price=%s qty=%s",
            self.strat_id, direction, chase.side, chase.price, chase.qty,
        )

    def _should_exit_chase(self, position_ratio: float, thr: float, hyst: float) -> bool:
        if not self._low_balance:
            return True
        d = self._chase_direction
        if d is None or self._dominant_size(d) <= 0:
            return True  # dominant position flat
        if d == DirectionType.LONG:
            return position_ratio < thr * (1.0 - hyst)
        return position_ratio > (1.0 / thr) * (1.0 + hyst)

    def _retire_live_chase_order(self, reason: str) -> None:
        """Retire the current chase order on exit / re-peg.

        Two cases (a chase decision can fire on several `on_position_update`s
        before the next dispatch tick drains the buffer):
        - the chase place is still BUFFERED (never dispatched) → just drop it
          from the buffer; there is nothing resting on the exchange. Without
          this it would dispatch and rest as an orphan after the state machine
          has already moved on.
        - the chase order was already DISPATCHED and is resting → cancel it by
          exchange order_id.
        """
        if not self._chase_order:
            return
        coid = self._chase_order["client_order_id"]
        before = len(self._pending_chase_intents)
        self._pending_chase_intents = [
            i for i in self._pending_chase_intents
            if not (isinstance(i, PlaceLimitIntent) and i.client_order_id == coid)
        ]
        if len(self._pending_chase_intents) != before:
            return  # was still buffered → dropped, nothing to cancel on-exchange
        tracked = self._tracked_orders.get(coid)
        if tracked and tracked.order_id and tracked.status == "placed":
            self._pending_chase_intents.append(CancelIntent(
                symbol=self._config.symbol, order_id=tracked.order_id,
                reason=reason, price=self._chase_order["price"],
                side=self._chase_order["side"],
            ))

    def _repeg_chase_if_drifted(self, price: float) -> None:
        if not self._chase_order:
            return
        ref = self._chase_order["price"]
        if ref <= 0:
            return
        rel = abs(Decimal(str(price)) - ref) / ref
        if rel <= Decimal(str(self._config.chase_replace_drift_pct)):
            return
        # Cancel-replace the chase order ONLY (explicitly exempt from the
        # forward-only invariant — it acts on its own order, not grid orders).
        self._retire_live_chase_order("chase_replace")
        size = self._dominant_size(self._chase_direction)
        new_chase = self._build_chase_order(
            self._chase_direction, self._chase_order["side"], price, size
        )
        if new_chase is None:
            self._exit_chase()
            return
        self._pending_chase_intents.append(new_chase)
        self._chase_order = {
            "client_order_id": new_chase.client_order_id,
            "price": new_chase.price,
            "side": new_chase.side,
        }
        logger.info(
            "%s: chase-close REPEG %s price=%s",
            self.strat_id, self._chase_direction, new_chase.price,
        )

    def _exit_chase(self) -> None:
        self._retire_live_chase_order("chase_exit")
        # Drop buffered grow-side cancel intents queued on _enter_chase. They are
        # only valid while CHASING; if exit happens before the next drain, leaving
        # them would cancel resting grid opens after chase mode has ended.
        self._pending_chase_intents = [
            i for i in self._pending_chase_intents
            if not (isinstance(i, CancelIntent) and i.reason == "chase_close")
        ]
        logger.info("%s: chase-close EXIT %s", self.strat_id, self._chase_direction)
        self._chase_state = "IDLE"
        self._chase_direction = None
        self._chase_order = None

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

        # Feature 0066 (issue #159) — low-balance preflight for OPEN orders.
        # Reject locally an open order the account can't afford so Bybit never
        # returns 110007 and the retry queue is never fed (the storm). Reduce-only
        # orders are exempt (they free margin) and fall through to the existing
        # reduce-only guard below.
        if (not intent.reduce_only
                and self._config.preflight_balance_check_enabled
                and self._preflight_blocks_open(intent)):
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

    def _evaluate_safety_caps(
        self, intent: PlaceLimitIntent, limits: dict[str, list[dict]]
    ) -> tuple[bool, Optional[str]]:
        """Evaluate C1/C2/C3 production safety caps for a place intent.

        Shared by the first-dispatch path (``_execute_place_intent`` Step 2.5)
        and the retry-queue re-dispatch (``retry_dispatch_place``) so the cap
        evaluation lives in one place; callers own the response (silent drop +
        log / rejection alert vs ``OrderResult`` error).

        Returns ``(allowed, reason)``. ``reason`` is ``None`` when allowed (or
        no SafetyCaps is wired); otherwise it is the cap sentinel WITHOUT the
        ``safety_cap_`` prefix — ``"loss_breaker"`` for the C3 latch, else the
        ``CapDecision.reason`` (``"max_notional"`` / ``"max_open_orders"``).
        """
        if self._safety_caps is None:
            return True, None
        # C3 latch (full circuit breaker — open AND reduce-only).
        if self._safety_caps.loss_tripped():
            return False, "loss_breaker"
        open_order_count = sum(len(v) for v in limits.values())
        if intent.reduce_only:
            decision = self._safety_caps.allow_reduce_only(
                open_order_count=open_order_count
            )
        else:
            total_notional = (
                self._long_position_value + self._short_position_value
            )
            decision = self._safety_caps.allow_open(
                total_notional=total_notional,
                open_order_count=open_order_count,
            )
        if not decision.allowed:
            return False, decision.reason
        return True, None

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

        # Step 2.5 — production safety caps (feature 0079 / issue #182). Runs
        # AFTER qty-resolve + the 110017 breaker and BEFORE the dirty refresh /
        # _is_good_to_place guard, so a capped intent never reaches the strategy
        # guard or the exchange. Inert when no SafetyCaps is wired (None) or its
        # config is disabled. A capped intent is dropped, NOT enqueued.
        allowed, reason = self._evaluate_safety_caps(intent, limits)
        if not allowed:
            if reason == "loss_breaker":
                # C3 latch: once the session-loss breaker has tripped, suppress
                # ALL new places (open AND reduce-only — full circuit breaker;
                # the trip ERROR/alert + working-order cancel already fired once
                # in on_position_update). Silent drop here, no rejection alert.
                logger.debug(
                    "%s: safety-cap loss breaker latched — dropping %s @ %s",
                    self.strat_id, intent.side, intent.price,
                )
            else:
                self._emit_safety_cap_rejection(intent, reason)
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

        # Feature 0079 (issue #182) — a C4 rate-limit sentinel from the executor
        # ("safety_cap_rate_limit") must be DROPPED, not enqueued (else the
        # rate-limited intent re-storms the retry queue). Mirrors the 110007 /
        # 110017 "drop, don't enqueue" decisions below.
        if result.error and result.error.startswith("safety_cap"):
            logger.warning(
                "%s: %s on %s %s @ %s — dropping (not enqueued)",
                self.strat_id, result.error, intent.direction, intent.side, intent.price,
            )
            return

        # Feature 0066 (issue #159) — 110007 "available balance not enough" on an
        # OPEN order: do NOT enqueue to the retry queue. A boundary race (balance
        # moved between the preflight read and submit) can still surface 110007;
        # retrying the same/grown qty against the same exhausted balance just
        # re-storms. Drop it (log once) — the preflight re-gates on the next tick
        # once balance recovers. Mirrors the 0064 "do NOT enqueue 110017"
        # decision. Scoped to OPEN orders (not reduce_only) to match the spec —
        # reduce-only closes do not 110007 (Bybit accepts them under exhausted
        # balance), so a reduce-only failure here is some other condition and
        # keeps the normal retry path rather than being silently dropped.
        # Stateless: no breaker.
        if not intent.reduce_only and is_insufficient_balance(result.error):
            logger.warning(
                "%s: 110007 available-balance-not-enough on %s %s @ %s — "
                "dropping (not enqueued); preflight re-gates next tick",
                self.strat_id, intent.direction, intent.side, intent.price,
            )
            return

        if not is_truncate_error(result.error):
            # Non-110017 failure → existing retry-queue behaviour (feature 0032
            # wire-id reuse preserved via the failed-tracked-order path above).
            # Feature 0069 — 110072/network leave via THIS branch, so record
            # here too (the helper applies the UNION test and ignores the rest).
            self._record_placement_failure(result.error)
            if self._on_intent_failed:
                self._on_intent_failed(assigned, result.error)
            return

        # --- 110017 (orderQty truncated to zero) handling ---
        # Feature 0069 signal 1 — 110017 feeds the placement-failure UNION too
        # (the helper short-circuits when the detector is disabled).
        self._record_placement_failure(result.error)
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

    def retry_dispatch_place(self, intent: PlaceLimitIntent) -> OrderResult:
        """Retry-queue choke point: re-apply C1/C2/C3 before re-submit.

        The first attempt already ran qty-resolve, dirty refresh, and
        ``_is_good_to_place`` via ``_execute_place_intent``; retries reuse the
        assigned wire ``order_link_id`` and only need a fresh cap read plus
        executor submit + tracking bookkeeping.

        Re-checks the 110017 truncate breaker (Step 2 on first dispatch): a
        queued place can outlive a breaker trip on the same ``(side, price)``
        scope, so retries must honor the cooldown or they partially bypass the
        0064 storm backstop.
        """
        now = self._clock()
        if self._truncate_breaker.is_blocked(intent.side, intent.price, now):
            logger.debug(
                "%s: 110017 breaker tripped — dropping retry %s @ %s",
                self.strat_id, intent.side, intent.price,
            )
            return OrderResult(
                success=False,
                order_link_id=intent.order_link_id,
                error="truncate_breaker_blocked",
            )

        limits = self.get_limit_orders()
        allowed, reason = self._evaluate_safety_caps(intent, limits)
        if not allowed:
            return OrderResult(
                success=False,
                order_link_id=intent.order_link_id,
                error=f"safety_cap_{reason}",
            )

        if not self._is_good_to_place(intent, limits):
            logger.warning(
                "%s: duplicate order on retry %s %s @ %s — dropping (not re-backed-off)",
                self.strat_id, intent.direction, intent.side, intent.price,
            )
            return OrderResult(
                success=False,
                order_link_id=intent.order_link_id,
                error="duplicate_order_blocked",
            )

        result = self._executor.execute_place(intent)
        if result.success:
            tracked = self._tracked_orders.get(intent.client_order_id)
            if tracked is not None:
                tracked.mark_placed(result.order_id)
            self._truncate_breaker.record_success(intent.side, intent.price)
            if intent.reduce_only:
                self._clear_dirty(intent.direction)
        return result

    def _execute_cancel_intent(self, intent: CancelIntent) -> None:
        """Execute a cancel order intent."""
        result = self._executor.execute_cancel(intent)

        tracked = self._find_tracked_order(None, intent.order_id)
        if tracked and result.success:
            tracked.mark_cancelled()

        if not result.success and self._on_intent_failed:
            self._on_intent_failed(intent, result.error)

    def _emit_safety_cap_rejection(self, intent: PlaceLimitIntent, reason: str) -> None:
        """Log (throttled) + alert a safety-cap rejection (feature 0079).

        The WARNING is throttled per reason (``_SAFETY_CAP_WARN_THROTTLE_SEC``)
        so a cap pinned at its threshold does not flood the log every tick; the
        Telegram alert is throttled by the notifier via ``error_key``. The
        caller drops the intent (does NOT enqueue it).
        """
        now = self._clock()
        last = self._safety_cap_warn_last.get(reason)
        if last is None or (now - last) >= _SAFETY_CAP_WARN_THROTTLE_SEC:
            self._safety_cap_warn_last[reason] = now
            logger.warning(
                "%s: safety cap '%s' rejecting %s %s @ %s (not enqueued)",
                self.strat_id, reason, intent.direction, intent.side, intent.price,
            )
            if self._notifier is not None:
                self._notifier.alert(
                    f"Gridbot: safety cap '{reason}' tripped for {self.strat_id}",
                    error_key=f"safety_cap_{reason}_{self.strat_id}",
                )
        else:
            logger.debug(
                "%s: safety cap '%s' rejecting %s @ %s (throttled)",
                self.strat_id, reason, intent.side, intent.price,
            )

    @staticmethod
    def _cur_realized_pnl_from_raw(position_data: Optional[dict]) -> Decimal:
        """Read Bybit ``curRealisedPnl`` from a raw position payload.

        Used by the C3 loss breaker independently of ``_build_position_state``,
        which returns ``None`` when ``size == 0``. A closing fill often arrives
        only on that flat payload, so the breaker must not discard it.
        """
        if not position_data:
            return Decimal("0")
        raw = position_data.get("curRealisedPnl")
        if raw in (None, ""):
            return Decimal("0")
        try:
            return Decimal(str(raw))
        except (TypeError, ValueError, InvalidOperation):
            return Decimal("0")

    def _evaluate_loss_breaker(
        self,
        long_position: Optional[dict],
        short_position: Optional[dict],
    ) -> None:
        """C3 — evaluate the session realized-loss circuit breaker (feature 0079).

        Uses the per-cycle "Realized" value (``curRealisedPnl`` — the Bybit UI
        Realized column), NOT the ~80x lifetime ``cumRealisedPnl``. On a NEW
        trip: emit one ERROR + alert and cancel ALL working orders for the
        symbol via ``_execute_cancel_intent`` (honors shadow mode + tracked-order
        state). After the trip, every ``_execute_place_intent`` short-circuits
        via ``loss_tripped()``. No-op when no SafetyCaps is wired.
        """
        if self._safety_caps is None:
            return
        session_realized_pnl = (
            self._cur_realized_pnl_from_raw(long_position)
            + self._cur_realized_pnl_from_raw(short_position)
        )
        newly_tripped = self._safety_caps.check_loss_breaker(
            session_realized_pnl=session_realized_pnl,
            now_utc=datetime.now(UTC),
        )
        if not newly_tripped:
            return
        logger.error(
            "%s: SAFETY CAP session-loss breaker TRIPPED — session realized "
            "PnL %s reached the configured loss limit; cancelling working "
            "orders and suppressing new places",
            self.strat_id, session_realized_pnl,
        )
        if self._notifier is not None:
            self._notifier.alert(
                f"Gridbot: safety-cap session-loss breaker tripped for "
                f"{self.strat_id} (session realized {session_realized_pnl})",
                error_key=f"safety_cap_loss_breaker_{self.strat_id}",
            )
        # Drain queued placement retries — stale intents would bypass the latch
        # via RetryQueue.process_due (mirrors auth-cooldown queue clear).
        if self._on_retry_queue_clear is not None:
            cleared = self._on_retry_queue_clear()
            if cleared:
                logger.info(
                    "%s: Cleared %d items from retry queue on session-loss trip",
                    self.strat_id, cleared,
                )
        # Cancel all working orders (both directions). order['orderId'] is the
        # wire id (get_limit_orders maps tracked.order_id → 'orderId'); a
        # CancelIntent requires symbol + order_id + reason.
        limits = self.get_limit_orders()
        for side_orders in limits.values():
            for order in side_orders:
                self._execute_cancel_intent(
                    CancelIntent(
                        symbol=self._config.symbol,
                        order_id=order["orderId"],
                        reason="safety_cap_loss_breaker",
                    )
                )

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
                strat_id=self._config.strat_id,
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

            # Feature 0080 migration: a failed ambiguous place is keyed by the
            # salted client_order_id, but the exchange order still carries the
            # pre-0080 wire prefix. Upgrade by order identity and re-key to the
            # exchange prefix so WS events continue to resolve.
            failed_key, failed_tracked = self._find_failed_tracked_by_order_identity(
                dec_price, dec_qty, side, reduce_only
            )
            if failed_tracked is not None and failed_key is not None:
                if failed_tracked.intent is None:
                    logger.warning(
                        f"{self.strat_id}: identity-matched upgrade skipped: "
                        f"tracked order has no intent (prefix={failed_key})"
                    )
                    continue

                exchange_link_id = (
                    order_link_id or failed_tracked.intent.order_link_id
                )
                upgraded_intent = replace(
                    failed_tracked.intent,
                    order_link_id=exchange_link_id,
                )
                prev_state = failed_tracked.status
                failed_tracked.intent = upgraded_intent
                failed_tracked.mark_placed(order_id)
                if failed_key != client_id:
                    del self._tracked_orders[failed_key]
                    failed_tracked.client_order_id = client_id
                    self._tracked_orders[client_id] = failed_tracked
                removed = (
                    self._on_retry_cancel_for_prefix(failed_key)
                    if self._on_retry_cancel_for_prefix
                    else 0
                )
                logger.info(
                    f"{self.strat_id}: tracked order upgraded from {prev_state} "
                    f"via reconcile identity match "
                    f"(failed_prefix={failed_key}, exchange_prefix={client_id}, "
                    f"order_id={order_id}, link_id={exchange_link_id}, "
                    f"retry_cancelled={removed})"
                )
                injected += 1
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

    def _seen_exec_id(self, exec_id: str) -> bool:
        """Record an execution identity; return True if already processed.

        Feature 0083 (issue #202): dedup guard for WS resync redelivery
        bursts. Bybit exec_ids are globally unique, so entries never expire
        by time — memory is bounded by FIFO eviction at
        ``_EXEC_DEDUP_MAX_ENTRIES``. A repeat lookup does not refresh the
        entry's position; pure insertion order keeps eviction deterministic.
        Recording on first sight (before the event is processed) is
        intentional: if processing throws after recording, a redelivery of
        the same exec_id is deduped rather than retried — correct for the
        redelivery-burst failure mode this guard targets.

        Args:
            exec_id: Exchange execution identity; empty string is never
                deduped (missing identity falls through to processing).

        Returns:
            True if this exec_id was seen before (caller should drop).
        """
        if not exec_id:
            return False
        if exec_id in self._processed_exec_ids:
            return True
        self._processed_exec_ids[exec_id] = None
        while len(self._processed_exec_ids) > _EXEC_DEDUP_MAX_ENTRIES:
            self._processed_exec_ids.popitem(last=False)
        return False

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
