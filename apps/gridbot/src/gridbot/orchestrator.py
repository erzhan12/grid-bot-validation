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
from datetime import datetime, timedelta, UTC
from typing import Optional
from uuid import UUID, uuid5

from bybit_adapter.rest_client import BybitRestClient
from bybit_adapter.ws_client import PublicWebSocketClient, PrivateWebSocketClient
from bybit_adapter.normalizer import BybitNormalizer
from grid_db import DatabaseFactory
from grid_db import Run, Strategy, BybitAccount, User
from gridcore import (
    GridAnchorStore,
    InstrumentInfo,
    TickerEvent,
    ExecutionEvent,
    OrderUpdateEvent,
)
from gridcore.intents import CancelIntent

from gridbot.config import GridbotConfig, AccountConfig, StrategyConfig
from gridbot.executor import IntentExecutor
from gridbot.notifier import Notifier
from gridbot.runner import StrategyRunner
from gridbot.reconciler import Reconciler
from gridbot.retry_queue import RetryQueue

_HEALTH_CHECK_INTERVAL = 10  # seconds
_CHECK_INTERVAL = 0.1  # 100 ms main loop tick (bbu2 value)
_RETRY_TICK_INTERVAL = 1.0  # seconds between retry-queue drains
_POSITION_FETCH_SLOW_THRESHOLD = 2.0  # log a warning if REST position fetch takes longer
_POSITION_FETCH_MIN_INTERVAL = 1.0  # per-account floor between REST fetches (defense-in-depth)
_POSITION_FETCH_TOTAL_BUDGET = 5.0  # total wall-clock ceiling for one _fetch_and_update_positions call
_STARTUP_POSITION_FETCH_BUDGET = 30.0  # higher ceiling for the startup pass (covers ~3 accounts hitting full pybit timeout)
_MAX_TICK_BACKOFF = 180.0  # cap (s) on main-loop backoff after consecutive _tick() failures
_UNKNOWN_ORDER_DEBOUNCE_SEC = 2.0  # min interval between WS-triggered fast-track order syncs


