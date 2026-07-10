"""Position-fetch subsystem extracted from Orchestrator.

Owns the per-account position cache (WS primary, REST fallback), the
wallet-balance cache, the steady-state rotation tick, and the startup
batch. Callers (Orchestrator) hand in collaborators via constructor
injection — this class holds no back-reference to Orchestrator.

Thread model:
- `on_position_message` runs on the pybit WS background thread and
  only mutates `_position_ws_data` via GIL-atomic `setdefault` +
  single assignment.
- `on_wallet_message` (feature 0066 Phase 4) ALSO runs on the pybit WS
  background thread. It writes `_wallet_ws_data[account] = (snapshot, ts)`
  via a single GIL-atomic dict assignment, holds no lock, and NEVER
  touches `_wallet_cache` (the main-thread-guarded REST cache). It never
  raises on the WS thread — a malformed frame is dropped (last good kept).
- All other methods run on the main polling thread and are the sole
  reader/writer of `_wallet_cache`, `_last_position_fetch`, and
  `_position_fetch_rotation_index`. `get_wallet_balance` /
  `get_wallet_snapshot` enforce main-thread-only access at runtime.
  `peek_wallet_snapshot` is a non-blocking main-thread reader of both
  caches (never fetches) used by the hot order-dispatch path.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Callable, Optional

from bybit_adapter.rest_client import BybitRestClient

from gridbot.notifier import Notifier
from gridbot.runner import StrategyRunner

logger = logging.getLogger(__name__)

_POSITION_FETCH_SLOW_THRESHOLD = 2.0  # log a warning if REST position fetch takes longer
_POSITION_TICK_BASE = 15.0  # base cadence for the steady-state position-fetch rotation tick (seconds)
_POSITION_STARTUP_HARD_CAP = 60.0  # hard ceiling on the startup batch; exceeding aborts startup


def _float_or_zero(value) -> float:
    """Parse a Bybit numeric field to float, treating None/'' as 0.0.

    Bybit mainnet UTA sends empty strings for unused numeric fields (e.g.
    ``availableToWithdraw`` on cross-margin or dust coins), which ``float('')``
    would raise on. See RULES.md pitfall #20 (``Decimal("")`` raises
    ``decimal.InvalidOperation`` — same empty-string trap, Decimal variant).
    """
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class WalletSnapshot:
    """Account wallet/margin snapshot (feature 0066 / issue #159).

    Extends the bare ``walletBalance`` float the fetcher used to surface with
    the account-level free-margin and maintenance-margin fields needed to
    detect a low-balance state BEFORE Bybit returns 110007. All values are
    USDT floats; missing/empty Bybit fields fall back to 0.0.

    ``available_balance`` is the USDT free balance for new orders: the per-coin
    ``availableToWithdraw`` when present, else the account-level
    ``totalAvailableBalance`` (UTA cross-margin reports the free figure on the
    account row and leaves the per-coin field empty).
    """

    wallet_balance: float = 0.0
    available_balance: float = 0.0
    total_available_balance: float = 0.0
    total_maintenance_margin: float = 0.0


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
        wallet_ws_max_age_seconds: float = 45.0,
    ):
        # Dicts are held by reference; Orchestrator mutates them in place
        # during _init_account, and those updates are visible here.
        self._rest_clients = rest_clients
        self._account_to_runners = account_to_runners
        self._notifier = notifier
        self._wallet_cache_interval = wallet_cache_interval
        self._position_check_interval = position_check_interval
        # Feature 0066 Phase 4: freshness window for the WS wallet feed. A WS
        # slot older than this is treated as stale by get_wallet_snapshot (falls
        # back to REST); the preflight applies the same bound to peek results.
        self._wallet_ws_max_age_seconds = wallet_ws_max_age_seconds
        # Optional WS-thread notification: called once per (account, symbol)
        # after on_position_message has written the cache. Orchestrator uses
        # this to coalesce a snapshot for the main-loop drain (feature 0023).
        self._on_position_changed = on_position_changed

        # WebSocket position data cache: account_name -> symbol -> side -> position_data
        # Follows original bbu2 pattern: WebSocket provides real-time updates,
        # HTTP REST is used only as fallback when WebSocket data is not available
        self._position_ws_data: dict[str, dict[str, dict[str, dict]]] = {}

        # Wallet snapshot cache: account_name -> (WalletSnapshot, timestamp).
        # Holds the full snapshot (balance + account-level margin) since
        # feature 0066; get_wallet_balance reads `.wallet_balance` off it.
        #
        # Thread safety: accessed ONLY from the main polling loop, via
        # get_wallet_snapshot / get_wallet_balance / _fetch_wallet_snapshot.
        # get_wallet_snapshot raises RuntimeError if called from any non-main
        # thread. The main loop is the sole reader/writer, so no lock is
        # required.
        #
        # Do NOT touch this from WS callbacks — they run in pybit threads
        # and would trip the guard.
        self._wallet_cache: dict[str, tuple[WalletSnapshot, datetime]] = {}

        # WS wallet snapshot cache: account_name -> (WalletSnapshot, receive_ts)
        # (feature 0066 Phase 4). Written ONLY by on_wallet_message on the pybit
        # WS thread via a single GIL-atomic assignment (no lock — mirrors
        # _position_ws_data). Read on the main thread by get_wallet_snapshot
        # (WS-primary within the freshness window) and peek_wallet_snapshot
        # (non-blocking hot-path read). Distinct from _wallet_cache (REST); the
        # WS thread NEVER touches _wallet_cache.
        self._wallet_ws_data: dict[str, tuple[WalletSnapshot, datetime]] = {}

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

    def get_wallet_snapshot(self, account_name: str) -> WalletSnapshot:
        """Get wallet snapshot (balance + account-level margin), using cache.

        Single-thread polling loop: no locking required — the main loop is the
        only reader/writer of `_wallet_cache`. Feature 0066 / issue #159.

        Args:
            account_name: Account name.

        Returns:
            WalletSnapshot for the account.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "get_wallet_snapshot touches _wallet_cache; must run on main thread"
            )
        # Feature 0066 Phase 4: WS-primary within the freshness window. A WS slot
        # newer than wallet_ws_max_age_seconds is authoritative (and skips a REST
        # round-trip); a stale or absent slot falls through to the REST cache.
        # This is the BACKGROUND reader (rotation / quiet-window backstop) — the
        # hot order path uses the non-blocking peek_wallet_snapshot instead.
        # Ops (review F3): a DEBUG line at each return path records WS-vs-REST
        # source + slot age — the signal to validate WS freshness in rollout. The
        # INFO `Position update` heartbeat `avail=` lags on the position-drain
        # cadence and is NOT a reliable freshness signal.
        ws_entry = self._wallet_ws_data.get(account_name)
        if ws_entry is not None:
            ws_snapshot, ws_ts = ws_entry
            age = (datetime.now(UTC) - ws_ts).total_seconds()
            if age < self._wallet_ws_max_age_seconds:
                logger.debug(
                    "wallet snapshot for %s served from WS (age=%.1fs)",
                    account_name, age,
                )
                return ws_snapshot
        # Check if caching is disabled
        if self._wallet_cache_interval <= 0:
            logger.debug(
                "wallet snapshot for %s served from REST fetch (cache disabled)",
                account_name,
            )
            return self._fetch_wallet_snapshot(account_name)

        cached = self._wallet_cache.get(account_name)
        if cached:
            snapshot, timestamp = cached
            age = (datetime.now(UTC) - timestamp).total_seconds()
            if age < self._wallet_cache_interval:
                logger.debug(
                    "wallet snapshot for %s served from REST cache (age=%.1fs)",
                    account_name, age,
                )
                return snapshot

        # Cache miss or expired - fetch fresh
        logger.debug(
            "wallet snapshot for %s served from REST fetch (cache miss/expired)",
            account_name,
        )
        snapshot = self._fetch_wallet_snapshot(account_name)
        self._wallet_cache[account_name] = (snapshot, datetime.now(UTC))
        return snapshot

    def get_wallet_balance(self, account_name: str) -> float:
        """Get wallet balance in USDT, using cache if available.

        Back-compat thin wrapper over ``get_wallet_snapshot`` (feature 0066);
        both share the same ``_wallet_cache`` + TTL + main-thread guard.

        Args:
            account_name: Account name.

        Returns:
            Wallet balance in USDT.
        """
        return self.get_wallet_snapshot(account_name).wallet_balance

    def _fetch_wallet_snapshot(self, account_name: str) -> WalletSnapshot:
        """Fetch a wallet snapshot from REST API (one call).

        Account-level free-margin / maintenance-margin live on the account row
        (``totalAvailableBalance`` / ``totalMaintenanceMargin``); per-coin
        ``walletBalance`` / ``availableToWithdraw`` live on the USDT coin row.
        Empty Bybit strings are coerced to 0.0 (UTA dust / cross-margin).

        pybit's HTTP() caps every request at `rest_fetch_timeout` seconds
        (plumbed through in P1), so no explicit timeout wrapper is needed.

        Args:
            account_name: Account name.

        Returns:
            WalletSnapshot (all-zero when no USDT coin is present).
        """
        rest_client = self._rest_clients[account_name]
        wallet = rest_client.get_wallet_balance()

        for account in wallet.get("list", []):
            snapshot = self._snapshot_from_wallet_account_row(account)
            if snapshot is not None:
                return snapshot

        logger.warning("No USDT balance found in wallet response for %s: %s", account_name, wallet)
        return WalletSnapshot()

    @staticmethod
    def _snapshot_from_wallet_account_row(account: dict) -> Optional[WalletSnapshot]:
        """Parse a Bybit wallet account row → WalletSnapshot (feature 0066 P3).

        Shared by ``_fetch_wallet_snapshot`` (REST ``result['list'][0]``) and
        ``on_wallet_message`` (WS ``msg['data'][0]``) — the two rows have the
        same Bybit V5 shape, so one parser prevents REST/WS field drift.

        Account-level free-margin / maintenance-margin live on the account row
        (``totalAvailableBalance`` / ``totalMaintenanceMargin``); per-coin
        ``walletBalance`` / ``availableToWithdraw`` live on the USDT coin row.
        Empty Bybit strings are coerced to 0.0 (UTA dust / cross-margin).

        Returns ``None`` when the row carries no USDT coin — the caller decides:
        ``_fetch_wallet_snapshot`` logs a WARNING + returns all-zero; the WS
        handler keeps the last good slot.
        """
        total_available = _float_or_zero(account.get("totalAvailableBalance"))
        total_mm = _float_or_zero(account.get("totalMaintenanceMargin"))
        for coin in account.get("coin", []):
            # USDT-margined only: look for USDT coin in unified wallet
            if coin.get("coin") == "USDT":
                wallet_balance = _float_or_zero(coin.get("walletBalance"))
                # UTA v5 reports per-coin free margin as `availableToWithdraw`;
                # legacy UTA 1.0 / some cross-margin coins surface it only as
                # `availableBalance`. Prefer the v5 field; fall back to the legacy
                # coin field when v5 is absent/empty (mirrors recorder.py:404-408
                # — keep the two parsers consistent). Then fall back to the
                # account-level total for UTA cross-margin (per-coin empty).
                raw_available = coin.get("availableToWithdraw")
                if raw_available in (None, "") and "availableBalance" in coin:
                    raw_available = coin.get("availableBalance")
                available = _float_or_zero(raw_available)
                if available == 0.0:
                    available = total_available
                return WalletSnapshot(
                    wallet_balance=wallet_balance,
                    available_balance=available,
                    total_available_balance=total_available,
                    total_maintenance_margin=total_mm,
                )
        return None

    def on_wallet_message(self, account_name: str, message: dict) -> None:
        """Handle wallet WebSocket message (runs in pybit WS thread).

        Stores the latest account-level free-margin snapshot + receive timestamp
        into ``_wallet_ws_data`` as the real-time source for the low-balance
        preflight (feature 0066 Phase 4 / issue #159).

        Structural validation only: a frame missing ``data[0]`` / the USDT coin
        (or that fails to parse) is dropped with NO write (keep last good) and
        NEVER raises on the WS thread. A valid row IS written even when
        ``available_balance`` parses to 0 — a funded account with no free margin
        (``wallet_balance > 0``) is a REAL low-balance signal, not "no data";
        gating on an all-zero heuristic would mask it (review P2-malformed).

        Bybit V5 wallet frame (see event_saver ``wallet_writer``):
        ``{"topic":"wallet","creationTime":<ms>,"data":[{<account row>,
        "coin":[{"coin":"USDT","walletBalance":...,"availableToWithdraw":...}]}]}``

        Thread-safety: the single ``_wallet_ws_data[account] = (...)`` assignment
        is GIL-atomic (mirrors ``_position_ws_data``); no lock. The WS thread
        NEVER touches ``_wallet_cache`` (the main-thread-guarded REST cache).
        """
        try:
            data = message.get("data") or []
            if not data:
                return  # partial / empty frame → keep last good
            snapshot = self._snapshot_from_wallet_account_row(data[0])
            if snapshot is None:
                return  # no USDT coin row → keep last good
            self._wallet_ws_data[account_name] = (snapshot, datetime.now(UTC))
        except Exception as e:
            # A malformed frame must never escape the WS thread.
            self._notifier.alert_exception("_on_wallet", e, error_key="ws_on_wallet")

    def peek_wallet_snapshot(
        self, account_name: str
    ) -> Optional[tuple[WalletSnapshot, float]]:
        """Non-blocking read of the freshest wallet snapshot (feature 0066 P4).

        Returns ``(snapshot, age_seconds)`` for the genuinely newest of the WS
        slot and the REST cache, compared by their write timestamps: both
        present → newer wins; only one present → that one; neither → ``None``.
        NEVER fetches — safe to call on the hot order-dispatch path every tick.

        Applies NO freshness threshold itself; the caller (the runner's
        preflight) bounds the returned age against ``wallet_ws_max_age_seconds``.

        review P2: a stale WS slot must NOT shadow a more-recently-rotated REST
        entry, so the choice is by timestamp — never "prefer WS unconditionally".
        Both ``_wallet_ws_data`` and ``_wallet_cache`` stamp ``datetime.now(UTC)``
        on write, so the timestamps are directly comparable.
        """
        ws_entry = self._wallet_ws_data.get(account_name)
        rest_entry = self._wallet_cache.get(account_name)
        if ws_entry is None and rest_entry is None:
            return None
        if rest_entry is None:
            snapshot, ts = ws_entry
        elif ws_entry is None:
            snapshot, ts = rest_entry
        else:
            snapshot, ts = ws_entry if ws_entry[1] >= rest_entry[1] else rest_entry
        age = (datetime.now(UTC) - ts).total_seconds()
        return snapshot, age

    def _fetch_wallet_balance(self, account_name: str) -> float:
        """Back-compat: USDT walletBalance float from a fresh REST snapshot."""
        return self._fetch_wallet_snapshot(account_name).wallet_balance

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
            # A never-fetched account is always eligible. Defaulting to 0.0
            # would skip it while time.monotonic() < floor — on Linux that is
            # seconds since boot, so fresh CI VMs silently no-op'd here.
            last = self._last_position_fetch.get(account_name)
            if last is not None and (now - last) < per_account_floor:
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

            # Fetch wallet snapshot (cached to reduce API calls) — balance plus
            # the account-level free-margin signal for the low-balance preflight
            # (feature 0066 / issue #159).
            # pybit's HTTP(timeout=...) caps the request; no extra wrapper.
            wallet = self.get_wallet_snapshot(account_name)
            wallet_balance = wallet.wallet_balance

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
                        available_balance=wallet.available_balance,
                        total_available_balance=wallet.total_available_balance,
                        total_maintenance_margin=wallet.total_maintenance_margin,
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
