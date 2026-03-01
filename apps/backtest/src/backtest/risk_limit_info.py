"""Risk limit tier fetcher with local caching.

Fetches per-symbol maintenance-margin tiers from Bybit API, caches locally.
Falls back to cache or hardcoded tiers if network unavailable.

Implementation is split across focused modules:
  - ``cache_lock`` — in-process and cross-process locking.
  - ``tier_serialization`` — MMTiers ↔ JSON dict conversion.
  - ``cache_validation`` — symlink / size / inode file checks.
"""

import json
import logging
import os
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gridcore.pnl import MMTiers, MM_TIERS, MM_TIERS_DEFAULT, parse_risk_limit_tiers

from backtest.cache_lock import (
    acquire_in_process_lock,
    release_in_process_lock,
    open_lock_file,
    acquire_file_lock,
    release_file_lock,
)
from backtest.cache_validation import (
    CacheSizeExceededError,
    cache_path_is_symlink,
    validate_and_open_cache_file,
    read_cache_from_fd,
)
from backtest.tier_serialization import tiers_to_dict, tiers_from_dict

if TYPE_CHECKING:
    from bybit_adapter.rest_client import BybitRestClient

logger = logging.getLogger(__name__)


# Re-export CacheSizeExceededError so existing imports keep working.
__all__ = ["CacheSizeExceededError", "RiskLimitProvider"]


# Default cache location (absolute to prevent path traversal)
DEFAULT_CACHE_PATH = Path(__file__).parent.parent.parent.parent / "conf" / "risk_limits_cache.json"

# Default allowed root directory for cache files (prevents path traversal)
DEFAULT_ALLOWED_CACHE_ROOT = Path(__file__).parent.parent.parent.parent / "conf"

# Maximum allowed cache file size (bytes) to prevent DoS via bloated files.
# 10 MB is generous for typical use (~50 symbols × ~10 tiers × ~200 bytes each
# ≈ 100 KB), but allows headroom for hundreds of symbols without triggering
# false positives.  Configurable per-instance via constructor or env var.
# 10 MB default allows ~50 symbols × ~10 tiers × ~200 bytes ≈ 100 KB with 100× headroom
_DEFAULT_MAX_CACHE_SIZE_BYTES = 10_000_000
# Minimum 1 KB prevents absurdly small limits that would break normal operation
_MIN_CACHE_SIZE_BYTES = 1_024
# Maximum 100 MB ceiling prevents DoS via unbounded env var configuration
_MAX_CACHE_SIZE_BYTES_LIMIT = 100_000_000


def _read_max_cache_size_from_env() -> int:
    """Read MAX_CACHE_SIZE_BYTES from GRIDBOT_RISK_CACHE_MAX_SIZE env var.

    Note: This function is called once at module import time.  The resulting
    value is stored in the module-level ``MAX_CACHE_SIZE_BYTES`` constant.
    Changes to the environment variable after import have no effect —
    restart the process or reload the module to pick up new values.
    """
    env_val = os.environ.get("GRIDBOT_RISK_CACHE_MAX_SIZE")
    if env_val is None:
        return _DEFAULT_MAX_CACHE_SIZE_BYTES
    try:
        value = int(env_val)
    except ValueError:
        logger.warning(
            "Invalid GRIDBOT_RISK_CACHE_MAX_SIZE=%r (not an integer), using default %d",
            env_val, _DEFAULT_MAX_CACHE_SIZE_BYTES,
        )
        return _DEFAULT_MAX_CACHE_SIZE_BYTES
    if value <= 0:
        logger.warning(
            "GRIDBOT_RISK_CACHE_MAX_SIZE=%d must be a positive integer, using default %d",
            value, _DEFAULT_MAX_CACHE_SIZE_BYTES,
        )
        return _DEFAULT_MAX_CACHE_SIZE_BYTES
    if value < _MIN_CACHE_SIZE_BYTES or value > _MAX_CACHE_SIZE_BYTES_LIMIT:
        logger.warning(
            "GRIDBOT_RISK_CACHE_MAX_SIZE=%d is outside safe range [%d, %d], using default %d",
            value, _MIN_CACHE_SIZE_BYTES, _MAX_CACHE_SIZE_BYTES_LIMIT,
            _DEFAULT_MAX_CACHE_SIZE_BYTES,
        )
        return _DEFAULT_MAX_CACHE_SIZE_BYTES
    return value


