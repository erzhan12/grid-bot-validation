"""Orchestrator for multi-strategy coordination.

The orchestrator is the main entry point for the gridbot. It:
- Loads configuration
- Creates strategy runners
- Manages WebSocket connections
- Routes events to the correct runners
- Creates database Run records
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional
from uuid import UUID

from bybit_adapter.rest_client import BybitRestClient
from bybit_adapter.ws_client import PublicWebSocketClient, PrivateWebSocketClient
from bybit_adapter.normalizer import BybitNormalizer
from grid_db import DatabaseFactory
from grid_db import Run, RunRepository, Strategy, BybitAccount, User
from grid_db.identity import account_id_for, strategy_id_for, user_id_for
from gridcore import (
    DirectionType,
    GridStateStore,
    InstrumentInfo,
    TickerEvent,
)
from gridcore.persistence import grid_fingerprint
from gridcore.intents import CancelIntent

from gridbot.config import GridbotConfig, AccountConfig, StrategyConfig
from gridbot.executor import IntentExecutor
from gridbot.notifier import Notifier
from gridbot.runner import StrategyRunner
from gridbot.safety_caps import SafetyCaps
from gridbot.reconciler import Reconciler
from gridbot.retry_queue import RetryQueue
from gridbot.position_fetcher import PositionFetcher, _POSITION_TICK_BASE
from gridbot.auth_cooldown_manager import AuthCooldownManager
from gridbot.health import (
    HealthMetrics,
    HealthState,
    HealthStatusWriter,
    build_snapshot,
)
from gridbot.writers import GridStateWriter

_HEALTH_CHECK_INTERVAL = 10  # seconds
_WS_HEALTH_CHECK_INTERVAL = 10.0  # seconds — bbu2 ENSURE_SOCKET_INTERVAL parity
_STATUS_WRITE_WARN_THROTTLE = 60.0  # seconds — throttle health status-write error logs (0082)
_CHECK_INTERVAL = 0.1  # 100 ms main loop tick (bbu2 value)
_RETRY_TICK_INTERVAL = 1.0  # seconds between retry-queue drains
_WS_RECONNECT_SLOW_THRESHOLD = 5.0  # log a warning if a single WS disconnect+connect takes longer
_MAX_TICK_BACKOFF = 30.0  # cap (s) on main-loop backoff after consecutive _tick() failures
_UNKNOWN_ORDER_DEBOUNCE_SEC = 2.0  # min interval between WS-triggered fast-track order syncs
_STARTUP_RECONCILE_BACKOFFS = (2.0, 5.0, 10.0)  # sleeps between startup reconcile attempts (0086)


logger = logging.getLogger(__name__)


class StartupReconciliationError(Exception):
    """Raised when startup reconciliation fails after all retries.

    Trading must not start on unconfirmed exchange order state (issue #206).
    main.py catches startup exceptions and returns exit code 1.
    """


def _ensure_utc_aware(dt: datetime) -> datetime:
    """Normalize datetimes for comparison (SQLite may return naive values)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _runner_market_price(runner: StrategyRunner, latest_ticker: TickerEvent | None = None) -> float | None:
    """Return a real market price for position-risk math, never a fabricated zero."""
    last_close = runner.engine.last_close
    if last_close is not None:
        return last_close
    if latest_ticker is None:
        return None
    return float(latest_ticker.last_price)


class Orchestrator:
    """Coordinates multiple strategies across accounts.

    The orchestrator manages the lifecycle of all trading components:
    - Creates REST clients and WebSocket connections per account
    - Creates strategy runners
    - Routes WebSocket events to the correct runners
    - Handles startup reconciliation
    - Creates Run records in the database

    Example:
        config = load_config("conf/gridbot.yaml")
        db = DatabaseFactory(DatabaseSettings())

        orchestrator = Orchestrator(config, db)
        orchestrator.start()
        try:
            orchestrator.run()
        finally:
            orchestrator.stop()
    """

    def __init__(
        self,
        config: GridbotConfig,
        db: Optional[DatabaseFactory] = None,
        anchor_store_path: str = "db/grid_anchor.json",
        notifier: Optional[Notifier] = None,
    ):
        """Initialize orchestrator.

        Args:
            config: Gridbot configuration.
            db: Database factory for persistence (optional).
            anchor_store_path: Path to grid state JSON file. Name preserved
                for deploy-config compatibility; the file now holds full grid
                state, not just anchor prices.
            notifier: Alert notifier (optional, log-only if None).
        """
        self._config = config
        self._db = db
        self._state_store = GridStateStore(anchor_store_path)
        self._notifier = notifier or Notifier()

        # Per-account resources
        self._rest_clients: dict[str, BybitRestClient] = {}
        self._public_ws: dict[str, PublicWebSocketClient] = {}
        self._private_ws: dict[str, PrivateWebSocketClient] = {}
        self._normalizers: dict[str, BybitNormalizer] = {}

        # Runners and supporting components
        self._runners: dict[str, StrategyRunner] = {}  # strat_id -> runner
        self._executors: dict[str, IntentExecutor] = {}  # account_name -> executor
        self._strategy_executors: dict[str, IntentExecutor] = {}  # strat_id -> executor
        self._reconcilers: dict[str, Reconciler] = {}  # account_name -> reconciler
        self._retry_queues: dict[str, RetryQueue] = {}  # strat_id -> queue
        # Feature 0064 — rate-limit the breaker-triggered forced reconcile so a
        # tripped breaker cannot itself spam REST during an outage. strat_id ->
        # monotonic ts of the last forced reconcile.
        self._force_reconcile_last_at: dict[str, float] = {}

        # Feature 0082 (issue #185) — operational observability. HealthMetrics is
        # the shared process-lifetime counter set (passed by-ref into each executor);
        # _safety_caps mirrors the per-strat caps so the sweep can read
        # loss_tripped()/rate_limited(); the writer emits the JSON status snapshot.
        self._health_metrics = HealthMetrics()
        self._health_writer = HealthStatusWriter(
            self._config.status_file_path,
            enabled=self._config.status_file_enabled,
        )
        self._safety_caps: dict[str, SafetyCaps] = {}  # strat_id -> caps (health sweep)
        # strat_id -> dirty-REST count observed at the previous sweep, so degraded
        # keys off a RECENT delta (not the sticky monotonic absolute) — review #195.
        self._dirty_rest_last_count: dict[str, int] = {}
        self._start_time: Optional[float] = None
        self._status_write_warn_last: float = 0.0

        # Feature 0069 (issue #151) — state-divergence detector. Logic lives on
        # the orchestrator (no separate class). All four signals converge on
        # _trigger_divergence_reconcile -> _force_reconcile_strat(direction=None).
        # Detector throttle (≥5 min default), SEPARATE from the breaker cooldown:
        # strat_id -> monotonic ts of the last divergence-driven forced reconcile.
        self._divergence_last_fire_at: dict[str, float] = {}
        # Signal 2 edge-tracking: strat_id -> the breaker reconcile-count value
        # we last fired on (fire once per new exhaustion edge, not every tick).
        self._divergence_budget_last_fired: dict[str, int] = {}
        # Signal 4 — strats pending a post-WS-recovery forced reconcile. Mutated
        # from the WS heartbeat thread (_on_ws_disconnect) AND the main loop, so
        # guarded by a dedicated lock; drained (swap-and-clear) once per _tick.
        self._pending_post_recovery_reconcile: set[str] = set()
        self._pending_post_recovery_lock = threading.Lock()
        # Signal 3 — next REST-vs-local size-delta sweep (primed with a phase
        # offset vs _next_order_sync in start() so the two sweeps do not co-fire).
        self._next_divergence_size_check: float = 0.0

        # Run tracking
        self._run_ids: dict[str, UUID] = {}  # strat_id -> run_id
        self._run_start_ts: dict[str, datetime] = {}  # strat_id -> Run.start_ts

        # 0047: parallel DB grid-state writer. Constructed only when a DB
        # is configured — standalone / no-DB runs (where _create_run_records
        # early-returns) get a None writer. Background thread starts in
        # start() (after _create_run_records populates _run_ids). The
        # run_id_provider closure reads _run_ids lazily at write time;
        # bootstrap-window writes (before _create_run_records) return None
        # and are dropped by the writer with an INFO log.
        if self._db is not None:
            self._grid_state_writer: Optional[GridStateWriter] = GridStateWriter(
                db=self._db,
                run_id_provider=lambda strat_id: (
                    str(self._run_ids[strat_id])
                    if strat_id in self._run_ids
                    else None
                ),
            )
        else:
            self._grid_state_writer = None

        # Event routing maps
        self._symbol_to_runners: dict[str, list[StrategyRunner]] = {}  # symbol -> runners
        self._account_to_runners: dict[str, list[StrategyRunner]] = {}  # account_name -> runners

        # State
        # `_started`: set once start() completes successfully; cleared by
        # stop(). Guards stop() against "never started" and against double
        # stop. Independent of `_running` because the main loop may have
        # already exited (via request_stop()) before stop() is called from
        # the finally block in main.py.
        # `_running`: main-loop gate. Flipped by request_stop() from a
        # signal handler or by stop() at teardown.
        self._started = False
        self._running = False

        # Coalesced WS position snapshot (feature 0023).
        # Outer key = account_name, inner key = symbol, value = dict with
        # "long" / "short" position dicts and a monotonic "seq". The WS
        # callback (`_on_position`) writes the latest snapshot here; the
        # main-loop tick drains it once per (account, symbol). Seq lives
        # in `_position_seq` and is bumped under the WS thread.
        self._latest_position: dict[str, dict[str, dict]] = {}
        self._last_processed_position_seq: dict[tuple[str, str], int] = {}
        self._position_seq: int = 0

        # Position-fetch subsystem (WS cache + REST fallback + wallet cache
        # + startup batch + rotation tick). Constructed eagerly so that
        # _init_account — which registers WS callbacks bound to
        # self._position_fetcher — sees a real instance, and so that
        # tests can exercise fetch logic without calling start(). The
        # fetcher holds _rest_clients / _account_to_runners by reference
        # (they are empty dicts at this point and get populated in place
        # by _init_account / _init_strategy).
        #
        # `on_position_changed=self._on_position` rewires the WS-thread
        # callback so a snapshot lands in `_latest_position` for the next
        # main-loop drain (feature 0023). Method-bound reference is fine —
        # `_on_position` is defined on the class, available before any WS
        # message can fire (sockets only connect inside start()).
        self._position_fetcher = PositionFetcher(
            rest_clients=self._rest_clients,
            account_to_runners=self._account_to_runners,
            notifier=self._notifier,
            wallet_cache_interval=self._config.wallet_cache_interval,
            position_check_interval=self._config.position_check_interval,
            on_position_changed=self._on_position,
            wallet_ws_max_age_seconds=self._config.wallet_ws_max_age_seconds,
        )

        # Auth-cooldown subsystem. Constructed eagerly for the same reason
        # as _position_fetcher — _init_strategy registers an executor
        # callback bound to this instance, and _strategy_executors /
        # _retry_queues are held by reference (empty now, populated in
        # place by _init_strategy).
        self._auth_cooldown = AuthCooldownManager(
            strategy_executors=self._strategy_executors,
            retry_queues=self._retry_queues,
            notifier=self._notifier,
            cooldown_minutes=self._config.auth_cooldown_minutes,
        )

        # WS → main-thread buffers.
        #
        # Memory model: CPython's GIL serialises bytecode execution, so a
        # dict/deque mutation in one thread and a read in another never
        # tear — the reader sees either the prior value or the newer write,
        # never a partially-updated state. Under CPython this is sufficient
        # without explicit locks or memory barriers. Caveat: free-threaded
        # CPython (PEP 703 / 3.13t) removes the GIL; if we ever migrate to
        # a GIL-less interpreter, these buffers need explicit synchronisation
        # (lock or lock-free atomic).
        #
        # Ticker: latest-wins cache. Older events coalesce away — only the
        # freshest value per symbol ever matters.
        self._latest_ticker: dict[str, TickerEvent] = {}
        self._last_processed_ticker: dict[str, TickerEvent] = {}
        # Per-runner pending execution/order events.
        self._pending_executions: dict[str, deque] = {}
        self._pending_orders: dict[str, deque] = {}

        # Periodic-tick schedulers (bbu2 timestamp-gating pattern).
        self._next_position_check: float = 0.0
        self._next_health_check: float = 0.0
        self._next_ws_health_check: float = 0.0
        self._next_order_sync: float = 0.0
        self._next_retry_tick: float = 0.0

        # Debounce window for WS-triggered fast-track order syncs. Bursts of
        # untracked-order WS events coalesce into a single reconciliation sweep.
        self._unknown_order_debounce_until: float = 0.0

    @property
    def running(self) -> bool:
        """Whether orchestrator is running."""
        return self._running

    def start(self) -> None:
        """Start the orchestrator (non-blocking initialization).

        This initializes all components:
        1. Creates REST clients per account
        2. Creates executors and reconcilers
        3. Creates strategy runners
        4. Performs startup reconciliation
        5. Creates database Run records
        6. Connects WebSocket streams
        7. Fetches initial positions via REST API
        8. Sets _running=True so run() can proceed.

        After start() returns, call run() to enter the blocking polling
        loop, or stop() to tear everything down.

        Raises:
            StartupReconciliationError: startup reconciliation failed after
                all retries (fail closed, issue #206) — no orders were
                placed; main.py converts this into exit code 1.
        """
        if self._running:
            return

        logger.info("Starting orchestrator")

        # Initialize per-account resources
        for account_config in self._config.accounts:
            self._init_account(account_config)

        # Initialize strategies
        for strategy_config in self._config.strategies:
            self._init_strategy(strategy_config)

        # Build routing maps
        self._build_routing_maps()

        # Seed per-runner event buffers
        for strat_id in self._runners:
            self._pending_executions[strat_id] = deque()
            self._pending_orders[strat_id] = deque()

        # Perform startup reconciliation. Fail closed: if open-order state
        # cannot be confirmed after retries, abort startup (issue #206).
        for runner in self._runners.values():
            account_name = self._get_account_for_strategy(runner.strat_id)
            reconciler = self._reconcilers.get(account_name)
            if reconciler:
                self._reconcile_startup_with_retry(runner, reconciler)

        # Create database Run records (populates _run_ids)
        self._create_run_records()

        # 0047: ensure the new ``grid_state_snapshots`` table exists on
        # pre-0047 production DBs. ``Base.metadata.create_all`` is
        # idempotent — only missing tables get created — and this project
        # provisions schema this way (no Alembic). Without this, deploys
        # would need an out-of-band ``python -m grid_db.init_db`` step or
        # the loader's ``has_table`` fallback would suppress every write.
        if self._db is not None:
            try:
                self._db.create_tables()
            except Exception as e:
                logger.warning(
                    "Schema provisioning failed (continuing without DB grid-state writer): %s",
                    e,
                )

        # 0047: start the DB grid-state writer's worker thread now that
        # _run_ids is populated; bootstrap-window writes (between runner
        # construction and this point) were dropped by the writer's
        # provider-returns-None guard. Immediately probe and write an
        # initial snapshot per built grid (issue #108); failures alert but
        # do not block startup.
        if self._grid_state_writer is not None:
            self._grid_state_writer.start()
            self._bootstrap_grid_snapshots()

        # Connect WebSocket streams (pybit internal threads start here)
        for account_name in self._public_ws:
            self._connect_websockets(account_name)

        # Initial position fetch so runners have multipliers before first ticker
        logger.info("Fetching initial positions before entering main loop")
        self._position_fetcher.fetch_and_update(startup=True)

        # Prime the coalesced-position seq map so the first main-loop tick
        # does not redispatch snapshots already covered by the startup
        # REST batch (feature 0023).
        self._prime_position_seq()

        # Prime periodic-tick schedulers so the first main loop iteration
        # does not immediately re-run expensive checks.
        now = time.monotonic()
        self._next_position_check = now + _POSITION_TICK_BASE
        self._next_health_check = now + _HEALTH_CHECK_INTERVAL
        self._next_ws_health_check = now + _WS_HEALTH_CHECK_INTERVAL
        self._next_order_sync = now  # order sync runs immediately on first tick
        self._next_retry_tick = now + _RETRY_TICK_INTERVAL
        # Feature 0069 signal 3 — phase-offset half an interval ahead of
        # _next_order_sync (which fires this tick) so the two periodic REST sweeps
        # do not co-fire and produce a synchronized burst.
        self._next_divergence_size_check = (
            now + self._divergence_size_check_interval() / 2.0
        )

        self._running = True
        self._started = True
        # Feature 0082 — uptime origin + emit the initial `starting` snapshot
        # before run() enters the blocking poll loop.
        self._start_time = time.monotonic()
        self._write_health_snapshot(overall=HealthState.STARTING)
        logger.info(f"Orchestrator started with {len(self._runners)} strategies")

    def _reconcile_startup_with_retry(
        self, runner: StrategyRunner, reconciler: Reconciler
    ) -> None:
        """Run startup reconciliation, retrying transient failures in place.

        One initial attempt plus one retry per _STARTUP_RECONCILE_BACKOFFS
        entry. On exhaustion, alert and raise StartupReconciliationError so
        start() aborts before any order is placed (issue #206). Sleeping here
        is safe: the main loop is not running yet and WebSockets are not
        connected.
        """
        attempts = 1 + len(_STARTUP_RECONCILE_BACKOFFS)
        for attempt in range(1, attempts + 1):
            result = reconciler.reconcile_startup(runner)
            if not result.errors:
                if attempt > 1:
                    logger.warning(
                        "%s: Startup reconciliation recovered on attempt %d/%d",
                        runner.strat_id, attempt, attempts,
                    )
                logger.info(
                    f"{runner.strat_id}: Reconciliation complete - "
                    f"fetched={result.orders_fetched}, injected={result.orders_injected}, "
                    f"untracked={result.untracked_orders_on_exchange}"
                )
                return
            if attempt < attempts:
                backoff = _STARTUP_RECONCILE_BACKOFFS[attempt - 1]
                logger.warning(
                    "%s: Startup reconciliation attempt %d/%d failed (%s), "
                    "retrying in %.0fs",
                    runner.strat_id, attempt, attempts, result.errors, backoff,
                )
                time.sleep(backoff)

        last_error = result.errors[-1]
        self._notifier.alert(
            f"Gridbot: startup reconciliation failed for {runner.strat_id} "
            f"after {attempts} attempts - {last_error}. Aborting startup, "
            f"no orders placed.",
            error_key=f"startup_reconcile_{runner.strat_id}",
        )
        raise StartupReconciliationError(
            f"{runner.strat_id}: startup reconciliation failed after "
            f"{attempts} attempts: {last_error}"
        )

    def run(self) -> None:
        """Main polling loop. Blocks until request_stop() / stop().

        On a successful _tick() we sleep the normal _CHECK_INTERVAL (100 ms).
        On a failure we escalate sleep exponentially (1 s → 2 s → 4 s → …) up
        to _MAX_TICK_BACKOFF, then hold at the cap until a tick succeeds. The
        bot never stops — sustained failures keep retrying at the capped
        interval, because the root cause may be exchange-side and self-heal.

        Routine per-event runner errors (on_execution / on_order_update /
        on_ticker) and periodic REST-check failures (_fetch_and_update_positions
        / _health_check_once / _order_sync_once / retry-queue tick) are caught
        and alerted inside _tick() and do NOT feed this backoff. Only genuinely
        unexpected exceptions that escape _tick() escalate sleep here.
        """
        logger.info("Orchestrator main loop started")
        consecutive_failures = 0
        while self._running:
            try:
                self._tick()
                consecutive_failures = 0
                sleep_for = _CHECK_INTERVAL
            except Exception as e:
                consecutive_failures += 1
                sleep_for = min(2 ** (consecutive_failures - 1), _MAX_TICK_BACKOFF)
                logger.error(
                    "Main loop tick error (#%d consecutive, %s, sleeping %.1fs): %s",
                    consecutive_failures, type(e).__name__, sleep_for, e, exc_info=True,
                )
                self._notifier.alert_exception("main_loop", e, error_key="main_loop")
            time.sleep(sleep_for)
        logger.info("Orchestrator main loop exited")

    def _tick(self) -> None:
        """Single iteration of the main polling loop.

        Drains pending events, processes the latest ticker per symbol, and
        runs time-gated periodic checks. Exceptions are caught one level up
        in run() so one bad iteration cannot kill the loop.
        """
        # 1. Drain pending execution events
        for runner in self._runners.values():
            dq = self._pending_executions.get(runner.strat_id)
            if not dq:
                continue
            while True:
                try:
                    event = dq.popleft()
                except IndexError:
                    break
                try:
                    runner.on_execution(event)
                except Exception as e:
                    logger.error(
                        "%s: on_execution error: %s",
                        runner.strat_id, e, exc_info=True,
                    )
                    self._notifier.alert_exception(
                        "runner.on_execution", e,
                        error_key=f"on_execution_{runner.strat_id}",
                    )

        # 2. Drain pending order-update events
        for runner in self._runners.values():
            dq = self._pending_orders.get(runner.strat_id)
            if not dq:
                continue
            while True:
                try:
                    event = dq.popleft()
                except IndexError:
                    break
                try:
                    runner.on_order_update(event)
                except Exception as e:
                    logger.error(
                        "%s: on_order_update error: %s",
                        runner.strat_id, e, exc_info=True,
                    )
                    self._notifier.alert_exception(
                        "runner.on_order_update", e,
                        error_key=f"on_order_update_{runner.strat_id}",
                    )

        # 2.5 Drain coalesced WS position snapshots (feature 0023).
        #     One dispatch per (account, symbol) per tick, deduped via the
        #     monotonic seq counter set in `_on_position`. Older snapshots
        #     overwrite each other in `_latest_position`; only the freshest
        #     is dispatched. Runs BEFORE the ticker drain so `_check_and_place`
        #     reasons over fresh position size on the same tick.
        for account_name, runners in self._account_to_runners.items():
            account_slot = self._latest_position.get(account_name)
            if not account_slot:
                continue
            wallet = self._position_fetcher.get_wallet_snapshot(account_name)
            wallet_balance = wallet.wallet_balance
            for runner in runners:
                snapshot = account_slot.get(runner.symbol)
                if snapshot is None:
                    continue
                seq = snapshot["seq"]
                key = (account_name, runner.symbol)
                if self._last_processed_position_seq.get(key) == seq:
                    continue
                self._last_processed_position_seq[key] = seq
                try:
                    runner.on_position_update(
                        long_position=snapshot["long"],
                        short_position=snapshot["short"],
                        wallet_balance=wallet_balance,
                        last_close=_runner_market_price(
                            runner, self._latest_ticker.get(runner.symbol)
                        ),
                        available_balance=wallet.available_balance,
                        total_available_balance=wallet.total_available_balance,
                        total_maintenance_margin=wallet.total_maintenance_margin,
                    )
                except Exception as e:
                    logger.error(
                        "%s: on_position_update error (WS drain): %s",
                        runner.strat_id, e, exc_info=True,
                    )
                    self._notifier.alert_exception(
                        "runner.on_position_update", e,
                        error_key=f"on_position_update_{runner.strat_id}",
                    )

        # 3. Process latest ticker per symbol (coalesced — WS callback
        #    overwrites older events, so only the freshest is processed).
        #    Identity check (`is`) relies on normalize_ticker() returning a
        #    fresh TickerEvent per message; if that ever changes (e.g.
        #    pooling/caching), switch to a monotonic seq counter written
        #    in _on_ticker.
        for symbol, runners in self._symbol_to_runners.items():
            event = self._latest_ticker.get(symbol)
            if event is None or event is self._last_processed_ticker.get(symbol):
                continue
            self._last_processed_ticker[symbol] = event
            for runner in runners:
                try:
                    runner.on_ticker(event)
                except Exception as e:
                    logger.error(
                        "%s: on_ticker error: %s",
                        runner.strat_id, e, exc_info=True,
                    )
                    self._notifier.alert_exception(
                        "runner.on_ticker", e,
                        error_key=f"on_ticker_{runner.strat_id}",
                    )

        # 4. Periodic checks via timestamp gating (bbu2 check_job pattern).
        #    Each check is wrapped so that one flaky REST call cannot wedge
        #    WS-event drain (sections 1-3 above) via the outer backoff path.
        #    The _next_* timestamp is advanced BEFORE the call so a
        #    persistently failing check does not retry every tick.
        now = time.monotonic()
        if now >= self._next_position_check:
            self._next_position_check = now + _POSITION_TICK_BASE
            try:
                self._position_fetcher.fetch_and_update()
            except Exception as e:
                logger.error(
                    "Periodic check failed (_fetch_and_update_positions): %s",
                    e, exc_info=True,
                )
                self._notifier.alert_exception(
                    "_fetch_and_update_positions", e,
                    error_key="periodic_fetch_positions",
                )
        if now >= self._next_health_check:
            self._next_health_check = now + _HEALTH_CHECK_INTERVAL
            try:
                self._health_check_once()
            except Exception as e:
                logger.error(
                    "Periodic check failed (_health_check_once): %s",
                    e, exc_info=True,
                )
                self._notifier.alert_exception(
                    "_health_check_once", e,
                    error_key="periodic_health_check",
                )
        if now >= self._next_ws_health_check:
            self._next_ws_health_check = now + _WS_HEALTH_CHECK_INTERVAL
            try:
                self._ws_health_check_once()
            except Exception as e:
                logger.error(
                    "Periodic check failed (_ws_health_check_once): %s",
                    e, exc_info=True,
                )
                self._notifier.alert_exception(
                    "_ws_health_check_once", e,
                    error_key="periodic_ws_health_check",
                )
        # Feature 0069 signal 4 — drain post-WS-recovery forced reconciles.
        # PINNED here: after the per-tick WS event drains (steps 1/2/2.5) so no
        # order-update event is adjudicated against a half-cleared dedup cache,
        # and IMMEDIATELY BEFORE the order-sync gate (sharing this tick's `now`)
        # so a fast-tracked _next_order_sync is consumed THIS tick. Swap-and-clear
        # under the lock, then iterate the captured set OUTSIDE the lock. The
        # drain NEVER re-adds: a strat whose reconcile is suppressed (detector
        # throttle or breaker cooldown) is DROPPED, not retried (position size is
        # backstopped by signal 3, order-level by the periodic order-sync).
        with self._pending_post_recovery_lock:
            pending_recovery = self._pending_post_recovery_reconcile
            self._pending_post_recovery_reconcile = set()
        for strat_id in pending_recovery:
            cfg = next(
                (c for c in self._config.strategies if c.strat_id == strat_id),
                None,
            )
            # Fast-track the order-sync (same tick) when the reconcile WOULD be
            # throttle-suppressed AND order-sync is enabled — shrinking the
            # order-level backstop from up to order_sync_interval to this tick.
            # Read-only peek (does NOT bump the throttle); a NON-suppressed strat
            # reconciles directly below and needs no fast-track.
            if cfg is not None and self._config.order_sync_interval > 0:
                last = self._divergence_last_fire_at.get(strat_id, 0.0)
                if (now - last) < cfg.divergence_reconcile_min_interval_seconds:
                    self._next_order_sync = 0.0
            self._trigger_divergence_reconcile(
                strat_id, "post_ws_recovery", "private_ws_recovery", direction=None,
            )
        if (
            self._config.order_sync_interval > 0
            and now >= self._next_order_sync
        ):
            self._next_order_sync = now + self._config.order_sync_interval
            try:
                self._order_sync_once()
            except Exception as e:
                logger.error(
                    "Periodic check failed (_order_sync_once): %s",
                    e, exc_info=True,
                )
                self._notifier.alert_exception(
                    "_order_sync_once", e,
                    error_key="periodic_order_sync",
                )
        # Feature 0069 signal 3 — periodic REST-vs-local position-size delta sweep.
        if now >= self._next_divergence_size_check:
            self._next_divergence_size_check = (
                now + self._divergence_size_check_interval()
            )
            try:
                self._divergence_size_check_once()
            except Exception as e:
                logger.error(
                    "Periodic check failed (_divergence_size_check_once): %s",
                    e, exc_info=True,
                )
                self._notifier.alert_exception(
                    "_divergence_size_check_once", e,
                    error_key="periodic_divergence_size_check",
                )
        if now >= self._next_retry_tick:
            for rq in self._retry_queues.values():
                try:
                    rq.process_due()
                except Exception as e:
                    logger.error("Retry queue tick error: %s", e, exc_info=True)
                    self._notifier.alert_exception(
                        "retry_queue.process_due", e, error_key="retry_queue_tick",
                    )
            self._next_retry_tick = now + _RETRY_TICK_INTERVAL

    def request_stop(self) -> None:
        """Signal the main loop to exit. Safe to call from any thread.

        Thread-safety model: `self._running = False` is a single
        attribute assignment, which is atomic under the CPython GIL.
        There is no memory barrier, but none is needed — the main
        loop reads `self._running` at the top of every iteration
        (every `_CHECK_INTERVAL` = 100 ms), so the worst case is
        one extra tick of delay before the loop notices the flag.

        `_running` is used ONLY as a loop gate; it does not guard any
        critical section, protect any invariant across multiple reads,
        or participate in any happens-before relationship with other
        shared state. That is why a plain bool is sufficient and a
        `threading.Event` is unnecessary here. If in the future
        `_running` starts gating shared-state transitions (e.g. "once
        _running is False, no thread may touch X"), switch to
        `threading.Event` at that point to get an explicit barrier.
        Do NOT preemptively add a lock or Event — it would just add
        contention to the hot main loop for no benefit today.

        Idempotent: calling this after the loop has already exited is
        a harmless no-op.
        """
        self._running = False

    def stop(self) -> None:
        """Stop the orchestrator gracefully (non-blocking).

        Idempotent and safe to call whether or not the main loop is
        still running. Gated on `_started` (not `_running`) because
        the signal-handler path calls request_stop() first — which
        clears _running — and only then reaches this method via the
        `finally` block in main.py. If we gated on _running we would
        skip WS disconnect and DB run-record cleanup on every normal
        shutdown.
        """
        if not self._started:
            # Never started, or already stopped. Double-stop and
            # stop-before-start are both no-ops.
            return

        logger.info("Stopping orchestrator")
        # Clear _started first so re-entry is a no-op, then flip
        # _running to be extra-safe for any concurrent run() loop that
        # may still be alive (request_stop() usually gets here first).
        self._started = False
        self._running = False

        # Disconnect WebSockets first to stop new events from flowing in
        for ws in self._public_ws.values():
            ws.disconnect()
        for ws in self._private_ws.values():
            ws.disconnect()

        # Retry queues have no background task to stop (see 0017_PLAN.md).

        # Grid state writes are dispatched to daemon threads so the hot path
        # never blocks on disk I/O. On graceful shutdown, wait for any pending
        # writes before the process exits; otherwise the daemon writer can be
        # killed and the latest post-fill grid state is lost. Bound the wait
        # so a stuck writer (slow/dead disk) cannot block stop() forever —
        # losing one save is preferable to a process that won't exit on
        # SIGTERM. 10s matches typical fsync upper bounds on healthy disks.
        self._state_store.flush(timeout=10.0)

        # 0047: drain the parallel DB writer before exit so the last
        # post-fill snapshot(s) sitting in its queue don't get killed
        # along with the daemon thread. Guarded for standalone / no-DB
        # mode where the writer was never constructed.
        if self._grid_state_writer is not None:
            self._grid_state_writer.flush(timeout=10.0)
            self._grid_state_writer.stop()

        # Update Run records
        self._update_run_records_stopped()

        logger.info("Orchestrator stopped")

    def _init_account(self, account_config: AccountConfig) -> None:
        """Initialize resources for an account."""
        name = account_config.name

        # Create REST client
        self._rest_clients[name] = BybitRestClient(
            api_key=account_config.api_key,
            api_secret=account_config.api_secret,
            testnet=account_config.testnet,
            timeout=self._config.rest_fetch_timeout,
        )

        # Create executor
        self._executors[name] = IntentExecutor(
            self._rest_clients[name],
            shadow_mode=False,  # Will be overridden per-strategy
        )

        # Create reconciler
        self._reconcilers[name] = Reconciler(self._rest_clients[name])

        # Create normalizer
        self._normalizers[name] = BybitNormalizer()

        # Collect symbols for this account
        account_symbols = [
            s.symbol for s in self._config.get_strategies_for_account(name)
        ]

        # Create WebSocket clients (but don't connect yet)
        # Callbacks are set at construction time; connect() subscribes automatically.
        self._public_ws[name] = PublicWebSocketClient(
            symbols=account_symbols,
            testnet=account_config.testnet,
            on_ticker=lambda msg, a=name: self._on_ticker(
                a, msg.get("data", {}).get("symbol", ""), msg
            ),
            on_disconnect=lambda ts, a=name: self._on_ws_disconnect(a, "public", ts),
        )
        # Feature 0066 Phase 4: subscribe the real-time `wallet` topic only when
        # the kill-switch is on. PrivateWebSocketClient subscribes wallet_stream
        # solely when on_wallet is set, so None here = no subscription (the full
        # Phase-4 kill-switch — the wallet_provider is also skipped below).
        on_wallet = (
            (lambda msg, a=name: self._position_fetcher.on_wallet_message(a, msg))
            if self._config.wallet_ws_enabled else None
        )
        self._private_ws[name] = PrivateWebSocketClient(
            api_key=account_config.api_key,
            api_secret=account_config.api_secret,
            testnet=account_config.testnet,
            on_position=lambda msg, a=name: self._position_fetcher.on_position_message(a, msg),
            on_order=lambda msg, a=name: self._on_order(a, msg),
            on_execution=lambda msg, a=name: self._on_execution(a, msg),
            on_wallet=on_wallet,
            on_disconnect=lambda ts, a=name: self._on_ws_disconnect(a, "private", ts),
            message_gap_watchdog_enabled=False,
        )

        logger.info(f"Initialized account: {name}")

    def _init_strategy(self, strategy_config: StrategyConfig) -> None:
        """Initialize a strategy runner."""
        strat_id = strategy_config.strat_id
        account_name = strategy_config.account

        # Feature 0079 (issue #182) — build ONE SafetyCaps per strat and pass
        # the SAME instance (and the SAME monotonic clock) to both the executor
        # (C4 rate-limit window) and the runner (C1/C2/C3) so they share one
        # source of truth. Production clock is time.monotonic.
        safety_caps_clock = time.monotonic
        safety_caps = SafetyCaps(
            strategy_config.safety_caps,
            strat_id=strat_id,
            clock=safety_caps_clock,
        )

        # Get executor for this account (with correct shadow mode)
        base_executor = self._executors[account_name]
        executor = IntentExecutor(
            base_executor._client,
            shadow_mode=strategy_config.shadow_mode,
            on_cooldown_entered=lambda sid=strat_id: self._auth_cooldown.enter(sid),
            safety_caps=safety_caps,
            clock=safety_caps_clock,
            health_metrics=self._health_metrics,
        )

        # Create retry queue with dispatcher that routes by intent type.
        # Place retries go through runner.retry_dispatch_place so C1/C2/C3 are
        # re-checked (the executor alone only enforces C4).
        _retry_runner: dict[str, StrategyRunner] = {}

        def _dispatch_intent(intent):
            if isinstance(intent, CancelIntent):
                return executor.execute_cancel(intent)
            return _retry_runner["runner"].retry_dispatch_place(intent)

        retry_queue = RetryQueue(
            executor_func=_dispatch_intent,
            max_attempts=3,
            max_elapsed_seconds=30.0,
            is_paused=lambda: (
                executor.auth_cooldown or safety_caps.loss_tripped()
            ),
        )
        self._retry_queues[strat_id] = retry_queue

        # Fetch instrument info for qty rounding
        instrument_info = self._fetch_instrument_info(
            strategy_config.symbol, account_name
        )

        # Feature 0066 Phase 4: inject the NON-BLOCKING peek_wallet_snapshot
        # reader as the preflight's real-time free-margin source. Skipped (None)
        # when wallet_ws_enabled is off → the runner takes the legacy
        # position-cadence _available_balance path (full Phase-4 kill-switch).
        wallet_provider = (
            (lambda acct=account_name: self._position_fetcher.peek_wallet_snapshot(acct))
            if self._config.wallet_ws_enabled else None
        )

        # Create runner
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
            account_id=account_id_for(account_name),
            instrument_info=instrument_info,
            state_store=self._state_store,
            grid_state_writer=self._grid_state_writer,
            on_intent_failed=lambda intent, error: retry_queue.add(intent, error),
            on_retry_cancel_for_prefix=lambda prefix: retry_queue.cancel_for_prefix(prefix),
            on_retry_queue_clear=retry_queue.clear,
            on_unknown_order=self._request_immediate_order_sync,
            notifier=self._notifier,
            # Feature 0064 — dirty-mirror REST refresh + breaker forced reconcile.
            rest_client=self._rest_clients[account_name],
            on_truncate_breaker_tripped=self._force_reconcile_strat,
            # Feature 0069 (issue #151) — signal 1 placement-failure UNION fire.
            on_divergence_failure_mix=self._trigger_divergence_reconcile,
            # Feature 0066 Phase 4 — real-time wallet preflight source.
            wallet_provider=wallet_provider,
            wallet_ws_max_age_seconds=self._config.wallet_ws_max_age_seconds,
            # Feature 0079 (issue #182) — SAME SafetyCaps instance as the executor.
            safety_caps=safety_caps,
        )
        _retry_runner["runner"] = runner
        self._runners[strat_id] = runner
        self._strategy_executors[strat_id] = executor
        # Feature 0082 — retain caps so _health_check_once can read circuit/C4 state.
        self._safety_caps[strat_id] = safety_caps

        logger.info(
            f"Initialized strategy: {strat_id} (symbol={strategy_config.symbol}, "
            f"shadow={strategy_config.shadow_mode})"
        )

    def _build_routing_maps(self) -> None:
        """Build event routing maps."""
        for strategy_config in self._config.strategies:
            runner = self._runners[strategy_config.strat_id]

            # Symbol -> runners
            symbol = strategy_config.symbol
            if symbol not in self._symbol_to_runners:
                self._symbol_to_runners[symbol] = []
            self._symbol_to_runners[symbol].append(runner)

            # Account -> runners
            account = strategy_config.account
            if account not in self._account_to_runners:
                self._account_to_runners[account] = []
            self._account_to_runners[account].append(runner)

    def _connect_websockets(self, account_name: str) -> None:
        """Connect WebSocket streams for an account.

        Callbacks are already configured at construction time in _init_account.
        connect() subscribes to all streams automatically.
        """
        self._public_ws[account_name].connect()
        self._private_ws[account_name].connect()

        logger.info(f"Connected WebSockets for account: {account_name}")

    def _prime_position_seq(self) -> None:
        """Prime `_last_processed_position_seq` from `_latest_position`.

        Called from `start()` AFTER `fetch_and_update(startup=True)` has
        pushed initial state through `runner.on_position_update`. Without
        this priming, the first `_tick()` would redispatch any WS-cached
        snapshot that the startup REST batch already covered.

        Snapshot via list(...) before iterating: pybit WS threads are
        already running by this point and `_on_position` can insert new
        entries into `_latest_position` (or its inner dicts) concurrently.
        Iterating the live dict could otherwise raise
        `RuntimeError: dictionary changed size during iteration`. Worst
        case with the snapshot: a WS-thread insert that lands AFTER we
        took the list() but BEFORE the first `_tick()` runs is correctly
        NOT primed here, so the first tick will dispatch it — that's
        acceptable because it represents data the startup REST batch
        didn't see.
        """
        for account_name, account_slot in list(self._latest_position.items()):
            for symbol, snapshot in list(account_slot.items()):
                self._last_processed_position_seq[(account_name, symbol)] = snapshot["seq"]

    def _on_position(self, account_name: str, symbol: str) -> None:
        """Handle WS position notification (runs in pybit WS thread).

        Called by `PositionFetcher.on_position_message` AFTER its cache
        write completes, so `get_position_from_ws` returns the freshest
        per-side data here. Builds a paired snapshot (long + short +
        monotonic seq) into `_latest_position[account_name][symbol]`
        for the main-loop drain to pick up — feature 0023.

        Incomplete-cache guard: skips publishing the snapshot if either
        side is missing from the WS cache. Bybit may push an update for
        only the changed side; the opposite side's cache slot can be
        absent until the first push for that side ever lands. Without
        this guard, the drain would call `runner.on_position_update`
        with `long_position=None` (or `short=None`), and runner.py:443-448
        would coerce that to `Decimal('0')`, erroneously zeroing the
        opposite-side size that REST already knew was nonzero. The
        periodic REST cycle keeps working in the meantime — it has its
        own REST fallback for missing WS sides and updates the runner
        directly without going through this drain.

        Thread-safety: per-`(account, symbol)` writes have at most one
        WS-thread writer, since pybit dispatches private-socket callbacks
        from a single thread per account, and `(account, symbol)` is
        partitioned by account. `_position_seq += 1` is shared across
        all private WS threads; with multiple accounts, two concurrent
        increments can collide and produce a duplicate seq value, but
        the resulting snapshots land in different `_latest_position`
        slots and are dispatched independently against per-key entries
        in `_last_processed_position_seq`, so no update is lost. Within
        the same `(account, symbol)`, only one writer exists, so the
        seq comparison in the drain is a clean single-writer monotonic
        check. Reads on the main thread are GIL-atomic.
        """
        try:
            long_pos = self._position_fetcher.get_position_from_ws(
                account_name, symbol, "Buy"
            )
            short_pos = self._position_fetcher.get_position_from_ws(
                account_name, symbol, "Sell"
            )
            if long_pos is None or short_pos is None:
                # Wait for the opposite side to arrive (or for the next
                # periodic REST cycle to refresh runner state). See
                # incomplete-cache guard rationale above.
                return
            self._position_seq += 1
            account_slot = self._latest_position.setdefault(account_name, {})
            account_slot[symbol] = {
                "long": long_pos,
                "short": short_pos,
                "seq": self._position_seq,
            }
        except Exception as e:
            self._notifier.alert_exception("_on_position", e, error_key="ws_on_position")

    def _on_ticker(self, account_name: str, symbol: str, message: dict) -> None:
        """Handle ticker WebSocket message (runs in pybit WS thread).

        Only writes to `_latest_ticker[symbol]`. The main-loop tick picks
        up the freshest event and dispatches it to runners. Older events
        are coalesced away — only the latest ticker ever matters.
        """
        try:
            normalizer = self._normalizers[account_name]
            # Contract: normalize_ticker must return a fresh object per
            # call — the orchestrator's coalescing loop uses `is` to
            # detect unprocessed events.
            event = normalizer.normalize_ticker(message)
            if event is None:
                return
            # See _latest_ticker field declaration for the GIL memory-model
            # rationale. Races with the main loop are benign: worst case we
            # overwrite a value that hasn't been read yet; the reader picks
            # up the newer one on the next iteration.
            self._latest_ticker[symbol] = event
        except Exception as e:
            self._notifier.alert_exception("_on_ticker", e, error_key="ws_on_ticker")

    def _on_order(self, account_name: str, message: dict) -> None:
        """Handle order WebSocket message (runs in pybit WS thread).

        Appends each event to the target runner's pending deque. The
        main-loop tick drains the deque in FIFO order via popleft().

        Thread-safety: `collections.deque.append` is atomic under the
        CPython GIL (single C-level operation), as is `popleft` on the
        main thread. No lock is needed. Races are benign: worst case
        an event queues after the main loop has already drained but
        before it sleeps, and the event is picked up on the next 100 ms
        tick — an acceptable delay. The `_pending_orders.get(...)` read
        is also GIL-atomic; the deque object is created up-front in
        start() and never replaced, so `is not None` is race-free.
        """
        try:
            normalizer = self._normalizers[account_name]
            events = normalizer.normalize_order(message)

            for event in events:
                runners = self._symbol_to_runners.get(event.symbol, [])
                for runner in runners:
                    if self._get_account_for_strategy(runner.strat_id) != account_name:
                        continue
                    dq = self._pending_orders.get(runner.strat_id)
                    if dq is not None:
                        # deque.append is atomic under CPython GIL.
                        dq.append(event)
        except Exception as e:
            self._notifier.alert_exception("_on_order", e, error_key="ws_on_order")

    def _on_execution(self, account_name: str, message: dict) -> None:
        """Handle execution WebSocket message (runs in pybit WS thread).

        Appends each event to the target runner's pending deque. The
        main-loop tick drains the deque in FIFO order via popleft().

        Thread-safety: identical guarantees to `_on_order` —
        `deque.append` is GIL-atomic, the deque object is created
        up-front in start() and never replaced, and races with the
        main-thread drainer cost at most one 100 ms tick of delay.
        """
        try:
            normalizer = self._normalizers[account_name]
            events = normalizer.normalize_execution(message)

            for event in events:
                runners = self._symbol_to_runners.get(event.symbol, [])
                for runner in runners:
                    if self._get_account_for_strategy(runner.strat_id) != account_name:
                        continue
                    dq = self._pending_executions.get(runner.strat_id)
                    if dq is not None:
                        # deque.append is atomic under CPython GIL.
                        dq.append(event)
        except Exception as e:
            self._notifier.alert_exception("_on_execution", e, error_key="ws_on_execution")

    def _fetch_instrument_info(
        self, symbol: str, account_name: str
    ) -> Optional[InstrumentInfo]:
        """Fetch instrument info from Bybit API for qty rounding.

        Uses the account's REST client public endpoint. Returns None if
        fetch fails (qty rounding will be skipped). pybit's HTTP() already
        caps the request at `rest_fetch_timeout` seconds.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT").
            account_name: Account name (for REST client access).

        Returns:
            InstrumentInfo or None if fetch fails.
        """
        try:
            rest_client = self._rest_clients[account_name]
            raw = rest_client.get_instruments_info(symbol)
            info = InstrumentInfo.from_bybit_response(symbol, raw)
            if info is None:
                logger.warning(
                    f"Invalid instrument params from API for {symbol}, "
                    f"will use defaults"
                )
                return None
            logger.info(
                f"Fetched instrument info for {symbol}: "
                f"qty_step={info.qty_step}, tick_size={info.tick_size}"
            )
            return info

        except Exception as e:
            logger.warning(f"Failed to fetch instrument info for {symbol}: {e}")
            return None

    def _health_check_once(self) -> None:
        """Single-shot WebSocket health check + auth-cooldown expiry sweep.

        Extracted from the former _health_check_loop. The main polling loop
        schedules this via timestamp gating every _HEALTH_CHECK_INTERVAL
        seconds.
        """
        try:
            self._auth_cooldown.sweep_expired(datetime.now(UTC))

            # Feature 0064 — surface 110017 breaker trip counts and dirty REST
            # refresh failures without per-occurrence ERROR spam. Monotonic per
            # runner lifetime.
            for runner in self._runners.values():
                trips = runner.truncate_breaker_reconcile_count
                rest_failures = runner.dirty_rest_refresh_failure_count
                if trips > 0 or rest_failures > 0:
                    logger.debug(
                        "%s: 110017 breaker trips=%d, dirty REST refresh failures=%d",
                        runner.strat_id, trips, rest_failures,
                    )

                # Feature 0069 signal 2 — reconciler retry-budget exhaustion.
                # Fire once per NEW edge (count crosses to a value we have not
                # yet fired on), not every tick while parked at the same value.
                cfg = next(
                    (c for c in self._config.strategies
                     if c.strat_id == runner.strat_id),
                    None,
                )
                if cfg is not None and cfg.divergence_detector_enabled:
                    if (
                        trips >= cfg.divergence_retry_budget
                        and trips != self._divergence_budget_last_fired.get(
                            runner.strat_id, -1
                        )
                    ):
                        if self._trigger_divergence_reconcile(
                            runner.strat_id, "retry_budget", trips,
                        ):
                            self._divergence_budget_last_fired[
                                runner.strat_id
                            ] = trips

            for account_name in list(self._public_ws.keys()):
                # Check public WS
                pub_ws = self._public_ws.get(account_name)
                if pub_ws and not pub_ws.is_connected():
                    self._notifier.alert(
                        f"Public WS disconnected for {account_name}, reconnecting",
                        error_key=f"ws_pub_disconnect_{account_name}",
                    )
                    reconnect_start = time.monotonic()
                    try:
                        pub_ws.disconnect()
                        pub_ws.connect()  # re-subscribes automatically via callbacks
                        logger.info(f"Public WS reconnected for {account_name}")
                        self._health_metrics.record_ws_reconnect("public")
                    except Exception as e:
                        self._notifier.alert_exception(
                            f"Public WS reconnect {account_name}", e,
                            error_key=f"ws_pub_reconnect_{account_name}",
                        )
                    finally:
                        elapsed = time.monotonic() - reconnect_start
                        if elapsed > _WS_RECONNECT_SLOW_THRESHOLD:
                            logger.warning(
                                "Public WS reconnect for %s took %.1fs "
                                "(threshold=%.1fs) — blocking main polling loop",
                                account_name, elapsed, _WS_RECONNECT_SLOW_THRESHOLD,
                            )

                # Check private WS
                priv_ws = self._private_ws.get(account_name)
                if priv_ws and not priv_ws.is_connected():
                    self._notifier.alert(
                        f"Private WS disconnected for {account_name}, reconnecting",
                        error_key=f"ws_priv_disconnect_{account_name}",
                    )
                    reconnect_start = time.monotonic()
                    try:
                        priv_ws.disconnect()
                        priv_ws.connect()  # re-subscribes automatically via callbacks
                        logger.info(f"Private WS reconnected for {account_name}")
                        self._health_metrics.record_ws_reconnect("private")
                    except Exception as e:
                        self._notifier.alert_exception(
                            f"Private WS reconnect {account_name}", e,
                            error_key=f"ws_priv_reconnect_{account_name}",
                        )
                    finally:
                        elapsed = time.monotonic() - reconnect_start
                        if elapsed > _WS_RECONNECT_SLOW_THRESHOLD:
                            logger.warning(
                                "Private WS reconnect for %s took %.1fs "
                                "(threshold=%.1fs) — blocking main polling loop",
                                account_name, elapsed, _WS_RECONNECT_SLOW_THRESHOLD,
                            )
                    # Feature 0069 signal 4 — a private socket was found dead and
                    # reconnected; schedule a forced reconcile (drained next _tick).
                    # Private-only — the public branch above does NOT enqueue.
                    self._enqueue_post_recovery_reconcile(account_name)

            # Feature 0082 (issue #185) — emit the health/metrics snapshot at the
            # end of the sweep. The writer is guarded, but the whole sweep is also
            # wrapped, so a status-write failure can never perturb the loop.
            self._write_health_snapshot()
        except Exception as e:
            self._notifier.alert_exception(
                "_health_check_once", e, error_key="health_check_loop",
            )

    def _write_health_snapshot(self, overall: Optional[HealthState] = None) -> None:
        """Build the health/metrics snapshot and write it atomically (feature 0082).

        ``overall`` forces the state (e.g. STARTING before the loop); otherwise the
        worst per-strat state wins. Any failure is logged (throttled) and swallowed
        so a status-write error NEVER perturbs the trading loop.
        """
        try:
            now = time.monotonic()
            uptime = (now - self._start_time) if self._start_time is not None else 0.0
            strat_states: list[dict] = []
            auth_active = 0
            loss_latched = 0
            preflight_skips = 0
            auth_cooldown_cycles = 0
            for strat_id, runner in self._runners.items():
                caps = self._safety_caps.get(strat_id)
                executor = self._strategy_executors.get(strat_id)
                in_cooldown = bool(executor and executor.auth_cooldown)
                circuit = bool(caps and caps.loss_tripped())
                # Degraded keys off a RECENT soft signal, not a sticky absolute:
                # a new dirty-REST failure SINCE THE LAST SWEEP (delta), or a
                # sustained C4 rate-limit. The monotonic count would otherwise pin
                # degraded forever after one transient failure (review #195 P1).
                cur_dirty = runner.dirty_rest_refresh_failure_count
                prev_dirty = self._dirty_rest_last_count.get(strat_id, cur_dirty)
                self._dirty_rest_last_count[strat_id] = cur_dirty
                degraded = bool(
                    (caps and caps.rate_limited(now)) or cur_dirty > prev_dirty
                )
                if circuit:
                    state = HealthState.CIRCUIT_OPEN
                elif in_cooldown:
                    state = HealthState.AUTH_COOLDOWN
                elif degraded:
                    state = HealthState.DEGRADED
                else:
                    state = HealthState.HEALTHY
                # Gauges count the underlying condition regardless of which state
                # won the worst-wins precedence (a strat both circuit_open AND in
                # cooldown contributes to BOTH gauges).
                if circuit:
                    loss_latched += 1
                if in_cooldown:
                    auth_active += 1
                preflight_skips += runner.preflight_skip_count
                auth_cooldown_cycles += self._auth_cooldown.cooldown_cycles(strat_id)
                strat_states.append({
                    "strat_id": strat_id,
                    "symbol": runner.symbol,
                    "state": state,
                    "shadow": runner.shadow_mode,
                    "net_position_size": runner.net_position_size,
                    "preflight_skips": runner.preflight_skip_count,
                })
            gauges = {
                "runners": len(self._runners),
                "auth_cooldown_active": auth_active,
                "loss_breaker_latched": loss_latched,
                "preflight_skips": preflight_skips,
                "auth_cooldown_cycles": auth_cooldown_cycles,
                "uptime_seconds": round(uptime, 1),
            }
            snapshot = build_snapshot(
                strat_states=strat_states,
                metrics=self._health_metrics,
                gauges=gauges,
                generated_at=datetime.now(UTC).isoformat(),
                overall=overall,
            )
            self._health_writer.write(snapshot)
        except Exception as e:
            warn_now = time.monotonic()
            if (warn_now - self._status_write_warn_last) >= _STATUS_WRITE_WARN_THROTTLE:
                self._status_write_warn_last = warn_now
                logger.warning("Health status write failed (throttled): %s", e)

    def _ws_health_check_once(self) -> None:
        """TCP-level WS socket probe with active reset on dead sockets.

        Walks every public/private WS client and calls `is_socket_alive()`
        (pybit's native `ws.sock.connected` check). If the socket is dead
        we don't wait for pybit's internal reconnect — call `client.reset()`
        immediately. Mirrors bbu2's `_ensure_*_connection` pattern.

        Distinct from `_health_check_once`, which uses the wrapper's
        state-flag `is_connected()` (only flipped on explicit disconnect)
        and therefore cannot detect a dead-but-not-noticed socket.
        """
        for account_name, pub_ws in list(self._public_ws.items()):
            try:
                if pub_ws.is_socket_alive():
                    continue
                logger.warning(
                    "WS socket dead for %s/public; resetting",
                    account_name,
                )
                pub_ws.reset()
            except Exception as e:
                logger.error(
                    "ws_health_check failed for %s/public: %s",
                    account_name, e, exc_info=True,
                )
                self._notifier.alert_exception(
                    f"ws_health_check {account_name}/public", e,
                    error_key=f"ws_health_check_pub_{account_name}",
                )
        for account_name, priv_ws in list(self._private_ws.items()):
            try:
                if priv_ws.is_socket_alive():
                    continue
                logger.warning(
                    "WS socket dead for %s/private; resetting",
                    account_name,
                )
                # Feature 0069 signal 4 — a dead PRIVATE socket IS the divergence
                # event; enqueue on DETECTION (before reset) so a forced reconcile
                # is scheduled even if reset() raises — the reconcile runs over
                # REST, independent of the WS socket. Mirrors _health_check_once,
                # which enqueues after its reconnect try/except regardless of
                # success. Private-only (public loop above does NOT enqueue).
                self._enqueue_post_recovery_reconcile(account_name)
                priv_ws.reset()
            except Exception as e:
                logger.error(
                    "ws_health_check failed for %s/private: %s",
                    account_name, e, exc_info=True,
                )
                self._notifier.alert_exception(
                    f"ws_health_check {account_name}/private", e,
                    error_key=f"ws_health_check_priv_{account_name}",
                )

    def _on_ws_disconnect(
        self, account_name: str, kind: str, disconnected_at: datetime
    ) -> None:
        """Secondary signal — heartbeat-detected message gap → reset.

        Called from the WS-client's heartbeat thread when the
        message-gap detector fires. We dispatch `reset()` to a one-shot
        worker thread for two reasons:

        1. The wrapper now self-skips `Thread.join()` when called from
           the heartbeat thread, so `reset()` is safe to invoke inline,
           but the heartbeat thread would still block waiting for the
           full disconnect+connect cycle (TCP teardown, WS handshake,
           subscription replay). Dispatching frees the heartbeat thread
           to return up the stack and exit promptly via the swapped Event.
        2. It keeps `reset()` failures from turning into unhandled
           exceptions on the heartbeat thread (which would only get
           logged by the WS wrapper's generic callback try/except).
        """
        client = (
            self._public_ws.get(account_name)
            if kind == "public"
            else self._private_ws.get(account_name)
        )
        if client is None:
            return
        logger.warning(
            "WS message gap detected for %s/%s; resetting",
            account_name, kind,
        )

        # Feature 0069 signal 4 — a PRIVATE-channel gap means order/position acks
        # may have been missed; schedule a forced reconcile drained next _tick.
        # Public-only gaps imply stale price ticks, not order/position divergence,
        # so they do NOT enqueue. Runs on the WS heartbeat thread — the helper
        # takes _pending_post_recovery_lock.
        if kind == "private":
            self._enqueue_post_recovery_reconcile(account_name)

        def _do_reset() -> None:
            try:
                client.reset()
            except Exception as e:
                logger.error(
                    "WS reset failed for %s/%s: %s",
                    account_name, kind, e, exc_info=True,
                )
                self._notifier.alert_exception(
                    f"ws_reset {account_name}/{kind}", e,
                    error_key=f"ws_reset_{kind}_{account_name}",
                )

        threading.Thread(
            target=_do_reset,
            daemon=True,
            name=f"WSReset-{account_name}-{kind}",
        ).start()

    def _request_immediate_order_sync(self, strat_id: str) -> None:
        """Fast-track the next order-sync sweep.

        Invoked by `StrategyRunner.on_order_update` when a `New`-status WS
        event arrives for an order ID we don't track (i.e., a manual order
        placed mid-run). Zeroing `_next_order_sync` makes the main polling
        loop run `_order_sync_once` on its next iteration (~100 ms), which
        adopts the unknown order via `Reconciler.reconcile_reconnect` so the
        next tick can cancel it if it's off-grid.

        Debounced so a burst of manual orders coalesces into one sweep.
        """
        now = time.monotonic()
        if now < self._unknown_order_debounce_until:
            return
        self._unknown_order_debounce_until = now + _UNKNOWN_ORDER_DEBOUNCE_SEC
        self._next_order_sync = 0.0
        logger.info(
            "%s: Untracked order seen — fast-tracking order sync", strat_id,
        )

    def _force_reconcile_strat(
        self,
        strat_id: str,
        direction: Optional[str],
        emit_breaker_warning: bool = True,
    ) -> bool:
        """Forced position + order reconcile (feature 0064; extended 0069).

        Wired as the runner's ``on_truncate_breaker_tripped`` callback AND reused
        by the state-divergence detector (issue #151). Resyncs the order book via
        ``reconcile_reconnect`` (handles untracked-on-exchange / missing-in-memory)
        AND the position size via ``_refresh_position_size_from_rest(force=True)``
        — the piece ``reconcile_reconnect`` does NOT do — closing the stale-mirror
        gap that defeated ``_is_good_to_place``. Rate-limited to at most once per
        ``truncate_breaker_cooldown_seconds`` per strat so a tripped breaker
        cannot itself spam REST during an outage.

        ``direction``:
          - a specific side (``DirectionType.LONG``/``SHORT``) — the breaker-trip
            caller's path: ONE ``reconcile_reconnect`` + ONE position refresh for
            that side (unchanged behaviour).
          - ``None`` — every detector signal's path: ONE rate-limit check, ONE
            ``reconcile_reconnect`` (whole-runner / direction-agnostic), then a
            position refresh for BOTH LONG and SHORT (each in its own try/except).
            Handled INTERNALLY so the rate-limit timestamp is set exactly once and
            the second direction is never silently dropped behind the rate-limit
            (which two back-to-back calls would do — leaving a hedge-mode mirror
            half stale behind a WARNING claiming a full reconcile).

        ``emit_breaker_warning``: when True (default — breaker-trip caller) the
        ``"110017 breaker tripped — forcing ..."`` WARNING is emitted (byte-for-
        byte unchanged). The detector wrapper passes False so that line — and its
        ``'None'`` direction text — is suppressed; only the wrapper's single
        ``state-divergence detected`` WARNING is emitted on a detector fire,
        keeping the analyzer's merged ``force_reconcile_fired`` count at one per
        reconcile (each reconcile matches exactly one of the two scanned patterns).

        Returns True when a reconcile was ATTEMPTED (not rate-limited / no runner),
        False on every no-op exit. True does NOT guarantee full success — the two
        REST steps run in independent try/excepts and a partial failure is alerted
        via ``_notifier.alert_exception`` but still returns True (the WARNING is
        about the action taken, not a success guarantee).
        """
        runner = self._runners.get(strat_id)
        if runner is None:
            return False
        account_name = self._get_account_for_strategy(strat_id)
        reconciler = self._reconcilers.get(account_name) if account_name else None

        # Rate limit per strat using its configured cooldown.
        cfg = next(
            (c for c in self._config.strategies if c.strat_id == strat_id), None
        )
        cooldown = cfg.truncate_breaker_cooldown_seconds if cfg else 60.0
        now = time.monotonic()
        last = self._force_reconcile_last_at.get(strat_id)
        if last is not None and (now - last) < cooldown:
            logger.debug(
                "%s: forced reconcile rate-limited (%.1fs since last < %.1fs)",
                strat_id, now - last, cooldown,
            )
            return False
        self._force_reconcile_last_at[strat_id] = now

        if emit_breaker_warning:
            logger.warning(
                "%s: 110017 breaker tripped — forcing %s position + order reconcile",
                strat_id, direction,
            )
        if reconciler is not None:
            try:
                reconciler.reconcile_reconnect(runner)
            except Exception as e:
                self._notifier.alert_exception(
                    f"forced reconcile orders {strat_id}", e,
                    error_key=f"force_reconcile_orders_{strat_id}",
                )
        # Position-size resync is the #149-critical healing step — it closes the
        # stale-mirror gap that defeats _is_good_to_place. Run each direction in
        # its OWN try/except so an order-reconcile failure above never skips it
        # (P2) and one direction's failure never skips the other (B-1).
        directions = (
            (DirectionType.LONG, DirectionType.SHORT)
            if direction is None
            else (direction,)
        )
        for d in directions:
            try:
                runner._refresh_position_size_from_rest(d, force=True)
            except Exception as e:
                self._notifier.alert_exception(
                    f"forced reconcile position {strat_id}", e,
                    error_key=f"force_reconcile_pos_{strat_id}_{d}",
                )
        return True

    def _trigger_divergence_reconcile(
        self,
        strat_id: str,
        signal: str,
        evidence: object,
        direction: Optional[str] = None,
    ) -> bool:
        """Thin divergence wrapper around ``_force_reconcile_strat`` (issue #151).

        All four detector signals funnel here. Returns ``True`` only when a forced
        reconcile was actually attempted (not kill-switched, detector-throttled,
        or breaker-cooldown-suppressed). Signal 2 uses this to avoid consuming a
        retry-budget edge when the reconcile was suppressed. Order:

        1. Master kill-switch (BEFORE the throttle): if the per-strat
           ``divergence_detector_enabled`` is False, RETURN immediately (no
           reconcile, no WARNING, no throttle bump). This is the catch-all the
           kill-switch test asserts.
        2. Detector throttle (SEPARATE from the breaker cooldown): if within
           ``divergence_reconcile_min_interval_seconds`` of the last divergence
           fire, emit DEBUG and RETURN — without calling ``_force_reconcile_strat``
           and without bumping the throttle.
        3. Call ``_force_reconcile_strat`` exactly ONCE — ALWAYS ``direction=None``
           (the internal both-directions handling does the work) and ALWAYS
           ``emit_breaker_warning=False`` (suppress the breaker line on this path).
           Branch on the returned bool so side-effects ONLY land on a real
           reconcile:
             - False (rate-limited by the breaker cooldown / no runner): single
               DEBUG "suppressed" line; NO WARNING, NO dedup-clear, NO throttle
               bump. A suppressed fire never evicts adjudication state without a
               resync, and the analyzer counter never overstates real reconciles.
             - True: the single ``state-divergence detected`` WARNING, clear the
               runner's dedup cache, and bump the detector throttle.
        """
        cfg = next(
            (c for c in self._config.strategies if c.strat_id == strat_id), None
        )
        if cfg is None or not cfg.divergence_detector_enabled:
            return False

        now = time.monotonic()
        last = self._divergence_last_fire_at.get(strat_id, 0.0)
        min_interval = cfg.divergence_reconcile_min_interval_seconds
        if (now - last) < min_interval:
            logger.debug(
                "%s: divergence signal=%s within throttle (%.1fs since last < %.1fs) "
                "— skipping",
                strat_id, signal, now - last, min_interval,
            )
            return False

        ran = self._force_reconcile_strat(strat_id, None, emit_breaker_warning=False)
        if not ran:
            logger.debug(
                "%s: divergence signal=%s suppressed — forced reconcile within "
                "breaker cooldown",
                strat_id, signal,
            )
            return False

        logger.warning(
            "%s: state-divergence detected (signal=%s, evidence=%s), "
            "forcing full reconcile",
            strat_id, signal, evidence,
        )
        runner = self._runners.get(strat_id)
        if runner is not None:
            try:
                runner.clear_dedup_cache()
            except Exception as e:
                self._notifier.alert_exception(
                    f"divergence dedup clear {strat_id}", e,
                    error_key=f"divergence_dedup_clear_{strat_id}",
                )
        self._divergence_last_fire_at[strat_id] = now
        return True

    def _enqueue_post_recovery_reconcile(self, account_name: str) -> None:
        """Signal 4 — fan out an account-level WS recovery to its strats.

        The three trigger sources (``_on_ws_disconnect`` heartbeat gap,
        ``_health_check_once`` private reconnect, ``_ws_health_check_once`` private
        reset) identify the socket by ``account_name``; the pending set is keyed by
        ``strat_id``, so fan out via ``_account_to_runners`` here. Skips per-strat
        when the detector is disabled. Takes ``_pending_post_recovery_lock`` because
        ``_on_ws_disconnect`` runs on the WS heartbeat thread. The set dedups the
        public+private double-fire for the same account naturally.
        """
        runners = self._account_to_runners.get(account_name, [])
        with self._pending_post_recovery_lock:
            for runner in runners:
                cfg = next(
                    (c for c in self._config.strategies
                     if c.strat_id == runner.strat_id),
                    None,
                )
                if cfg is None or not cfg.divergence_detector_enabled:
                    continue
                self._pending_post_recovery_reconcile.add(runner.strat_id)

    def _divergence_size_check_interval(self) -> float:
        """Cadence for the signal-3 size-delta sweep (orchestrator-level gate).

        The per-strat ``divergence_size_check_interval_seconds`` is honoured by
        taking the MIN across enabled strats so the most-frequent strat's cadence
        is respected; the sweep itself is per-runner and cheap, so over-checking a
        slower strat is harmless. Falls back to 300s when no strat is enabled.
        """
        intervals = [
            c.divergence_size_check_interval_seconds
            for c in self._config.strategies
            if c.divergence_detector_enabled
        ]
        return min(intervals) if intervals else 300.0

    def _divergence_size_check_once(self) -> None:
        """Signal 3 — read-only REST-vs-local position-size delta sweep.

        For each runner: skip when the detector is disabled, when
        ``_instrument_info`` is None (no-rounding mode), or when ``qty_step`` is not
        a positive ``Decimal``. Evaluate BOTH directions via the pure
        ``rest_position_size`` read (no mirror mutation); a ``None`` REST read skips
        that direction (no fire). If EITHER direction's ``abs(rest - local)`` exceeds
        ``qty_step * multiplier``, fire ONCE with ``direction=None`` (full reconcile
        refreshes both mirrors — never targets one side, avoiding the same-sweep
        throttle trap).
        """
        for runner in self._runners.values():
            cfg = next(
                (c for c in self._config.strategies
                 if c.strat_id == runner.strat_id),
                None,
            )
            if cfg is None or not cfg.divergence_detector_enabled:
                continue
            info = getattr(runner, "_instrument_info", None)
            if info is None:
                continue
            qty_step = getattr(info, "qty_step", None)
            if not isinstance(qty_step, Decimal) or qty_step <= 0:
                continue
            threshold = qty_step * Decimal(
                str(cfg.divergence_size_delta_qty_step_multiplier)
            )
            deltas: list[tuple[str, Decimal]] = []
            for direction, local_size in (
                (DirectionType.LONG, runner._long_position.size),
                (DirectionType.SHORT, runner._short_position.size),
            ):
                rest_size = runner.rest_position_size(direction)
                if rest_size is None:
                    logger.debug(
                        "%s: divergence size-check REST read failed for %s — "
                        "skipping that direction",
                        runner.strat_id, direction,
                    )
                    continue
                deltas.append((direction, abs(rest_size - local_size)))
            diverged = [(d, delta) for d, delta in deltas if delta > threshold]
            if diverged:
                evidence = "+".join(f"{d} Δ{delta}" for d, delta in deltas)
                self._trigger_divergence_reconcile(
                    runner.strat_id, "rest_size_delta", evidence, direction=None,
                )

    def _order_sync_once(self) -> None:
        """Single-shot order reconciliation sweep.

        Fetches open orders from exchange via REST and reconciles with
        in-memory state. Matches bbu2's LIMITS_READ_INTERVAL pattern
        (61 seconds by default). The main polling loop schedules this via
        timestamp gating every `order_sync_interval` seconds.
        """
        try:
            for account_name, runners in list(self._account_to_runners.items()):
                reconciler = self._reconcilers.get(account_name)
                if not reconciler:
                    continue

                for runner in runners:
                    try:
                        result = reconciler.reconcile_reconnect(runner)

                        if result.errors:
                            logger.warning(
                                "%s: Order sync completed with errors: %s",
                                runner.strat_id, result.errors,
                            )
                            self._notifier.alert(
                                f"Gridbot: order sync failed for "
                                f"{runner.strat_id} - {result.errors[-1]}",
                                error_key=f"order_sync_{runner.strat_id}",
                            )
                        elif result.orders_injected > 0 or result.untracked_orders_on_exchange > 0:
                            logger.info(
                                "%s: Order sync - fetched=%d, injected=%d, untracked=%d",
                                runner.strat_id, result.orders_fetched,
                                result.orders_injected, result.untracked_orders_on_exchange,
                            )
                        else:
                            logger.debug(
                                "%s: Order sync - in sync, %d orders checked",
                                runner.strat_id, result.orders_fetched,
                            )

                    except Exception as e:
                        logger.error("%s: Order sync error: %s", runner.strat_id, e)
                        self._notifier.alert_exception(
                            f"order_sync {runner.strat_id}", e,
                            error_key=f"order_sync_{runner.strat_id}",
                        )
        except Exception as e:
            logger.error("Order sync sweep error: %s", e)

    def _get_account_for_strategy(self, strat_id: str) -> Optional[str]:
        """Get account name for a strategy."""
        for config in self._config.strategies:
            if config.strat_id == strat_id:
                return config.account
        return None

    def _create_run_records(self) -> None:
        """Create Run records in database.

        Creates User, BybitAccount, Strategy, and Run rows using
        deterministic UUIDs derived from account/strategy names.
        Populates self._run_ids (strat_id -> run_id) for downstream use.
        """
        if self._db is None:
            return

        try:
            with self._db.get_session() as session:
                for account_config in self._config.accounts:
                    user_id = user_id_for(account_config.name)
                    account_id = account_id_for(account_config.name)
                    environment = "testnet" if account_config.testnet else "mainnet"

                    # Upsert User
                    user = session.get(User, user_id)
                    if user is None:
                        user = User(
                            user_id=user_id,
                            username=account_config.name,
                        )
                        session.add(user)

                    # Upsert BybitAccount
                    account = session.get(BybitAccount, account_id)
                    if account is None:
                        account = BybitAccount(
                            account_id=account_id,
                            user_id=user_id,
                            account_name=account_config.name,
                            environment=environment,
                        )
                        session.add(account)

                    for strat_config in self._config.get_strategies_for_account(
                        account_config.name
                    ):
                        strategy_id = strategy_id_for(strat_config.strat_id)

                        # Upsert Strategy
                        strategy = session.get(Strategy, strategy_id)
                        if strategy is None:
                            strategy = Strategy(
                                strategy_id=strategy_id,
                                account_id=account_id,
                                strategy_type="GridStrategy",
                                symbol=strat_config.symbol,
                                config_json=strat_config.model_dump(mode="json"),
                            )
                            session.add(strategy)

                        # Create Run
                        run_type = "shadow" if strat_config.shadow_mode else "live"
                        now = datetime.now(UTC)
                        closed = RunRepository(session).close_stale_running_runs(
                            user_id,
                            account_id,
                            strategy_id,
                            run_type,
                            end_ts=now,
                        )
                        if closed:
                            logger.info(
                                "Closed %d orphaned %s run(s) for strategy %s "
                                "before starting a new run",
                                closed,
                                run_type,
                                strat_config.strat_id,
                            )

                        run = Run(
                            user_id=user_id,
                            account_id=account_id,
                            strategy_id=strategy_id,
                            run_type=run_type,
                            status="running",
                        )
                        session.add(run)
                        session.flush()  # populate run.run_id

                        self._run_ids[strat_config.strat_id] = UUID(run.run_id)
                        self._run_start_ts[strat_config.strat_id] = run.start_ts
                        logger.info(
                            "Created Run %s for strategy %s",
                            run.run_id,
                            strat_config.strat_id,
                        )

        except Exception as e:
            logger.warning("Failed to create Run records: %s", e)

    def _bootstrap_grid_snapshots(self) -> None:
        """Best-effort initial grid_state_snapshots write after restart (issue #108)."""
        if self._grid_state_writer is None:
            return

        writer = self._grid_state_writer
        errors_before = writer.get_stats()["total_errors"]
        enqueued = False

        for strat_id, runner in self._runners.items():
            try:
                if strat_id not in self._run_ids:
                    continue

                run_id = str(self._run_ids[strat_id])
                run_start_ts = _ensure_utc_aware(self._run_start_ts[strat_id])
                account_name = self._get_account_for_strategy(strat_id)
                account_id = account_id_for(account_name)

                grid_step = runner._config.grid_step
                grid_count = runner._config.grid_count
                symbol = runner.symbol

                grid = runner.engine.grid.grid
                if len(grid) <= 1:
                    continue

                current_fp = grid_fingerprint(grid, grid_step, grid_count)
                last = writer.get_last_fingerprint(run_id, account_id, strat_id)
                scope = (run_id, account_id, strat_id)

                if last is None:
                    writer.write(
                        strat_id, grid, grid_step, grid_count,
                        account_id, symbol, exchange_ts=run_start_ts,
                    )
                    enqueued = True
                elif last[0] == current_fp:
                    writer.prime_fingerprint(scope, current_fp)
                else:
                    last_ts = _ensure_utc_aware(last[1])
                    if last_ts <= run_start_ts:
                        writer.write(
                            strat_id, grid, grid_step, grid_count,
                            account_id, symbol, exchange_ts=run_start_ts,
                        )
                        enqueued = True
                    else:
                        logger.warning(
                            "Bootstrap grid snapshot anomaly for %s: stale row "
                            "exchange_ts %s is after run start %s; skipping "
                            "correction (investigate run_id reuse)",
                            strat_id, last_ts, run_start_ts,
                            exc_info=False,
                        )
                        self._notifier.alert(
                            f"Bootstrap grid snapshot anomaly for {strat_id}: "
                            f"stale row exchange_ts {last_ts} is after run start "
                            f"{run_start_ts}; skipping correction (investigate "
                            f"run_id reuse)",
                            error_key=f"bootstrap_anomalous_{strat_id}",
                        )
                        writer.increment_bootstrap_failures()
            except Exception as exc:
                run_id = self._run_ids.get(strat_id)
                logger.warning(
                    "Bootstrap grid snapshot failed for %s (run %s): %s",
                    strat_id, run_id, exc,
                    exc_info=True,
                )
                self._notifier.alert(
                    f"Bootstrap grid snapshot failed for {strat_id} "
                    f"(run {run_id}): {exc}",
                    error_key=f"bootstrap_{strat_id}",
                )
                writer.increment_bootstrap_failures()

        if enqueued:
            success = writer.flush(timeout=5.0)
            errors_after = writer.get_stats()["total_errors"]
            if not success:
                self._notifier.alert(
                    f"Bootstrap grid snapshot flush timed out after 5.0s "
                    f"({writer.get_stats()['queue_size']} items still queued)",
                    error_key="bootstrap_flush",
                )
                writer.increment_bootstrap_failures()
            elif errors_after > errors_before:
                n_failed = errors_after - errors_before
                logger.warning(
                    "Bootstrap grid snapshot insert failed for %d enqueued "
                    "snapshot(s) during flush (total_errors %d -> %d)",
                    n_failed, errors_before, errors_after,
                )
                self._notifier.alert(
                    f"Bootstrap grid snapshot insert failed during flush "
                    f"({n_failed} error(s); see logs)",
                    error_key="bootstrap_insert",
                )
                writer.increment_bootstrap_failures()

    def _update_run_records_stopped(self) -> None:
        """Update Run records to stopped status."""
        if self._db is None or not self._run_ids:
            return

        try:
            with self._db.get_session() as session:
                for strat_id, run_id in self._run_ids.items():
                    run = session.get(Run, str(run_id))
                    if run:
                        run.status = "completed"
                        run.end_ts = datetime.now(UTC)
                        logger.info(
                            "Marked Run %s as completed for strategy %s",
                            run_id,
                            strat_id,
                        )
        except Exception as e:
            logger.warning("Failed to update Run records: %s", e)
