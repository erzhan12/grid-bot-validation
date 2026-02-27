"""Risk limit tier fetcher with local caching.

Fetches per-symbol maintenance-margin tiers from Bybit API, caches locally.
Falls back to cache or hardcoded tiers if network unavailable.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gridcore.pnl import MMTiers, MM_TIERS, MM_TIERS_DEFAULT, parse_risk_limit_tiers

if TYPE_CHECKING:
    from bybit_adapter.rest_client import BybitRestClient

logger = logging.getLogger(__name__)

# Default cache location (absolute to prevent path traversal)
DEFAULT_CACHE_PATH = Path(__file__).parent.parent.parent.parent / "conf" / "risk_limits_cache.json"


class RiskLimitProvider:
    """Fetches and caches risk limit tier tables.

    Three-tier fallback strategy:
      1. **Cache** — local JSON file (default TTL: 24 hours).
      2. **Bybit API** — ``/v5/market/risk-limit`` via injected ``BybitRestClient``.
      3. **Hardcoded** — static tier tables in ``gridcore.pnl`` (last resort).

    Cache behaviour:
      - Entries older than *cache_ttl* (default 24 h) are considered stale and
        trigger an API refresh, but stale data is still used as a fallback when
        the API is unreachable.
      - Use ``force_fetch=True`` to bypass the cache entirely — recommended
        after a detected tier change or on startup for critical systems.

    Error handling:
      - Corrupted cache files are logged and skipped (non-fatal).
      - API errors trigger fallback to cache or hardcoded tiers.
      - All errors are logged but never raised — ``get()`` always returns
        a valid ``MMTiers`` list.
      - Cache files exceeding 10 MB are rejected to prevent DoS.

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
    ):
        self.cache_path = cache_path
        self.cache_ttl = cache_ttl
        self._rest_client = rest_client

    def fetch_from_bybit(self, symbol: str) -> Optional[MMTiers]:
        """Fetch risk limit tiers from Bybit API via BybitRestClient.

        Uses the injected rest_client if available (recommended — goes through
        the shared rate limiter). Returns None if no client is configured.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")

        Returns:
            MMTiers if successful, None if failed or no client configured
        """
        if self._rest_client is None:
            logger.debug("No rest_client configured, skipping API fetch")
            return None

        try:
            raw_tiers = self._rest_client.get_risk_limit(symbol=symbol)
            if not raw_tiers:
                logger.warning(f"No risk limit tiers returned for {symbol}")
                return None

            tiers = parse_risk_limit_tiers(raw_tiers)
            logger.info(f"Fetched {len(tiers)} risk limit tiers for {symbol}")
            return tiers

        except (ConnectionError, TimeoutError, ValueError, KeyError) as e:
            logger.warning(f"Error fetching risk limit tiers: {e}")
            return None

    def load_from_cache(self, symbol: str) -> Optional[MMTiers]:
        """Load risk limit tiers from local cache.

        Args:
            symbol: Trading pair

        Returns:
            MMTiers if found in cache, None otherwise
        """
        if not self.cache_path.exists():
            return None

        try:
            with open(self.cache_path) as f:
                cache = json.load(f)

            if symbol in cache:
                tiers = _tiers_from_dict(cache[symbol].get("tiers", []))
                return tiers if tiers else None

        except json.JSONDecodeError as e:
            logger.warning(f"Corrupted cache file {self.cache_path}: {e}")
        except ValueError as e:
            logger.warning(f"Invalid tier data format in cache for {symbol}: {e}")

        return None

    def save_to_cache(self, symbol: str, tiers: MMTiers) -> None:
        """Save risk limit tiers to local cache.

        Caching is optional — PermissionError / OSError are caught and logged
        so that a read-only filesystem never crashes the caller.

        Args:
            symbol: Trading pair
            tiers: Parsed tier table to cache
        """
        try:
            self._save_to_cache_impl(symbol, tiers)
        except (PermissionError, OSError, ValueError) as e:
            logger.warning(f"Failed to write cache file {self.cache_path}: {e}")

    def _save_to_cache_impl(self, symbol: str, tiers: MMTiers) -> None:
        cache = {}

        # Load existing cache
        if self.cache_path.exists():
            if self.cache_path.stat().st_size > 10_000_000:
                raise ValueError("Cache file exceeds 10MB limit")
            try:
                with open(self.cache_path) as f:
                    cache = json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Corrupted cache file {self.cache_path}, overwriting: {e}")
                cache = {}

        # Skip write if tiers haven't changed (direct equality check)
        existing = cache.get(symbol)
        new_tiers_dict = _tiers_to_dict(tiers)
        if (
            existing
            and isinstance(existing, dict)
            and isinstance(existing.get("tiers"), list)
            and "cached_at" in existing
            and existing["tiers"] == new_tiers_dict
        ):
            return

        # Update cache with timestamp
        cache[symbol] = {
            "tiers": new_tiers_dict,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }

        # Ensure directory exists
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Write cache
        with open(self.cache_path, "w") as f:
            json.dump(cache, f, indent=2)

        logger.info(f"Cached risk limit tiers for {symbol}")

    def _is_cache_fresh(self, symbol: str) -> bool:
        """Check if cached entry for symbol is younger than cache_ttl."""
        if not self.cache_path.exists():
            return False

        try:
            with open(self.cache_path) as f:
                cache = json.load(f)

            entry = cache.get(symbol, {})
            cached_at_str = entry.get("cached_at")
            if not cached_at_str:
                return False

            cached_at = datetime.fromisoformat(cached_at_str)
            age = datetime.now(timezone.utc) - cached_at
            return age < self.cache_ttl

        except (json.JSONDecodeError, ValueError):
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
        # Try cache first (unless force_fetch or stale)
        if not force_fetch:
            cached = self.load_from_cache(symbol)
            if cached and self._is_cache_fresh(symbol):
                logger.debug(f"Using cached risk limit tiers for {symbol}")
                return cached
            if cached:
                logger.debug(f"Risk limit cache stale for {symbol}, refreshing...")

        # Try API
        fetched = self.fetch_from_bybit(symbol)
        if fetched:
            self.save_to_cache(symbol, fetched)
            return fetched

        # API failed, try cache as fallback
        cached = self.load_from_cache(symbol)
        if cached:
            logger.warning(f"API unavailable, using cached risk limits for {symbol}")
            return cached

        # No cache, use hardcoded fallback
        logger.warning(f"No risk limit data for {symbol}, using hardcoded fallback")
        return MM_TIERS.get(symbol, MM_TIERS_DEFAULT)


def _tiers_to_dict(tiers: MMTiers) -> list[dict[str, str]]:
    """Serialize MMTiers to JSON-compatible list of dicts."""
    return [
        {
            "max_value": str(max_val),
            "mmr_rate": str(mmr_rate),
            "deduction": str(deduction),
            "imr_rate": str(imr_rate),
        }
        for max_val, mmr_rate, deduction, imr_rate in tiers
    ]


def _tiers_from_dict(tier_dicts: list[dict[str, str]]) -> MMTiers:
    """Deserialize MMTiers from cached list of dicts.

    Handles old cache files that lack ``imr_rate`` by defaulting to "0".
    """
    return [
        (
            Decimal(d["max_value"]),
            Decimal(d["mmr_rate"]),
            Decimal(d["deduction"]),
            Decimal(d.get("imr_rate", "0")),
        )
        for d in tier_dicts
    ]