MAX_CACHE_SIZE_BYTES = _read_max_cache_size_from_env()
logger.debug("Configured MAX_CACHE_SIZE_BYTES=%d", MAX_CACHE_SIZE_BYTES)


class RiskLimitProvider:
    """Fetches and caches risk limit tier tables.

    Three-tier fallback strategy:
      1. **Cache** — local JSON file (default TTL: 24 hours).
      2. **Bybit API** — ``/v5/market/risk-limit`` via injected ``BybitRestClient``.
      3. **Hardcoded** — static tier tables in ``gridcore.pnl`` (last resort).

    Cache freshness uses a two-level check:
      1. **File mtime** (quick pre-check): if the entire cache file is older
         than *cache_ttl*, skip JSON parsing — no entry can be fresh.
      2. **Per-entry ``cached_at`` timestamp** (authoritative): each symbol's
         entry stores an ISO-8601 ``cached_at`` value written at save time.
         This is the actual freshness check. File mtime is only an optimization
         to avoid parsing when the whole file is clearly stale.

      Entries older than *cache_ttl* (default 24 h) are considered stale and
      trigger an API refresh, but stale data is still used as a fallback when
      the API is unreachable.
      Use ``force_fetch=True`` to bypass the cache entirely — recommended
      after a detected tier change or on startup for critical systems.

    Error handling strategy:
      Methods follow one of three patterns depending on the failure type:

      - **Return ``None``** for expected, recoverable failures where a
        fallback exists: ``load_from_cache()``, ``fetch_from_bybit()``.
        Callers should check the return value and fall through to the
        next tier in the fallback chain.
      - **Raise an exception** for programming errors or invalid state
        that cannot be recovered from: ``_ensure_open()`` raises
        ``RuntimeError`` when the provider has been ``close()``d;
        the constructor raises ``ValueError`` for invalid parameters
        (negative ``max_cache_size_bytes``, path outside allowed root).
      - **Catch, log, and continue** for optional side-effects that
        must not crash the caller: ``save_to_cache()`` catches
        ``PermissionError`` / ``OSError`` / ``ValueError`` from disk
        writes and logs a warning — a read-only filesystem never
        blocks tier lookups.

      ``get()`` always returns a valid ``MMTiers`` list (API, cache,
      or hardcoded fallback) unless the provider has been closed.

    Thread safety:
      Concurrent ``get()`` calls within a single process are safe — an
      internal ``threading.Lock`` serializes cache writes.  For
      multi-process safety, see the *cache_path* documentation and the
      README "Concurrent Access" section.

    Args:
        cache_path: Path to the JSON cache file. Resolved to an absolute
            canonical path at construction time. Must be within
            *allowed_cache_root* (when set).
        cache_ttl: Maximum age for a cached entry to be considered fresh.
            Affects both the mtime pre-check and the per-entry ``cached_at``
            comparison. All processes sharing a cache file should use the
            same TTL to avoid inconsistent freshness decisions.
        rest_client: Optional ``BybitRestClient`` for API fetching. When
            ``None``, the provider operates in offline mode (cache and
            hardcoded fallback only). This is the recommended setup for
            backtests to ensure reproducibility.
        max_cache_size_bytes: Upper bound on cache file size (bytes).
            Files exceeding this limit are rejected with
            ``CacheSizeExceededError``. Set to ``0`` to disable the check.
        allowed_cache_root: Directory that *cache_path* must reside under.
            Prevents path-traversal attacks. Set to ``None`` to disable.

    Example:
        from bybit_adapter.rest_client import BybitRestClient

        client = BybitRestClient(api_key="...", api_secret="...", testnet=False)
        provider = RiskLimitProvider(rest_client=client)
        tiers = provider.get("BTCUSDT")
        tiers = provider.get("BTCUSDT", force_fetch=True)  # bypass cache
    """

    def __init__(
        self,
        cache_path: Path = DEFAULT_CACHE_PATH,
        cache_ttl: timedelta = timedelta(hours=24),
        rest_client: Optional["BybitRestClient"] = None,
        max_cache_size_bytes: int = MAX_CACHE_SIZE_BYTES,
        allowed_cache_root: Optional[Path] = DEFAULT_ALLOWED_CACHE_ROOT,
    ):
        expanded_cache_path = cache_path.expanduser()
        # Freeze configured path to an absolute location so symlink checks
        # remain stable even if process CWD changes after initialization.
        self._configured_cache_path = expanded_cache_path.absolute()
        # Normalize path and resolve symlinks for a canonical cache location.
        self.cache_path = expanded_cache_path.resolve()
        # Validate cache path is within expected directory tree to prevent
        # path traversal attacks (e.g. cache_path="../../etc/passwd").
        if allowed_cache_root is not None:
            expected_root = allowed_cache_root.resolve()
            if not self.cache_path.is_relative_to(expected_root):
                raise ValueError(
                    f"Cache path {self.cache_path} is outside allowed directory {expected_root}"
                )
            # Ensure cache path (if it exists) is a regular file,
            # not a device, socket, pipe, or other special file type.
            if self.cache_path.exists():
                import stat as stat_mod

                if not stat_mod.S_ISREG(self.cache_path.lstat().st_mode):
                    raise ValueError(
                        f"Cache path {self.cache_path} is not a regular file"
                    )
        self.cache_ttl = cache_ttl
        self._rest_client = rest_client
        if max_cache_size_bytes < 0:
            raise ValueError(
                "max_cache_size_bytes must be non-negative. "
                "Set to 0 to disable size limit checks (allows unlimited cache file size)."
            )
        self.max_cache_size_bytes = max_cache_size_bytes
        self._in_process_lock_key, self._in_process_lock = acquire_in_process_lock(
            self.cache_path
        )
        self._closed = False
        # Guard concurrent access to _no_client_warned across threads.
        self._warn_lock = threading.Lock()
        self._no_client_warned = False
        # Deterministic cleanup without relying on __del__ timing.
        self._lock_finalizer = weakref.finalize(
            self, release_in_process_lock, self._in_process_lock_key
        )

    def close(self) -> None:
        """Release in-process lock reference and mark provider closed."""
        with self._in_process_lock:
            if self._closed:
                return
            self._closed = True
            self._rest_client = None
            self._lock_finalizer()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("RiskLimitProvider is closed")

    def _cache_path_is_symlink(self) -> bool:
        return cache_path_is_symlink(self._configured_cache_path, self.cache_path)

    def fetch_from_bybit(self, symbol: str) -> Optional[MMTiers]:
        """Fetch risk limit tiers from Bybit API via BybitRestClient.

        Uses the injected rest_client if available (recommended — goes through
        the shared rate limiter). Returns None if no client is configured.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")

        Returns:
            MMTiers if successful, None if failed or no client configured
        """
        self._ensure_open()
        if self._rest_client is None:
            with self._warn_lock:
                if not self._no_client_warned:
                    logger.warning(
                        "No rest_client configured — calculations may use stale or "
                        "hardcoded tier data. Provide a BybitRestClient for live data."
                    )
                    self._no_client_warned = True
            return None

        logger.debug(
            f"Fetching risk limit tiers for {symbol} via rest_client "
            f"(type={type(self._rest_client).__name__})"
        )

        # --- Network call: catch only transport-level errors ---
        try:
            raw_tiers = self._rest_client.get_risk_limit(symbol=symbol)
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Network error fetching risk limit tiers for {symbol}: {e}")
            return None

        if not raw_tiers:
            logger.warning(f"No risk limit tiers returned for {symbol}")
            return None

        # --- Data validation: catch only parsing / value errors ---
        try:
            tiers = parse_risk_limit_tiers(raw_tiers)
        except (ValueError, KeyError, ArithmeticError) as e:
            logger.warning(
                f"Data validation error parsing risk limit tiers for {symbol}: {e}"
            )
            return None

        logger.info(f"Fetched {len(tiers)} risk limit tiers for {symbol}")
        return tiers

    def load_from_cache(self, symbol: str) -> Optional[MMTiers]:
        """Load risk limit tiers from local cache.

        Args:
            symbol: Trading pair

        Returns:
            MMTiers if found in cache, None otherwise
        """
        self._ensure_open()
        tiers, _cached_at_str = self._load_cache_entry(symbol)
        return tiers

    def _load_cache_entry(self, symbol: str) -> tuple[Optional[MMTiers], Optional[str]]:
        """Load tiers and ``cached_at`` for a symbol with a single JSON parse."""
        if self._cache_path_is_symlink():
            logger.warning(f"Cache path must not be a symlink: {self._configured_cache_path}")
            return None, None

        try:
            fd = validate_and_open_cache_file(self.cache_path, self.max_cache_size_bytes)
            if fd is None:
                return None, None

            cache = read_cache_from_fd(fd)
            # fd is now closed by read_cache_from_fd

            entry = cache.get(symbol)
            if not isinstance(entry, dict) or not isinstance(entry.get("tiers"), list):
                return None, None

            tiers = tiers_from_dict(entry.get("tiers", []))
            cached_at = entry.get("cached_at")
            cached_at_str = cached_at if isinstance(cached_at, str) else None
            return (tiers if tiers else None), cached_at_str

        except CacheSizeExceededError as e:
            logger.warning(str(e))
        except OSError:
            # O_NOFOLLOW causes OSError if path is a symlink
            logger.warning("Cache file is a symlink or inaccessible (possible symlink swap)")
        except json.JSONDecodeError as e:
            logger.warning(f"Corrupted cache file {self.cache_path}: {e}")
        except (TypeError, ValueError, KeyError, ArithmeticError) as e:
            logger.warning(f"Invalid cache data for {symbol}: {e}")

        return None, None

    def save_to_cache(self, symbol: str, tiers: MMTiers) -> None:
        """Save risk limit tiers to local cache.

        Caching is optional — PermissionError / OSError are caught and logged
        so that a read-only filesystem never crashes the caller.

        Args:
            symbol: Trading pair
            tiers: Parsed tier table to cache
        """
        self._ensure_open()
        try:
            self._save_to_cache_impl(symbol, tiers)
        except (PermissionError, OSError, ValueError) as e:
            logger.warning(f"Failed to write cache file {self.cache_path}: {e}")

    def save_multiple_to_cache(self, items: dict[str, MMTiers]) -> None:
        """Save multiple symbols' tiers in a single read-modify-write cycle.

        More efficient than calling save_to_cache() per symbol because the
        cache file is read once, all entries are updated, and written once.

        Args:
            items: Mapping of symbol → tier table to cache
        """
        if not items:
            return
        self._ensure_open()
        try:
            self._save_multiple_to_cache_impl(items)
        except (PermissionError, OSError, ValueError) as e:
            logger.warning(f"Failed to write cache file {self.cache_path}: {e}")

    def _load_existing_cache(self) -> dict:
        """Load and return existing cache contents, or empty dict on failure."""
        if self._cache_path_is_symlink():
            raise ValueError("Cache path must not be a symlink")

        try:
            fd = validate_and_open_cache_file(self.cache_path, self.max_cache_size_bytes)
        except OSError:
            raise ValueError("Cache path must not be a symlink")

        if fd is None:
            return {}

        try:
            return read_cache_from_fd(fd)
        except ValueError:
            logger.warning(
                f"Invalid cache root in {self.cache_path}: expected JSON object; overwriting"
            )
            return {}
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Corrupted cache file {self.cache_path}, overwriting: {e}")
            return {}

    @staticmethod
    def _should_skip_cache_write(
        existing: object, new_tiers_dict: list[dict[str, str]]
    ) -> bool:
        """Return True if the cached entry already matches new_tiers_dict."""
        return (
            isinstance(existing, dict)
            and isinstance(existing.get("tiers"), list)
            and "cached_at" in existing
            and existing["tiers"] == new_tiers_dict
        )

    def _write_cache_file(self, cache: dict) -> None:
        """Atomically write the full cache dict to disk.

        Writes to a temporary file first, then renames to the target path.
        This prevents cache corruption if the process crashes mid-write.
        File permissions are set to 0o600 (owner-only read/write) to
        prevent other users from reading potentially sensitive tier data.
        """
        temp_path = self.cache_path.with_suffix(".tmp")
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(cache, f, indent=2)
            temp_path.replace(self.cache_path)
        except Exception:
            # Clean up temp file on failure (system-level exceptions like
            # KeyboardInterrupt/SystemExit are intentionally not caught to
            # avoid interfering with process shutdown)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked_cache(self):
        """Validate paths, acquire locks, load cache, and yield it.

        Context manager that encapsulates the shared boilerplate for
        cache write operations:
          1. Acquires in-process threading lock
          2. Validates provider is open and cache path is not a symlink
          3. Ensures parent directory exists
          4. Acquires cross-process file lock
          5. Loads existing cache contents
          6. Yields the cache dict for caller to modify and write

        The caller is responsible for calling ``_write_cache_file(cache)``
        inside the ``with`` block if changes were made.
        """
        with self._in_process_lock:
            self._ensure_open()
            if self._cache_path_is_symlink():
                raise ValueError("Cache path must not be a symlink")

            self.cache_path.parent.mkdir(parents=True, exist_ok=True)

            lock_path = self.cache_path.with_suffix(f"{self.cache_path.suffix}.lock")
            if lock_path.parent != self.cache_path.parent:
                raise ValueError("Lock file path outside cache directory")
            lock_file = None
            try:
                lock_file = open_lock_file(lock_path)
                acquire_file_lock(lock_file)

                yield self._load_existing_cache()
            finally:
                if lock_file is not None:
                    try:
                        release_file_lock(lock_file)
                    finally:
                        lock_file.close()

    def _save_to_cache_impl(self, symbol: str, tiers: MMTiers) -> None:
        """Perform the actual read-modify-write cache update.

        Performance note: This method reads the entire cache file, updates
        one symbol entry, and writes the whole file back.  The
        read-modify-write cycle is serialized by both an in-process
        threading lock and a cross-process file lock, so concurrent
        writers block rather than corrupt.  For low concurrency (1-5
        processes) this is acceptable (~1-5 ms per write).  At higher
        concurrency, use separate cache files per process — see the
        README "Concurrent Access" section for benchmarks.

        Hint: When updating many symbols at once, prefer
        ``save_multiple_to_cache()`` which batches all updates into a
        single read-modify-write pass.

        An early-exit optimization skips the write entirely when the
        existing cached tiers already match the new ones (same tier list,
        only ``cached_at`` would change).
        """
        with self._locked_cache() as cache:
            new_tiers_dict = tiers_to_dict(tiers)
            if self._should_skip_cache_write(cache.get(symbol), new_tiers_dict):
                return

            cache[symbol] = {
                "tiers": new_tiers_dict,
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_cache_file(cache)

        logger.info(f"Cached risk limit tiers for {symbol}")

    def _save_multiple_to_cache_impl(self, items: dict[str, MMTiers]) -> None:
        """Perform a single read-modify-write for multiple symbols.

        Same locking strategy as _save_to_cache_impl but updates all
        symbols in one pass, avoiding repeated file I/O.
        """
        with self._locked_cache() as cache:
            now = datetime.now(timezone.utc).isoformat()
            updated = False

            for symbol, tiers in items.items():
                new_tiers_dict = tiers_to_dict(tiers)
                if self._should_skip_cache_write(cache.get(symbol), new_tiers_dict):
                    continue
                cache[symbol] = {
                    "tiers": new_tiers_dict,
                    "cached_at": now,
                }
                updated = True

            if updated:
                self._write_cache_file(cache)

        if updated:
            logger.info(f"Cached risk limit tiers for {len(items)} symbols")

    def _is_cache_fresh(self, symbol: str, cached_at_str: Optional[str] = None) -> bool:
        """Check if cached entry for symbol is younger than cache_ttl.

        Uses file mtime as a quick pre-check: if the entire file is older
        than cache_ttl, the per-symbol entry cannot be fresh either.

        When ``cached_at_str`` is already known (passed by caller), the
        expensive ``_load_existing_cache()`` JSON parse is skipped entirely.
        """
        if self._cache_path_is_symlink():
            return False
        if not self.cache_path.exists():
            return False
        try:
            # Single try block: lstat, size check, and mtime are all
            # subject to the same race (file deleted/replaced between
            # calls).  Catching OSError here handles all those cases.
            cache_stat = self.cache_path.lstat()
            if self.max_cache_size_bytes and cache_stat.st_size > self.max_cache_size_bytes:
                return False

            # Quick pre-check: if file mtime is older than TTL, skip parsing.
            # Return early before the expensive _load_existing_cache() call.
            file_mtime = datetime.fromtimestamp(
                cache_stat.st_mtime, tz=timezone.utc
            )
            if datetime.now(timezone.utc) - file_mtime > self.cache_ttl:
                return False

            # When cached_at_str is not provided, the mtime pre-check above
            # already confirmed the file was modified within TTL.  Use mtime
            # as a sufficient freshness signal and avoid the expensive
            # _load_existing_cache() JSON parse.  The primary caller (get())
            # always passes cached_at_str from a prior _load_cache_entry()
            # call, so this fast-path rarely executes.
            if cached_at_str is None:
                return True
            if not cached_at_str:
                return False

            cached_at = datetime.fromisoformat(cached_at_str)
            age = datetime.now(timezone.utc) - cached_at
            return age < self.cache_ttl

        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False

    def get(self, symbol: str, force_fetch: bool = False) -> MMTiers:
        """Get risk limit tiers, fetching from API if needed.

        Strategy:
        1. If force_fetch, try API first
        2. Otherwise, try cache first
        3. If cache miss or stale, try API
        4. If API succeeds, update cache
        5. If API fails, use cache (if available) or hardcoded fallback

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            force_fetch: If True, always try API first

        Returns:
            MMTiers (from API, cache, or hardcoded fallback)

        Example:
            >>> provider = RiskLimitProvider(rest_client=client)
            >>> tiers = provider.get("BTCUSDT")
            >>> provider.get("BTCUSDT", force_fetch=True)  # bypass cache
        """
        self._ensure_open()
        cached: Optional[MMTiers] = None
        cached_at_str: Optional[str] = None

        # Try cache first (unless force_fetch or stale)
        if not force_fetch:
            cached, cached_at_str = self._load_cache_entry(symbol)
            if cached and self._is_cache_fresh(symbol, cached_at_str=cached_at_str):
                logger.debug(f"Using cached risk limit tiers for {symbol}")
                return cached
            if cached:
                logger.debug(f"Risk limit cache stale for {symbol}, refreshing...")

        # Try API
        fetched = self.fetch_from_bybit(symbol)
        if fetched:
            self.save_to_cache(symbol, fetched)
            return fetched

        # API failed, try cache as fallback.
        # Only load from cache if we skipped it earlier (force_fetch=True).
        # When force_fetch=False, cached is already set from the first load.
        if cached is None and force_fetch:
            cached, cached_at_str = self._load_cache_entry(symbol)
        if cached:
            staleness = ""
            if cached_at_str:
                staleness = f" (cached_at={cached_at_str})"
            logger.warning(
                f"API unavailable for {symbol}, using potentially stale cached "
                f"risk limits{staleness}. Margin calculations may be inaccurate."
            )
            return cached

        # No cache, use hardcoded fallback
        logger.warning(
            f"No risk limit data for {symbol}, using hardcoded fallback. "
            f"Margin calculations may be inaccurate — connect a BybitRestClient "
            f"to fetch current tiers."
        )
        return MM_TIERS.get(symbol, MM_TIERS_DEFAULT)


# ---------------------------------------------------------------------------
# Backward-compatible aliases for private helpers used by tests.
# These delegate to the extracted modules so existing test imports
# (e.g. ``from backtest.risk_limit_info import _tiers_to_dict``) keep working.
# ---------------------------------------------------------------------------
_tiers_to_dict = tiers_to_dict
_tiers_from_dict = tiers_from_dict
_acquire_in_process_lock = acquire_in_process_lock
_release_in_process_lock = release_in_process_lock
_open_lock_file = open_lock_file
_acquire_file_lock = acquire_file_lock
_release_file_lock = release_file_lock
