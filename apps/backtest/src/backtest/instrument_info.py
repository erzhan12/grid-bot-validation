"""Instrument info fetcher with local caching.

Fetches qty_step and tick_size from Bybit API, caches locally.
Falls back to cache if network unavailable.
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default cache location
DEFAULT_CACHE_PATH = Path("conf/instruments_cache.json")


class InstrumentInfo:
    """Instrument trading parameters."""

    def __init__(
        self,
        symbol: str,
        qty_step: Decimal,
        tick_size: Decimal,
        min_qty: Decimal,
        max_qty: Decimal,
    ):
        self.symbol = symbol
        self.qty_step = qty_step
        self.tick_size = tick_size
        self.min_qty = min_qty
        self.max_qty = max_qty

    def round_qty(self, qty: Decimal) -> Decimal:
        """Round quantity up to nearest qty_step (matching bbu2 behavior)."""
        steps = math.ceil(float(qty) / float(self.qty_step))
        return Decimal(str(steps)) * self.qty_step

    def round_price(self, price: Decimal) -> Decimal:
        """Round price to nearest tick_size."""
        steps = round(float(price) / float(self.tick_size))
        return Decimal(str(steps)) * self.tick_size

    def to_dict(self) -> dict:
        """Convert to dictionary for caching."""
        return {
            "symbol": self.symbol,
            "qty_step": str(self.qty_step),
            "tick_size": str(self.tick_size),
            "min_qty": str(self.min_qty),
            "max_qty": str(self.max_qty),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InstrumentInfo":
        """Create from cached dictionary."""
        return cls(
            symbol=data["symbol"],
            qty_step=Decimal(data["qty_step"]),
            tick_size=Decimal(data["tick_size"]),
            min_qty=Decimal(data["min_qty"]),
            max_qty=Decimal(data["max_qty"]),
        )


class InstrumentInfoProvider:
    """Fetches and caches instrument trading parameters.

    Tries cache first, then Bybit API, falls back to defaults.
    """

    def __init__(
        self,
        cache_path: Path = DEFAULT_CACHE_PATH,
        cache_ttl: timedelta = timedelta(hours=24),
    ):
        self.cache_path = cache_path
        self.cache_ttl = cache_ttl

    def fetch_from_bybit(self, symbol: str) -> Optional[InstrumentInfo]:
        """Fetch instrument info from Bybit API using pybit.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")

        Returns:
            InstrumentInfo if successful, None if failed
        """
        try:
            from pybit.unified_trading import HTTP

            # Create session without API keys (public endpoint)
            session = HTTP()
            response = session.get_instruments_info(category="linear", symbol=symbol)

            if response.get("retCode") != 0:
                logger.warning(f"Bybit API error: {response.get('retMsg')}")
                return None

            instruments = response.get("result", {}).get("list", [])
            if not instruments:
                logger.warning(f"No instrument found for {symbol}")
                return None

            info = instruments[0]
            lot_filter = info.get("lotSizeFilter", {})
            price_filter = info.get("priceFilter", {})

            qty_step = Decimal(lot_filter.get("qtyStep", "0.001"))
            tick_size = Decimal(price_filter.get("tickSize", "0.1"))

            if qty_step <= 0 or tick_size <= 0:
                logger.warning(
                    f"Invalid instrument params for {symbol}: "
                    f"qty_step={qty_step}, tick_size={tick_size}"
                )
                return None

            return InstrumentInfo(
                symbol=symbol,
                qty_step=qty_step,
                tick_size=tick_size,
                min_qty=Decimal(lot_filter.get("minOrderQty", "0.001")),
                max_qty=Decimal(lot_filter.get("maxOrderQty", "1000")),
            )

        except Exception as e:
            logger.warning(f"Error fetching instrument info: {e}")
            return None

    def load_from_cache(self, symbol: str) -> Optional[InstrumentInfo]:
        """Load instrument info from local cache.

        Args:
            symbol: Trading pair

        Returns:
            InstrumentInfo if found in cache, None otherwise
        """
        if not self.cache_path.exists():
            return None

        try:
            with open(self.cache_path) as f:
                cache = json.load(f)

            if symbol in cache:
                return InstrumentInfo.from_dict(cache[symbol])

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Error reading cache: {e}")

        return None

    def save_to_cache(self, info: InstrumentInfo) -> None:
        """Save instrument info to local cache.

        Args:
            info: InstrumentInfo to cache
        """
        cache = {}

        # Load existing cache
        if self.cache_path.exists():
            try:
                with open(self.cache_path) as f:
                    cache = json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass

        # Update cache with timestamp
        entry = info.to_dict()
        entry["cached_at"] = datetime.now(timezone.utc).isoformat()
        cache[info.symbol] = entry

        # Ensure directory exists
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Write cache
        with open(self.cache_path, "w") as f:
            json.dump(cache, f, indent=2)

        logger.info(f"Cached instrument info for {info.symbol}")

    def _is_cache_fresh(self, symbol: str) -> bool:
        """Check if cached entry for symbol is younger than CACHE_TTL."""
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

    def get(self, symbol: str, force_fetch: bool = False) -> InstrumentInfo:
        """Get instrument info, fetching from API if needed.

        Strategy:
        1. If force_fetch, try API first
        2. Otherwise, try cache first
        3. If cache miss or force_fetch, try API
        4. If API succeeds, update cache
        5. If API fails, use cache (if available) or defaults

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            force_fetch: If True, always try API first

        Returns:
            InstrumentInfo (from API, cache, or defaults)
        """
        # Try cache first (unless force_fetch or stale)
        if not force_fetch:
            cached = self.load_from_cache(symbol)
            if cached and self._is_cache_fresh(symbol):
                logger.debug(f"Using cached instrument info for {symbol}")
                return cached
            if cached:
                logger.debug(f"Cache stale for {symbol}, refreshing...")

        # Try API
        fetched = self.fetch_from_bybit(symbol)
        if fetched:
            self.save_to_cache(fetched)
            return fetched

        # API failed, try cache as fallback
        cached = self.load_from_cache(symbol)
        if cached:
            logger.warning(f"API unavailable, using cached info for {symbol}")
            return cached

        # No cache, use defaults
        logger.warning(f"No instrument info for {symbol}, using defaults")
        return InstrumentInfo(
            symbol=symbol,
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
        )