logger = logging.getLogger(__name__)


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
            anchor_store_path: Path to grid anchor JSON file.
            notifier: Alert notifier (optional, log-only if None).
        """
        self._config = config
        self._db = db
        self._anchor_store = GridAnchorStore(anchor_store_path)
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

        # WebSocket position data cache: account_name -> symbol -> side -> position_data
        # Follows original bbu2 pattern: WebSocket provides real-time updates,
        # HTTP REST is used only as fallback when WebSocket data is not available
        self._position_ws_data: dict[str, dict[str, dict[str, dict]]] = {}

        # Wallet balance cache: account_name -> (balance, timestamp)
        # IMPORTANT: Only accessed from main thread, never from WS callbacks.
        # The main polling loop is the single reader/writer, so no lock is
        # required. Do NOT touch this from _on_ticker / _on_order /
        # _on_execution / _on_position — those run in pybit WS threads.
        # No runtime enforcement by design — this is a field, not an entry
        # point; invariant held by review.
        self._wallet_cache: dict[str, tuple[float, datetime]] = {}

        # WS → main-thread buffers. dict/deque mutations are atomic under CPython GIL.
        # Ticker: latest-wins cache, symbol -> event.
        self._latest_ticker: dict[str, TickerEvent] = {}
        self._last_processed_ticker: dict[str, TickerEvent] = {}
        # Per-runner pending execution/order events (deques are atomic under GIL).
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

        # Per-account floor for blocking REST position fetches. Defense-
        # in-depth against scheduler glitches or manual re-entry that
        # could fire two back-to-back fetches and pin the main polling
        # loop. `_next_position_check` already spaces real ticks by the
        # configured interval; this floor only matters if something
        # bypasses that. Keyed by account_name; value is `time.monotonic`.
        self._last_position_fetch: dict[str, float] = {}

        # Auth cooldown tracking
        self._auth_cooldown_until: dict[str, datetime] = {}  # strat_id -> expiry
        self._auth_cooldown_cycles: dict[str, int] = {}  # strat_id -> cumulative cycle count

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
        self._fetch_and_update_positions(startup=True)

        # Prime periodic-tick schedulers so the first main loop iteration
        # does not immediately re-run expensive checks.
        now = time.monotonic()
        self._next_position_check = now + self._config.position_check_interval
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
        now = time.monotonic()
        if now >= self._next_position_check:
            self._fetch_and_update_positions()
            self._next_position_check = now + self._config.position_check_interval
        if now >= self._next_health_check:
            self._health_check_once()
            self._next_health_check = now + _HEALTH_CHECK_INTERVAL
        if (
            self._config.order_sync_interval > 0
            and now >= self._next_order_sync
        ):
            self._order_sync_once()
            self._next_order_sync = now + self._config.order_sync_interval
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
            timeout=int(self._config.rest_fetch_timeout),
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
            on_position=lambda msg, a=name: self._on_position(a, msg),
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
            on_cooldown_entered=lambda sid=strat_id: self._on_auth_cooldown_entered(sid),
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
            anchor_store=self._anchor_store,
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
            # dict[k] = v is atomic under CPython GIL. Races with the main
            # loop are benign: worst case we overwrite a value that hasn't
            # been read yet, but the reader will pick up the newer one on
            # the next iteration.
            self._latest_ticker[symbol] = event
        except Exception as e:
            self._notifier.alert_exception("_on_ticker", e, error_key="ws_on_ticker")

    def _on_auth_cooldown_entered(self, strat_id: str) -> None:
        """Called by executor when auth cooldown activates.

        Works regardless of whether the failure came from the ticker path
        or the retry queue path.

        Thread-safety: this callback is main-thread only. It mutates
        ``_auth_cooldown_cycles`` (read-then-write — NOT atomic) and
        ``_auth_cooldown_until``, and calls ``retry_queue.clear()`` which
        runs in parallel with ``process_due()`` only if the main-thread
        assumption holds. All current callers satisfy this: executor
        entry points (`execute_place`/`execute_cancel`/`execute_amend`)
        are invoked from `StrategyRunner` (main-thread ticker cycle) and
        `RetryQueue.process_due()` (main-thread retry-drain tick). If a
        future change wires this callback to a WebSocket handler or any
        other thread, the cycle-counter update and the retry-queue clear
        must be serialized with a lock (and with `process_due`).

        Design note: fail-loud thread guard is deliberate — not a missing
        lock. Adding one here would signal "safe from any thread" and
        invite callers that deadlock against `process_due` or push us
        into a drain-pattern that delays cooldown activation. Enforcing
        the invariant at runtime keeps the design simple and makes any
        violation impossible to miss.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "_on_auth_cooldown_entered must run on the main thread; "
                "see docstring for the locking required before relaxing "
                f"this. Called from: {threading.current_thread().name}"
            )
        cycle = self._auth_cooldown_cycles.get(strat_id, 0) + 1
        self._auth_cooldown_cycles[strat_id] = cycle

        cooldown_minutes = self._config.auth_cooldown_minutes
        expiry = datetime.now(UTC) + timedelta(minutes=cooldown_minutes)
        self._auth_cooldown_until[strat_id] = expiry

        executor = self._strategy_executors.get(strat_id)
        failure_count = executor.auth_failure_count if executor else "?"

        # Clear retry queue — stale intents would fail with the same auth error,
        # and fresh intents at current prices will be generated after cooldown.
        retry_queue = self._retry_queues.get(strat_id)
        if retry_queue:
            cleared = retry_queue.clear()
            if cleared:
                logger.info(f"Cleared {cleared} items from retry queue for {strat_id}")

        msg = (
            f"Strategy {strat_id}: {failure_count} consecutive auth errors, "
            f"entering {cooldown_minutes}-min cooldown (cycle {cycle})"
        )
        logger.error(msg)
        self._notifier.alert(msg, error_key=f"auth_cooldown_{strat_id}")

    def _on_position(self, account_name: str, message: dict) -> None:
        """Handle position WebSocket message (runs in pybit WS thread).

        Stores position data into `_position_ws_data` as the primary,
        real-time source. `_fetch_and_update_positions` on the main
        thread later reads this cache (with REST fallback when a slot
        is still None).

        Thread-safety: every mutation here is a single `dict[k] = v`
        (setdefault plus an explicit assignment), which is atomic under
        the CPython GIL. No lock is needed. Races with the main-thread
        reader are benign — worst case the reader sees a partially
        populated `_position_ws_data[account][symbol]` for one side
        before the other lands, and falls back to REST for the missing
        side, which is exactly the same code path taken on a real WS
        gap. Following original bbu2 pattern: WS primary, REST fallback.

        Bybit position message format:
        {
            "topic": "position",
            "data": [
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "side": "Buy",  # "Buy" for long, "Sell" for short
                    "size": "0.1",
                    "avgPrice": "42500.00",
                    "liqPrice": "35000.00",
                    "unrealisedPnl": "10.50",
                    ...
                },
                ...
            ]
        }
        """
        try:
            # Initialize account cache if needed. setdefault is a single
            # C-level call and is GIL-atomic (unlike check-then-assign,
            # which is two separate bytecode ops and can race with a
            # reader observing a transiently missing key).
            account_cache = self._position_ws_data.setdefault(account_name, {})

            # Filter and store position data
            for pos in message.get("data", []):
                # Only process linear (derivatives) positions
                if pos.get("category") != "linear":
                    continue

                symbol = pos.get("symbol", "")
                side = pos.get("side", "")  # "Buy" for long, "Sell" for short

                if not symbol or not side:
                    continue

                # Initialize symbol cache if needed — setdefault is atomic.
                symbol_cache = account_cache.setdefault(symbol, {})

                # Store position data by side — atomic dict-set under GIL.
                symbol_cache[side] = pos

                logger.debug(
                    f"Position WS update: {account_name}/{symbol}/{side} "
                    f"size={pos.get('size')} avgPrice={pos.get('avgPrice')}"
                )

        except Exception as e:
            self._notifier.alert_exception("_on_position", e, error_key="ws_on_position")

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

    def _get_position_from_ws(
        self, account_name: str, symbol: str, side: str
    ) -> Optional[dict]:
        """Get position data from WebSocket cache.

        Uses explicit None checks rather than try/except so real type
        errors (e.g., a cache slot holding a non-dict) surface as bugs
        instead of being silently masked as "no WS data".

        Args:
            account_name: Account name.
            symbol: Trading symbol.
            side: Position side ("Buy" for long, "Sell" for short).

        Returns:
            Position data dict or None if not available.
        """
        account_cache = self._position_ws_data.get(account_name)
        if account_cache is None:
            return None
        symbol_cache = account_cache.get(symbol)
        if symbol_cache is None:
            return None
        return symbol_cache.get(side)

    def _get_wallet_balance(self, account_name: str) -> float:
        """Get wallet balance, using cache if available.

        Single-thread polling loop: no locking required — the main loop
        is the only reader/writer of `_wallet_cache`.

        Args:
            account_name: Account name.

        Returns:
            Wallet balance in USDT.
        """
        assert threading.current_thread() is threading.main_thread(), \
            "_get_wallet_balance touches _wallet_cache; must run on main thread"
        # Check if caching is disabled
        if self._config.wallet_cache_interval <= 0:
            return self._fetch_wallet_balance(account_name)

        cached = self._wallet_cache.get(account_name)
        if cached:
            balance, timestamp = cached
            age = (datetime.now(UTC) - timestamp).total_seconds()
            if age < self._config.wallet_cache_interval:
                return balance

        # Cache miss or expired - fetch fresh
        balance = self._fetch_wallet_balance(account_name)
        self._wallet_cache[account_name] = (balance, datetime.now(UTC))
        return balance

    def _fetch_wallet_balance(self, account_name: str) -> float:
        """Fetch wallet balance from REST API.

        pybit's HTTP() caps every request at `rest_fetch_timeout` seconds
        (plumbed through in P1), so no explicit timeout wrapper is needed.

        Args:
            account_name: Account name.

        Returns:
            Wallet balance in USDT.
        """
        rest_client = self._rest_clients[account_name]
        wallet = rest_client.get_wallet_balance()

        for account in wallet.get("list", []):
            for coin in account.get("coin", []):
                # USDT-margined only: look for USDT coin in unified wallet
                if coin.get("coin") == "USDT":
                    return float(coin.get("walletBalance", 0))

        logger.warning("No USDT balance found in wallet response for %s: %s", account_name, wallet)
        return 0.0

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

    def _fetch_and_update_positions(self, *, startup: bool = False) -> None:
        """Fetch positions and wallet balance, then update all runners.

        Following original bbu2 pattern:
        1. Use WebSocket position data as primary source (real-time)
        2. Fall back to REST API when WebSocket data is not available
        3. Periodically sync via REST to ensure data freshness

        Wall-clock budget: the whole call is capped by one of two
        ceilings — ``_POSITION_FETCH_TOTAL_BUDGET`` for steady-state
        ticks and the larger ``_STARTUP_POSITION_FETCH_BUDGET`` for
        the startup pass. Before starting the REST section for each
        account we check remaining budget and break out of the loop
        if spent. This prevents a sequence of slow accounts (~2s each
        x N) from pinning the main polling loop well beyond any
        single pybit timeout. The startup pass uses a higher ceiling
        so the first pass almost always finishes and initializes
        every runner's multipliers, but is still bounded so a
        pathological pybit stall cannot block startup indefinitely.

        Args:
            startup: If True, log warnings with startup context on
                failure and apply the larger startup budget.
        """
        loop_start = time.monotonic()
        budget = _STARTUP_POSITION_FETCH_BUDGET if startup else _POSITION_FETCH_TOTAL_BUDGET
        for account_name, runners in list(self._account_to_runners.items()):
            start = time.monotonic()
            # Total-budget gate: stop scheduling new REST sections once
            # the cumulative wall-clock ceiling is reached. Remaining
            # accounts will be picked up on the next periodic tick.
            # Startup uses a larger budget (see _STARTUP_POSITION_FETCH_BUDGET)
            # so the first pass almost always completes, but is still bounded.
            elapsed_total = start - loop_start
            if elapsed_total >= budget:
                logger.warning(
                    "Position fetch total budget exceeded (%s): %.1fs >= %.1fs, "
                    "deferring remaining accounts to next tick (skipped: %s)",
                    "startup" if startup else "steady-state",
                    elapsed_total, budget, account_name,
                )
                break
            # Short-circuit: per-account minimum interval between blocking
            # REST fetches. _next_position_check already spaces ticks by
            # the configured interval, but if anything bypasses the
            # scheduler (manual call, startup race), this floor prevents
            # two back-to-back pybit calls from pinning the main loop.
            # Skipped during startup so the initial fetch always runs.
            if not startup:
                last = self._last_position_fetch.get(account_name)
                if last is not None and (start - last) < _POSITION_FETCH_MIN_INTERVAL:
                    logger.debug(
                        "Skipping position fetch for %s: last fetch was %.2fs ago "
                        "(floor=%.1fs)",
                        account_name, start - last, _POSITION_FETCH_MIN_INTERVAL,
                    )
                    continue
            self._last_position_fetch[account_name] = start
            # Instrument the whole per-account REST section so we can spot
            # cases where a blocking pybit call stalls the main polling loop.
            # The 2s threshold is well under the 10s pybit timeout but far
            # enough above normal (~100-300ms) to only fire on real stalls.
            try:
                rest_client = self._rest_clients[account_name]

                # Fetch wallet balance (cached to reduce API calls).
                # pybit's HTTP(timeout=...) caps the request; no extra wrapper.
                wallet_balance = self._get_wallet_balance(account_name)

                # Check if we need to fall back to REST for positions
                # (REST sync ensures freshness even when WS data exists)
                rest_positions = None

                # Update each runner
                for runner in runners:
                    symbol = runner.symbol

                    # Try WebSocket data first (real-time)
                    long_pos = self._get_position_from_ws(account_name, symbol, "Buy")
                    short_pos = self._get_position_from_ws(account_name, symbol, "Sell")

                    # Fall back to REST if WebSocket data not available
                    if long_pos is None or short_pos is None:
                        # Lazy fetch REST positions (once per account)
                        if rest_positions is None:
                            rest_positions = rest_client.get_positions()
                            logger.debug(
                                f"Fetched positions from REST for {account_name} "
                                f"(WS data incomplete)"
                            )

                        # Find positions from REST response
                        for pos in rest_positions:
                            if pos.get("symbol") != symbol:
                                continue
                            side = pos.get("side", "")
                            if side == "Buy" and long_pos is None:
                                long_pos = pos
                            elif side == "Sell" and short_pos is None:
                                short_pos = pos

                    # Get last close from engine
                    last_close = runner.engine.last_close or 0.0

                    try:
                        runner.on_position_update(
                            long_position=long_pos,
                            short_position=short_pos,
                            wallet_balance=wallet_balance,
                            last_close=last_close,
                        )
                    except Exception as e:
                        logger.error(
                            "Position update failed for runner %s: %s",
                            runner.strat_id, e, exc_info=True,
                        )
                        self._notifier.alert_exception(
                            f"runner.on_position_update({runner.strat_id})",
                            e,
                            error_key=f"position_update_{account_name}_{runner.strat_id}",
                        )
                        # Continue to next runner instead of raising

            except Exception as e:
                if startup:
                    logger.warning(
                        "Failed to fetch initial positions for %s during startup: %s. "
                        "Runners may not have multipliers until next periodic check.",
                        account_name, e,
                    )
                else:
                    logger.error("Position check error for %s: %s", account_name, e)
                    self._notifier.alert_exception(
                        "_fetch_and_update_positions", e,
                        error_key=f"position_fetch_{account_name}",
                    )
            finally:
                elapsed = time.monotonic() - start
                if elapsed > _POSITION_FETCH_SLOW_THRESHOLD:
                    logger.warning(
                        "Position fetch for %s took %.1fs (threshold=%.1fs) — "
                        "blocking REST stalled the main polling loop",
                        account_name, elapsed, _POSITION_FETCH_SLOW_THRESHOLD,
                    )

    def _health_check_once(self) -> None:
        """Single-shot WebSocket health check + auth-cooldown expiry sweep.

        Extracted from the former _health_check_loop. The main polling loop
        schedules this via timestamp gating every _HEALTH_CHECK_INTERVAL
        seconds.
        """
        try:
            # Check auth cooldown expiry
            now = datetime.now(UTC)
            for strat_id in list(self._auth_cooldown_until.keys()):
                expiry = self._auth_cooldown_until[strat_id]
                if now >= expiry:
                    executor = self._strategy_executors.get(strat_id)
                    cycle = self._auth_cooldown_cycles.get(strat_id, 1)
                    if executor:
                        executor.reset_auth_cooldown()
                        msg = (
                            f"Strategy {strat_id}: cooldown expired (cycle {cycle}), "
                            f"resuming order execution"
                        )
                        logger.info(msg)
                        self._notifier.alert(
                            msg, error_key=f"auth_cooldown_resume_{strat_id}",
                        )
                    del self._auth_cooldown_until[strat_id]

            for account_name in list(self._public_ws.keys()):
                # Check public WS
                pub_ws = self._public_ws.get(account_name)
                if pub_ws and not pub_ws.is_connected():
                    self._notifier.alert(
                        f"Public WS disconnected for {account_name}, reconnecting",
                        error_key=f"ws_pub_disconnect_{account_name}",
                    )
                    try:
                        pub_ws.disconnect()
                        pub_ws.connect()  # re-subscribes automatically via callbacks
                        logger.info(f"Public WS reconnected for {account_name}")
                    except Exception as e:
                        self._notifier.alert_exception(
                            f"Public WS reconnect {account_name}", e,
                            error_key=f"ws_pub_reconnect_{account_name}",
                        )

                # Check private WS
                priv_ws = self._private_ws.get(account_name)
                if priv_ws and not priv_ws.is_connected():
                    self._notifier.alert(
                        f"Private WS disconnected for {account_name}, reconnecting",
                        error_key=f"ws_priv_disconnect_{account_name}",
                    )
                    try:
                        priv_ws.disconnect()
                        priv_ws.connect()  # re-subscribes automatically via callbacks
                        logger.info(f"Private WS reconnected for {account_name}")
                    except Exception as e:
                        self._notifier.alert_exception(
                            f"Private WS reconnect {account_name}", e,
                            error_key=f"ws_priv_reconnect_{account_name}",
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
