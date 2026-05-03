"""Position-fetch subsystem extracted from Orchestrator.

Owns the per-account position cache (WS primary, REST fallback), the
wallet-balance cache, the steady-state rotation tick, and the startup
batch. Callers (Orchestrator) hand in collaborators via constructor
injection — this class holds no back-reference to Orchestrator.

Thread model:
- `on_position_message` runs on the pybit WS background thread and
  only mutates `_position_ws_data` via GIL-atomic `setdefault` +
  single assignment.
- All other methods run on the main polling thread and are the sole
  reader/writer of `_wallet_cache`, `_last_position_fetch`, and
  `_position_fetch_rotation_index`. `get_wallet_balance` enforces
  main-thread-only access at runtime.
"""

import logging
import threading
import time
from datetime import datetime, UTC
from typing import Callable, Optional

from bybit_adapter.rest_client import BybitRestClient

from gridbot.notifier import Notifier
from gridbot.runner import StrategyRunner

logger = logging.getLogger(__name__)

_POSITION_FETCH_SLOW_THRESHOLD = 2.0  # log a warning if REST position fetch takes longer
_POSITION_TICK_BASE = 15.0  # base cadence for the steady-state position-fetch rotation tick (seconds)
_POSITION_STARTUP_HARD_CAP = 60.0  # hard ceiling on the startup batch; exceeding aborts startup


class StartupTimeoutError(RuntimeError):
    """Raised when the startup position-fetch batch exceeds the hard cap.

    main.py catches startup exceptions and returns exit code 1, so this
    aborts the bot cleanly instead of silently continuing with partially
    initialized accounts.
    """


