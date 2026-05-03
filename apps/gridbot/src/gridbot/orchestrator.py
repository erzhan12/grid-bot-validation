"""Orchestrator for multi-strategy coordination.

The orchestrator is the main entry point for the gridbot. It:
- Loads configuration
- Creates strategy runners
- Manages WebSocket connections
- Routes events to the correct runners
- Creates database Run records
"""

import logging
import time
from collections import deque
from datetime import datetime, UTC
from typing import Optional
from uuid import UUID, uuid5

from bybit_adapter.rest_client import BybitRestClient
from bybit_adapter.ws_client import PublicWebSocketClient, PrivateWebSocketClient
from bybit_adapter.normalizer import BybitNormalizer
from grid_db import DatabaseFactory
from grid_db import Run, Strategy, BybitAccount, User
from gridcore import (
    GridStateStore,
    InstrumentInfo,
    TickerEvent,
)
from gridcore.intents import CancelIntent

from gridbot.config import GridbotConfig, AccountConfig, StrategyConfig
from gridbot.executor import IntentExecutor
from gridbot.notifier import Notifier
from gridbot.runner import StrategyRunner
from gridbot.reconciler import Reconciler
from gridbot.retry_queue import RetryQueue
from gridbot.position_fetcher import PositionFetcher, _POSITION_TICK_BASE
from gridbot.auth_cooldown_manager import AuthCooldownManager

_HEALTH_CHECK_INTERVAL = 10  # seconds
_CHECK_INTERVAL = 0.1  # 100 ms main loop tick (bbu2 value)
_RETRY_TICK_INTERVAL = 1.0  # seconds between retry-queue drains
_WS_RECONNECT_SLOW_THRESHOLD = 5.0  # log a warning if a single WS disconnect+connect takes longer
_MAX_TICK_BACKOFF = 30.0  # cap (s) on main-loop backoff after consecutive _tick() failures
_UNKNOWN_ORDER_DEBOUNCE_SEC = 2.0  # min interval between WS-triggered fast-track order syncs


logger = logging.getLogger(__name__)


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

        # Run tracking
        self._run_ids: dict[str, UUID] = {}  # strat_id -> run_id

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

        # Perform startup reconciliation
        for runner in self._runners.values():
            account_name = self._get_account_for_strategy(runner.strat_id)
            reconciler = self._reconcilers.get(account_name)
            if reconciler:
                result = reconciler.reconcile_startup(runner)
                logger.info(
                    f"{runner.strat_id}: Reconciliation complete - "
                    f"fetched={result.orders_fetched}, injected={result.orders_injected}, "
                    f"untracked={result.untracked_orders_on_exchange}"
                )

        # Create database Run records (populates _run_ids)
        self._create_run_records()

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
        self._next_order_sync = now  # order sync runs immediately on first tick
        self._next_retry_tick = now + _RETRY_TICK_INTERVAL

        self._running = True
        self._started = True
        logger.info(f"Orchestrator started with {len(self._runners)} strategies")

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
            wallet_balance = self._position_fetcher.get_wallet_balance(account_name)
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
        )
        self._private_ws[name] = PrivateWebSocketClient(
            api_key=account_config.api_key,
            api_secret=account_config.api_secret,
            testnet=account_config.testnet,
            on_position=lambda msg, a=name: self._position_fetcher.on_position_message(a, msg),
            on_order=lambda msg, a=name: self._on_order(a, msg),
            on_execution=lambda msg, a=name: self._on_execution(a, msg),
        )

        logger.info(f"Initialized account: {name}")

    def _init_strategy(self, strategy_config: StrategyConfig) -> None:
        """Initialize a strategy runner."""
        strat_id = strategy_config.strat_id
        account_name = strategy_config.account

        # Get executor for this account (with correct shadow mode)
        base_executor = self._executors[account_name]
        executor = IntentExecutor(
            base_executor._client,
            shadow_mode=strategy_config.shadow_mode,
            on_cooldown_entered=lambda sid=strat_id: self._auth_cooldown.enter(sid),
        )

        # Create retry queue with dispatcher that routes by intent type
        def _dispatch_intent(intent):
            if isinstance(intent, CancelIntent):
                return executor.execute_cancel(intent)
            return executor.execute_place(intent)

        retry_queue = RetryQueue(
            executor_func=_dispatch_intent,
            max_attempts=3,
            max_elapsed_seconds=30.0,
            is_paused=lambda: executor.auth_cooldown,
        )
        self._retry_queues[strat_id] = retry_queue

        # Fetch instrument info for qty rounding
        instrument_info = self._fetch_instrument_info(
            strategy_config.symbol, account_name
        )

        # Create runner
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
            instrument_info=instrument_info,
            state_store=self._state_store,
            on_intent_failed=lambda intent, error: retry_queue.add(intent, error),
            on_unknown_order=self._request_immediate_order_sync,
            notifier=self._notifier,
        )
        self._runners[strat_id] = runner
        self._strategy_executors[strat_id] = executor

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
        except Exception as e:
            self._notifier.alert_exception(
                "_health_check_once", e, error_key="health_check_loop",
            )

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

        namespace = UUID("12345678-1234-5678-1234-567812345678")

        try:
            with self._db.get_session() as session:
                for account_config in self._config.accounts:
                    user_id = str(uuid5(namespace, f"user:{account_config.name}"))
                    account_id = str(uuid5(namespace, f"account:{account_config.name}"))
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
                        strategy_id = str(
                            uuid5(namespace, f"strategy:{strat_config.strat_id}")
                        )

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
                        logger.info(
                            "Created Run %s for strategy %s",
                            run.run_id,
                            strat_config.strat_id,
                        )

        except Exception as e:
            logger.warning("Failed to create Run records: %s", e)

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
