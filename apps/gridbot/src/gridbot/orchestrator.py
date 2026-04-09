"""Orchestrator for multi-strategy coordination.

The orchestrator is the main entry point for the gridbot. It:
- Loads configuration
- Creates strategy runners
- Manages WebSocket connections
- Routes events to the correct runners
- Creates database Run records
"""

import asyncio
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional
from uuid import UUID, uuid5

from bybit_adapter.rest_client import BybitRestClient
from bybit_adapter.ws_client import PublicWebSocketClient, PrivateWebSocketClient
from bybit_adapter.normalizer import BybitNormalizer
from grid_db import DatabaseFactory
from grid_db import Run, Strategy, BybitAccount, User
from gridcore import GridAnchorStore, InstrumentInfo
from gridcore.intents import CancelIntent

from event_saver.main import EventSaver
from event_saver.config import EventSaverConfig
from event_saver.collectors import AccountContext

from gridbot.config import GridbotConfig, AccountConfig, StrategyConfig
from gridbot.executor import IntentExecutor
from gridbot.notifier import Notifier
from gridbot.runner import StrategyRunner
from gridbot.reconciler import Reconciler
from gridbot.retry_queue import RetryQueue

_HEALTH_CHECK_INTERVAL = 10  # seconds


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
        await orchestrator.start()
        await orchestrator.run_until_shutdown()
        await orchestrator.stop()
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
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._position_check_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._order_sync_task: Optional[asyncio.Task] = None

        # Event saver (embedded, optional)
        self._event_saver: Optional[EventSaver] = None

        # WebSocket position data cache: account_name -> symbol -> side -> position_data
        # Follows original bbu2 pattern: WebSocket provides real-time updates,
        # HTTP REST is used only as fallback when WebSocket data is not available
        self._position_ws_data: dict[str, dict[str, dict[str, dict]]] = {}

        # Wallet balance cache: account_name -> (balance, timestamp)
        self._wallet_cache: dict[str, tuple[float, datetime]] = {}
        # NOTE: single lock covers all accounts; acceptable for low account counts.
        # TODO: switch to per-account locks if account count grows beyond ~10.
        self._wallet_cache_lock = asyncio.Lock()  # safe outside event loop (Python 3.10+)

        # Auth cooldown tracking
        self._auth_cooldown_until: dict[str, datetime] = {}  # strat_id -> expiry
        self._auth_cooldown_cycles: dict[str, int] = {}  # strat_id -> cumulative cycle count

    @property
    def running(self) -> bool:
        """Whether orchestrator is running."""
        return self._running

    async def start(self) -> None:
        """Start the orchestrator.

        This initializes all components:
        1. Creates REST clients per account
        2. Creates executors and reconcilers
        3. Creates strategy runners
        4. Performs startup reconciliation
        5. Connects WebSocket streams
        6. Fetches initial positions via REST API
        7. Creates database Run records
        """
        if self._running:
            return

        logger.info("Starting orchestrator")
        self._event_loop = asyncio.get_event_loop()
        self._running = True

        # Initialize per-account resources
        for account_config in self._config.accounts:
            await self._init_account(account_config)

        # Initialize strategies
        for strategy_config in self._config.strategies:
            await self._init_strategy(strategy_config)

        # Build routing maps
        self._build_routing_maps()

        # Perform startup reconciliation
        for runner in self._runners.values():
            account_name = self._get_account_for_strategy(runner.strat_id)
            reconciler = self._reconcilers.get(account_name)
            if reconciler:
                result = await reconciler.reconcile_startup(runner)
                logger.info(
                    f"{runner.strat_id}: Reconciliation complete - "
                    f"fetched={result.orders_fetched}, injected={result.orders_injected}, "
                    f"untracked={result.untracked_orders_on_exchange}"
                )
                if self._config.allow_shared_symbol:
                    logger.warning(
                        f"{runner.strat_id}: Running with allow_shared_symbol=true "
                        f"- order cross-contamination risk active"
                    )

        # Create database Run records (populates _run_ids)
        await self._create_run_records()

        # Start embedded EventSaver before gridbot WS connect so no events are missed
        if self._config.enable_event_saver and self._db is not None:
            await self._start_event_saver()

        # Connect WebSocket streams
        for account_name in self._public_ws:
            await self._connect_websockets(account_name)

        # Initial position fetch so runners have multipliers before first ticker
        logger.info("Fetching initial positions before starting background tasks")
        await self._fetch_and_update_positions(startup=True)

        # Start background tasks
        self._position_check_task = asyncio.create_task(self._position_check_loop())
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        self._order_sync_task = asyncio.create_task(self._order_sync_loop())

        # Start retry queues
        for queue in self._retry_queues.values():
            await queue.start()

        logger.info(f"Orchestrator started with {len(self._runners)} strategies")

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        if not self._running:
            return

        logger.info("Stopping orchestrator")
        self._running = False
        self._shutdown_event.set()

        # Disconnect WebSockets first to stop new events from flowing in
        for ws in self._public_ws.values():
            ws.disconnect()
        for ws in self._private_ws.values():
            ws.disconnect()

        # Stop retry queues (before cancelling tasks so they exit cleanly)
        for queue in self._retry_queues.values():
            await queue.stop()

        # Stop background tasks
        for task in (self._position_check_task, self._health_check_task, self._order_sync_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Stop EventSaver
        if self._event_saver:
            await self._event_saver.stop()
            logger.info("EventSaver stopped")

        # Update Run records
        await self._update_run_records_stopped()

        logger.info("Orchestrator stopped")

    async def run_until_shutdown(self) -> None:
        """Run until shutdown signal received."""
        await self._shutdown_event.wait()

    async def _init_account(self, account_config: AccountConfig) -> None:
        """Initialize resources for an account."""
        name = account_config.name

        # Create REST client
        self._rest_clients[name] = BybitRestClient(
            api_key=account_config.api_key,
            api_secret=account_config.api_secret,
            testnet=account_config.testnet,
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

    async def _init_strategy(self, strategy_config: StrategyConfig) -> None:
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
        instrument_info = await self._fetch_instrument_info(
            strategy_config.symbol, account_name
        )

        # Create runner
        runner = StrategyRunner(
            strategy_config=strategy_config,
            executor=executor,
            instrument_info=instrument_info,
            anchor_store=self._anchor_store,
            on_intent_failed=lambda intent, error: retry_queue.add(intent, error),
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

    async def _connect_websockets(self, account_name: str) -> None:
        """Connect WebSocket streams for an account.

        Callbacks are already configured at construction time in _init_account.
        connect() subscribes to all streams automatically.
        """
        self._public_ws[account_name].connect()
        self._private_ws[account_name].connect()

        logger.info(f"Connected WebSockets for account: {account_name}")

    def _on_ticker(self, account_name: str, symbol: str, message: dict) -> None:
        """Handle ticker WebSocket message."""
        try:
            normalizer = self._normalizers[account_name]
            event = normalizer.normalize_ticker(message)

            if event is None:
                return

            # Route to all runners for this symbol
            runners = self._symbol_to_runners.get(symbol, [])
            for runner in runners:
                asyncio.run_coroutine_threadsafe(
                    runner.on_ticker(event),
                    self._event_loop,
                )
        except Exception as e:
            self._notifier.alert_exception("_on_ticker", e, error_key="ws_on_ticker")

    def _on_auth_cooldown_entered(self, strat_id: str) -> None:
        """Called by executor when auth cooldown activates.

        Works regardless of whether the failure came from the ticker path
        or the retry queue path.
        """
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
        """Handle position WebSocket message.

        Stores position data from WebSocket for real-time updates.
        Following original bbu2 pattern: WebSocket is primary source,
        HTTP REST is fallback only.

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
            # Initialize account cache if needed
            if account_name not in self._position_ws_data:
                self._position_ws_data[account_name] = {}

            # Filter and store position data
            for pos in message.get("data", []):
                # Only process linear (derivatives) positions
                if pos.get("category") != "linear":
                    continue

                symbol = pos.get("symbol", "")
                side = pos.get("side", "")  # "Buy" for long, "Sell" for short

                if not symbol or not side:
                    continue

                # Initialize symbol cache if needed
                if symbol not in self._position_ws_data[account_name]:
                    self._position_ws_data[account_name][symbol] = {}

                # Store position data by side
                self._position_ws_data[account_name][symbol][side] = pos

                logger.debug(
                    f"Position WS update: {account_name}/{symbol}/{side} "
                    f"size={pos.get('size')} avgPrice={pos.get('avgPrice')}"
                )

        except Exception as e:
            self._notifier.alert_exception("_on_position", e, error_key="ws_on_position")

    def _on_order(self, account_name: str, message: dict) -> None:
        """Handle order WebSocket message."""
        try:
            normalizer = self._normalizers[account_name]
            events = normalizer.normalize_order(message)

            for event in events:
                # Route to runner for this symbol
                runners = self._symbol_to_runners.get(event.symbol, [])
                for runner in runners:
                    # Filter by account
                    if self._get_account_for_strategy(runner.strat_id) == account_name:
                        asyncio.run_coroutine_threadsafe(
                            runner.on_order_update(event),
                            self._event_loop,
                        )
        except Exception as e:
            self._notifier.alert_exception("_on_order", e, error_key="ws_on_order")

    def _on_execution(self, account_name: str, message: dict) -> None:
        """Handle execution WebSocket message."""
        try:
            normalizer = self._normalizers[account_name]
            events = normalizer.normalize_execution(message)

            for event in events:
                # Route to runner for this symbol
                runners = self._symbol_to_runners.get(event.symbol, [])
                for runner in runners:
                    # Filter by account
                    if self._get_account_for_strategy(runner.strat_id) == account_name:
                        asyncio.run_coroutine_threadsafe(
                            runner.on_execution(event),
                            self._event_loop,
                        )
        except Exception as e:
            self._notifier.alert_exception("_on_execution", e, error_key="ws_on_execution")

    def _get_position_from_ws(
        self, account_name: str, symbol: str, side: str
    ) -> Optional[dict]:
        """Get position data from WebSocket cache.

        Args:
            account_name: Account name.
            symbol: Trading symbol.
            side: Position side ("Buy" for long, "Sell" for short).

        Returns:
            Position data dict or None if not available.
        """
        try:
            return self._position_ws_data.get(account_name, {}).get(symbol, {}).get(side)
        except (KeyError, TypeError):
            return None

    async def _get_wallet_balance(self, account_name: str) -> float:
        """Get wallet balance, using cache if available.

        Uses asyncio.Lock to prevent duplicate REST fetches when multiple
        async tasks call this concurrently for the same account.

        Args:
            account_name: Account name.

        Returns:
            Wallet balance in USDT.
        """
        # Check if caching is disabled
        if self._config.wallet_cache_interval <= 0:
            return await self._fetch_wallet_balance(account_name)

        async with self._wallet_cache_lock:
            # Check cache (inside lock to prevent duplicate fetches)
            cached = self._wallet_cache.get(account_name)
            if cached:
                balance, timestamp = cached
                age = (datetime.now(UTC) - timestamp).total_seconds()
                if age < self._config.wallet_cache_interval:
                    return balance

            # Cache miss or expired - fetch fresh
            balance = await self._fetch_wallet_balance(account_name)
            self._wallet_cache[account_name] = (balance, datetime.now(UTC))
            return balance

    async def _fetch_wallet_balance(self, account_name: str) -> float:
        """Fetch wallet balance from REST API.

        Runs synchronous REST call in thread to avoid blocking event loop.

        Args:
            account_name: Account name.

        Returns:
            Wallet balance in USDT.
        """
        rest_client = self._rest_clients[account_name]
        wallet = await asyncio.to_thread(rest_client.get_wallet_balance)

        for account in wallet.get("list", []):
            for coin in account.get("coin", []):
                # USDT-margined only: look for USDT coin in unified wallet
                if coin.get("coin") == "USDT":
                    return float(coin.get("walletBalance", 0))

        logger.warning("No USDT balance found in wallet response for %s: %s", account_name, wallet)
        return 0.0

    async def _fetch_instrument_info(
        self, symbol: str, account_name: str
    ) -> Optional[InstrumentInfo]:
        """Fetch instrument info from Bybit API for qty rounding.

        Uses the account's REST client public endpoint.
        Returns None if fetch fails (qty rounding will be skipped).

        Args:
            symbol: Trading pair (e.g., "BTCUSDT").
            account_name: Account name (for REST client access).

        Returns:
            InstrumentInfo or None if fetch fails.
        """
        try:
            rest_client = self._rest_clients[account_name]
            raw = await asyncio.wait_for(
                asyncio.to_thread(rest_client.get_instruments_info, symbol),
                timeout=10,
            )
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

    async def _fetch_and_update_positions(self, *, startup: bool = False) -> None:
        """Fetch positions and wallet balance, then update all runners.

        Following original bbu2 pattern:
        1. Use WebSocket position data as primary source (real-time)
        2. Fall back to REST API when WebSocket data is not available
        3. Periodically sync via REST to ensure data freshness

        Args:
            startup: If True, log warnings with startup context on failure.
        """
        for account_name, runners in list(self._account_to_runners.items()):
            try:
                rest_client = self._rest_clients[account_name]

                # Fetch wallet balance (cached to reduce API calls)
                timeout = self._config.rest_fetch_timeout
                wallet_balance = await asyncio.wait_for(
                    self._get_wallet_balance(account_name), timeout=timeout
                )

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
                            # Note: asyncio.to_thread cannot cancel the underlying
                            # thread on timeout; it runs until the HTTP request
                            # completes. pybit.HTTP is synchronous with no async
                            # alternative, so this is the best we can do.
                            rest_positions = await asyncio.wait_for(
                                asyncio.to_thread(rest_client.get_positions),
                                timeout=timeout,
                            )
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
                        await runner.on_position_update(
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

            except asyncio.TimeoutError:
                msg = (
                    f"Timeout ({timeout}s) fetching positions for {account_name}"
                )
                if startup:
                    msg += ". Runners may not have multipliers until next periodic check"
                    logger.warning(msg)
                else:
                    logger.error(msg)
                    exc = asyncio.TimeoutError(msg)
                    self._notifier.alert_exception(
                        "_fetch_and_update_positions",
                        exc,
                        error_key=f"position_fetch_{account_name}",
                    )
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

    async def _position_check_loop(self) -> None:
        """Periodic position check loop."""
        while self._running:
            try:
                await asyncio.sleep(self._config.position_check_interval)
                await self._fetch_and_update_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Position check loop error: %s", e)

    async def _health_check_loop(self) -> None:
        """Periodic WebSocket health check.

        Checks every 10 seconds whether each WebSocket connection is alive.
        Reconnects only the disconnected ones. Alerts on disconnect/reconnect.
        """
        while self._running:
            try:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL)

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

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._notifier.alert_exception(
                    "_health_check_loop", e, error_key="health_check_loop"
                )

    async def _order_sync_loop(self) -> None:
        """Periodic order reconciliation loop.

        Fetches open orders from exchange via REST and reconciles with in-memory state.
        Matches bbu2's LIMITS_READ_INTERVAL pattern (61 seconds by default).
        """
        # Skip if disabled
        if self._config.order_sync_interval <= 0:
            logger.info("Order sync loop disabled (order_sync_interval <= 0)")
            return

        while self._running:
            try:
                # Reconcile immediately on start, then sleep between cycles.
                # (Differs from _position_check_loop which sleeps first —
                # immediate sync on startup ensures order state is fresh.)
                for account_name, runners in list(self._account_to_runners.items()):
                    reconciler = self._reconcilers.get(account_name)
                    if not reconciler:
                        continue

                    for runner in runners:
                        try:
                            result = await reconciler.reconcile_reconnect(runner)

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

                await asyncio.sleep(self._config.order_sync_interval)

            except asyncio.CancelledError:
                # CancelledError is a BaseException, not Exception, so it
                # passes through the inner `except Exception` and is caught
                # here to cleanly exit the loop on task cancellation.
                break
            except Exception as e:
                # Guards against errors outside the per-runner try/except:
                # e.g. missing reconciler attribute or asyncio.sleep failure.
                logger.error("Order sync loop error: %s", e)
                try:
                    await asyncio.sleep(self._config.order_sync_interval)
                except asyncio.CancelledError:
                    break

    def _get_account_for_strategy(self, strat_id: str) -> Optional[str]:
        """Get account name for a strategy."""
        for config in self._config.strategies:
            if config.strat_id == strat_id:
                return config.account
        return None

    async def _start_event_saver(self) -> None:
        """Create and start the embedded EventSaver.

        Derives symbols from all strategies and creates AccountContext
        for each configured account. Must be called after _create_run_records()
        so that _run_ids is populated.
        """
        if not self._config.accounts:
            logger.warning("EventSaver enabled but no accounts configured, skipping")
            return

        # Collect all symbols across strategies
        all_symbols = {s.symbol for s in self._config.strategies}

        # Use first account's testnet flag for public streams
        first_account = self._config.accounts[0]

        es_config = EventSaverConfig(
            symbols=",".join(sorted(all_symbols)),
            testnet=first_account.testnet,
            database_url=self._config.database_url,
        )

        self._event_saver = EventSaver(config=es_config, db=self._db)

        # Add each account
        for account_config in self._config.accounts:
            account_strategies = self._config.get_strategies_for_account(
                account_config.name
            )

            # Skip accounts with no strategies — empty symbols means "no filter"
            # in PrivateCollector, which would over-collect
            if not account_strategies:
                logger.warning(
                    "Skipping EventSaver account %s: no strategies configured",
                    account_config.name,
                )
                continue

            # Derive deterministic UUIDs from account name
            namespace = UUID("12345678-1234-5678-1234-567812345678")
            account_id = uuid5(namespace, f"account:{account_config.name}")
            user_id = uuid5(namespace, f"user:{account_config.name}")

            # Get symbols for this account
            account_symbols = [s.symbol for s in account_strategies]

            # Look up run_id — only use it when the account has exactly one
            # strategy. With multiple strategies, Run is strategy-scoped so a
            # single run_id would mis-tag events for the other strategies.
            if len(account_strategies) == 1:
                run_id = self._run_ids.get(account_strategies[0].strat_id)
            else:
                run_id = None
                logger.warning(
                    "Account %s has %d strategies; setting run_id=None to avoid "
                    "mis-tagging. Executions/orders will be captured but not "
                    "linked to a specific Run.",
                    account_config.name,
                    len(account_strategies),
                )

            context = AccountContext(
                account_id=account_id,
                user_id=user_id,
                run_id=run_id,
                api_key=account_config.api_key,
                api_secret=account_config.api_secret,
                environment="testnet" if account_config.testnet else "mainnet",
                symbols=account_symbols,
            )
            await self._event_saver.add_account(context)

        await self._event_saver.start()
        logger.info(
            "EventSaver started (symbols=%s, accounts=%d)",
            sorted(all_symbols),
            len(self._config.accounts),
        )

    async def _create_run_records(self) -> None:
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

    async def _update_run_records_stopped(self) -> None:
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