class PositionFetcher:
    """Periodic position + wallet fetch with WS-primary / REST-fallback merge.

    Owns the four pieces of state needed by the fetch loop:
    `_position_ws_data`, `_wallet_cache`, `_last_position_fetch`,
    `_position_fetch_rotation_index`.
    """

    def __init__(
        self,
        *,
        rest_clients: dict[str, BybitRestClient],
        account_to_runners: dict[str, list[StrategyRunner]],
        notifier: Notifier,
        wallet_cache_interval: float,
        position_check_interval: float,
        on_position_changed: Optional[Callable[[str, str], None]] = None,
    ):
        # Dicts are held by reference; Orchestrator mutates them in place
        # during _init_account, and those updates are visible here.
        self._rest_clients = rest_clients
        self._account_to_runners = account_to_runners
        self._notifier = notifier
        self._wallet_cache_interval = wallet_cache_interval
        self._position_check_interval = position_check_interval
        # Optional WS-thread notification: called once per (account, symbol)
        # after on_position_message has written the cache. Orchestrator uses
        # this to coalesce a snapshot for the main-loop drain (feature 0023).
        self._on_position_changed = on_position_changed

        # WebSocket position data cache: account_name -> symbol -> side -> position_data
        # Follows original bbu2 pattern: WebSocket provides real-time updates,
        # HTTP REST is used only as fallback when WebSocket data is not available
        self._position_ws_data: dict[str, dict[str, dict[str, dict]]] = {}

        # Wallet balance cache: account_name -> (balance, timestamp)
        #
        # Thread safety: accessed ONLY from the main polling loop, via
        # get_wallet_balance / _fetch_wallet_balance. get_wallet_balance
        # raises RuntimeError if called from any non-main thread. The main
        # loop is the sole reader/writer, so no lock is required.
        #
        # Do NOT touch this from WS callbacks — they run in pybit threads
        # and would trip the guard.
        self._wallet_cache: dict[str, tuple[float, datetime]] = {}

        # Per-account timestamp of the last completed REST position fetch
        # (time.monotonic). Steady-state rotation uses this to enforce a
        # per-account minimum interval of
        # max(config.position_check_interval, N * _POSITION_TICK_BASE).
        self._last_position_fetch: dict[str, float] = {}

        # Rotation index for steady-state one-account-per-tick position
        # fetching. After each successful fetch the index advances mod N,
        # guaranteeing every account eventually gets its turn.
        self._position_fetch_rotation_index: int = 0

    def on_position_message(self, account_name: str, message: dict) -> None:
        """Handle position WebSocket message (runs in pybit WS thread).

        Stores position data into `_position_ws_data` as the primary,
        real-time source. `fetch_and_update` on the main thread later
        reads this cache (with REST fallback when a slot is still None).

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
        changed_symbols: set[str] = set()
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
                changed_symbols.add(symbol)

                logger.debug(
                    f"Position WS update: {account_name}/{symbol}/{side} "
                    f"size={pos.get('size')} avgPrice={pos.get('avgPrice')}"
                )

        except Exception as e:
            self._notifier.alert_exception("_on_position", e, error_key="ws_on_position")
            return

        # Notify orchestrator (feature 0023). Done after the cache writes so
        # the callback can read the freshest snapshot via get_position_from_ws.
        # Deduped to one call per (account, symbol) regardless of how many
        # sides arrived in this message. Wrapped so a misbehaving callback
        # cannot wedge the WS thread.
        if self._on_position_changed is not None:
            for symbol in changed_symbols:
                try:
                    self._on_position_changed(account_name, symbol)
                except Exception as e:
                    self._notifier.alert_exception(
                        "_on_position_changed", e, error_key="ws_on_position_changed",
                    )

    def get_position_from_ws(
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

    def get_wallet_balance(self, account_name: str) -> float:
        """Get wallet balance, using cache if available.

        Single-thread polling loop: no locking required — the main loop
        is the only reader/writer of `_wallet_cache`.

        Args:
            account_name: Account name.

        Returns:
            Wallet balance in USDT.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "_get_wallet_balance touches _wallet_cache; must run on main thread"
            )
        # Check if caching is disabled
        if self._wallet_cache_interval <= 0:
            return self._fetch_wallet_balance(account_name)

        cached = self._wallet_cache.get(account_name)
        if cached:
            balance, timestamp = cached
            age = (datetime.now(UTC) - timestamp).total_seconds()
            if age < self._wallet_cache_interval:
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

    def fetch_and_update(self, *, startup: bool = False) -> None:
        """Fetch positions and wallet balance, then update runners.

        Two modes:
        - startup=True: batch pass through every account, bounded by a
          hard wall-clock cap (_POSITION_STARTUP_HARD_CAP). Exceeding
          the cap raises StartupTimeoutError to abort the bot.
        - startup=False: one account per call, round-robin. The picked
          account must satisfy the per-account floor:
              floor = max(config.position_check_interval, N * _POSITION_TICK_BASE)
          If no eligible account exists this tick, nothing happens.

        Per-account body (WS-first with REST fallback, per-runner
        on_position_update, slow-REST warning) is shared between the
        two modes via `_fetch_one_account`.
        """
        if startup:
            self._fetch_positions_startup_batch()
        else:
            self._fetch_positions_rotation_tick()

    def _fetch_positions_startup_batch(self) -> None:
        """Startup: fetch every account serially, aborting on hard cap."""
        accounts = list(self._account_to_runners.items())
        total = len(accounts)
        loop_start = time.monotonic()
        done = 0
        for account_name, runners in accounts:
            elapsed_total = time.monotonic() - loop_start
            if elapsed_total >= _POSITION_STARTUP_HARD_CAP:
                raise StartupTimeoutError(
                    f"Startup position fetch exceeded "
                    f"{_POSITION_STARTUP_HARD_CAP:.0f}s "
                    f"({elapsed_total:.1f}s elapsed); initialized "
                    f"{done}/{total} accounts. Aborting startup — "
                    f"check REST connectivity and pybit timeouts."
                )
            try:
                self._fetch_one_account(account_name, runners)
            except Exception as e:
                # Per-account exception during startup is logged as
                # warning and the batch continues. The only startup
                # abort signal is the hard cap above.
                logger.warning(
                    "Failed to fetch initial positions for %s during startup: %s. "
                    "Runners may not have multipliers until next periodic check.",
                    account_name, e,
                )
            else:
                done += 1
            self._last_position_fetch[account_name] = time.monotonic()
        # Rotation begins after startup; start from index 0.
        self._position_fetch_rotation_index = 0

    def _fetch_positions_rotation_tick(self) -> None:
        """Steady-state: fetch ONE eligible account per call, round-robin."""
        accounts = list(self._account_to_runners.items())
        n = len(accounts)
        if n == 0:
            return
        per_account_floor = max(
            float(self._position_check_interval),
            n * _POSITION_TICK_BASE,
        )
        now = time.monotonic()
        start_idx = self._position_fetch_rotation_index % n
        for offset in range(n):
            idx = (start_idx + offset) % n
            account_name, runners = accounts[idx]
            last = self._last_position_fetch.get(account_name, 0.0)
            if (now - last) < per_account_floor:
                continue
            try:
                self._fetch_one_account(account_name, runners)
            except Exception as e:
                logger.error("Position check error for %s: %s", account_name, e)
                self._notifier.alert_exception(
                    "_fetch_and_update_positions", e,
                    error_key=f"position_fetch_{account_name}",
                )
            self._last_position_fetch[account_name] = time.monotonic()
            self._position_fetch_rotation_index = (idx + 1) % n
            return
        # Nobody eligible — all accounts fetched within the floor window.
        # Silent no-op; the next tick will retry.

    def _fetch_one_account(self, account_name: str, runners: list) -> None:
        """Fetch wallet + positions for one account, update each runner.

        WS data is primary; REST is fallback when WS is missing. Per-runner
        on_position_update exceptions are caught and alerted but do not
        abort the per-account pass. REST or wallet-fetch exceptions
        propagate to the caller (startup / rotation-tick) which decides
        how to handle them.
        """
        start = time.monotonic()
        try:
            rest_client = self._rest_clients[account_name]

            # Fetch wallet balance (cached to reduce API calls).
            # pybit's HTTP(timeout=...) caps the request; no extra wrapper.
            wallet_balance = self.get_wallet_balance(account_name)

            # Lazy REST positions (fetched on demand if WS data is missing).
            rest_positions = None

            for runner in runners:
                symbol = runner.symbol

                # Try WebSocket data first (real-time)
                long_pos = self.get_position_from_ws(account_name, symbol, "Buy")
                short_pos = self.get_position_from_ws(account_name, symbol, "Sell")

                # Fall back to REST if WebSocket data not available
                if long_pos is None or short_pos is None:
                    if rest_positions is None:
                        rest_positions = rest_client.get_positions()
                        logger.debug(
                            f"Fetched positions from REST for {account_name} "
                            f"(WS data incomplete)"
                        )
                    for pos in rest_positions:
                        if pos.get("symbol") != symbol:
                            continue
                        side = pos.get("side", "")
                        if side == "Buy" and long_pos is None:
                            long_pos = pos
                        elif side == "Sell" and short_pos is None:
                            short_pos = pos

                try:
                    runner.on_position_update(
                        long_position=long_pos,
                        short_position=short_pos,
                        wallet_balance=wallet_balance,
                        last_close=runner.engine.last_close,
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
        finally:
            elapsed = time.monotonic() - start
            if elapsed > _POSITION_FETCH_SLOW_THRESHOLD:
                logger.warning(
                    "Position fetch for %s took %.1fs (threshold=%.1fs) — "
                    "blocking REST stalled the main polling loop",
                    account_name, elapsed, _POSITION_FETCH_SLOW_THRESHOLD,
                )
