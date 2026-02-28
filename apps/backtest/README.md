# backtest

Grid trading strategy backtester using historical market data.

## Quick Start

```python
from backtest.risk_limit_info import RiskLimitProvider

# Create a provider (offline mode — uses cached/hardcoded tiers)
provider = RiskLimitProvider()

# Get risk limit tiers for a symbol
tiers = provider.get("BTCUSDT")

# Each tier: (max_position_value, mmr_rate, deduction, imr_rate)
for max_val, mmr, ded, imr in tiers:
    print(f"  Up to {max_val}: MMR={mmr}, deduction={ded}, IMR={imr}")
```

To use live API data, provide a `BybitRestClient`:

```python
from bybit_adapter.rest_client import BybitRestClient

client = BybitRestClient(api_key="...", api_secret="...", testnet=False)
provider = RiskLimitProvider(rest_client=client)
tiers = provider.get("BTCUSDT", force_fetch=True)
```

## Risk Limit Tiers

The backtest engine uses per-symbol maintenance-margin tier tables from Bybit to calculate accurate margin requirements. Tiers are fetched from the `/v5/market/risk-limit` API and cached locally.

### Caching Strategy

`RiskLimitProvider` follows a layered fallback strategy:

1. **Fresh cache** (< TTL age) -- returned immediately, no API call
2. **Bybit API** -- fetched when cache is missing or stale
3. **Stale cache** -- used when API is unavailable
4. **Hardcoded fallback** -- built-in BTCUSDT/ETHUSDT/default tables as last resort

Cache is stored as JSON at `conf/risk_limits_cache.json` (configurable via `cache_path`).

### Configuration

```python
from backtest.risk_limit_info import RiskLimitProvider

# Default: 24-hour TTL, conf/risk_limits_cache.json
provider = RiskLimitProvider()

# Custom TTL and cache path
from datetime import timedelta
from pathlib import Path

provider = RiskLimitProvider(
    cache_path=Path("my_cache/risk_limits.json"),
    cache_ttl=timedelta(hours=12),
)

# With a BybitRestClient for API fetching
from bybit_adapter.rest_client import BybitRestClient

client = BybitRestClient(api_key="...", api_secret="...", testnet=False)
provider = RiskLimitProvider(rest_client=client)
```

See [Design Decisions](#design-decisions) below for why the backtest engine does not make API calls.

### Force Cache Refresh

To bypass the cache and fetch fresh tiers from the API:

```python
tiers = provider.get("BTCUSDT", force_fetch=True)
```

**Warning:** The default `cache_ttl` of 24 hours means tier changes on Bybit may not be reflected until the cache expires. For critical systems (live trading, production backtests), use `force_fetch=True` on startup, or immediately after Bybit announces risk limit tier changes, to ensure calculations use the latest API tiers. Note: `force_fetch=True` makes a synchronous API call and blocks until the response is received. In production, prefer scheduled cache refreshes rather than per-request force fetches.

**Concurrent access:** If running multiple backtest processes simultaneously, each should use a separate cache file path. Cache writes use file locking to prevent concurrent write corruption, but separate cache files are still recommended to reduce lock contention and keep per-process cache state isolated. When sharing cache files, all processes should use the same `cache_ttl` to avoid inconsistent freshness checks. For example, Process A with `cache_ttl=1h` and Process B with `cache_ttl=24h` reading the same cache will have different views of whether a cached entry is fresh or stale.

### Performance Considerations

Cache writes use file-level locking (POSIX `flock` / Windows `msvcrt.locking`) to prevent corruption when multiple processes share one cache file. This locking serializes all writers, which means:

- **Low concurrency (1-5 processes):** Locking overhead is negligible. Shared cache files work fine.
- **High concurrency (>5-10 processes):** Lock contention becomes a bottleneck because every write must wait for the previous one to complete the read-modify-write cycle. At this scale, use **separate cache files per process** to eliminate contention entirely:

```python
provider = RiskLimitProvider(
    cache_path=Path(f"/tmp/risk_cache_{os.getpid()}.json"),
)
```

Reads do not acquire file locks, so concurrent readers do not block each other or writers. However, a reader may see a partially-written file if it races with a writer. The provider handles this gracefully by falling back to hardcoded tiers on JSON parse errors.

**Practical recommendations:**

| Processes | Strategy | Notes |
|-----------|----------|-------|
| 1 | Shared cache (default) | No contention |
| 2-5 | Shared cache (default) | Lock wait < 1 ms per write typically |
| 6-10 | Separate cache files recommended | Lock contention becomes measurable |
| 10+ | Separate cache files **required** | Shared cache becomes a bottleneck |

Each read-modify-write cycle involves: open lock file, `flock(LOCK_EX)`, read JSON, update entry, write JSON, release lock. With a typical cache file (~50-100 KB for 50 symbols), this takes 1-5 ms per write on SSD storage. Benchmarked on SSD storage with ~50 KB cache files. Performance may vary on slower storage (e.g. NFS, HDD) or with larger cache files.

### Troubleshooting

**Corrupted cache file**

If the cache file becomes corrupted (e.g., partial write, manual edit error), delete it and let the provider rebuild it:

```bash
rm conf/risk_limits_cache.json
```

The provider will fetch fresh tiers from the API on the next run, or fall back to hardcoded tiers if the API is unavailable.

**API rate limits**

If Bybit rate limits are hit during tier fetching, the provider logs a warning and falls back to cached or hardcoded tiers. To reduce API calls:
- Increase `cache_ttl` (default is 24 hours)
- Use `force_fetch=False` (default) to prefer cached data

**Invalid tier data from API**

If Bybit returns malformed tier data, the provider will log a warning with the specific validation error and fall back to cached or hardcoded tiers. Check the logs for ValueError messages indicating which field failed validation (e.g., "Invalid riskLimitValue format", "MMR rate outside valid range").

**Empty tier list from API**

If Bybit returns an empty risk limit list (rare edge case), the provider returns `None` and falls back to hardcoded tiers. This indicates a potential API issue.

**Using hardcoded fallback tiers**

When both the API and cache are unavailable, the provider uses hardcoded tier tables from `gridcore.pnl.MM_TIERS`. These are static snapshots and may become outdated if Bybit changes their risk limits. If you see the log message "using hardcoded fallback", ensure API access is restored to get accurate margin calculations.

### Design Decisions

**No API calls during backtests (reproducibility)**

The backtest engine intentionally creates `RiskLimitProvider` without a `BybitRestClient` (see `apps/backtest/src/backtest/engine.py`). This means backtests use cached or hardcoded tier tables only and never fetch from the Bybit API at runtime. This is a deliberate design choice for **reproducibility**: backtest results should be deterministic and not depend on the current state of external APIs. Running the same backtest twice should produce identical margin calculations.

When no `rest_client` is provided (e.g., offline backtesting), the provider uses cached data or falls back to hardcoded tier tables. No API calls are attempted.

To update the cache with current API data **before** running backtests:

```python
from backtest.risk_limit_info import RiskLimitProvider
from bybit_adapter.rest_client import BybitRestClient

client = BybitRestClient(api_key="...", api_secret="...", testnet=False)
provider = RiskLimitProvider(rest_client=client)

# Force-refresh cache for symbols you plan to backtest
for symbol in ["BTCUSDT", "ETHUSDT"]:
    provider.get(symbol, force_fetch=True)
```

After this, backtests will pick up the refreshed cache automatically.

## Dynamic Risk Limit Tiers

The backtest engine supports dynamic risk limit tiers fetched from the Bybit `/v5/market/risk-limit` API. This ensures margin calculations use current tier boundaries rather than potentially outdated hardcoded values.

### How It Works

1. **Fetch**: `RiskLimitProvider` calls the Bybit API via `BybitRestClient.get_risk_limit(symbol)` to retrieve per-symbol maintenance-margin and initial-margin tier tables.
2. **Parse**: Raw API response is parsed by `gridcore.pnl.parse_risk_limit_tiers()`, which validates rates, sorts tiers by ascending `riskLimitValue`, and ensures the last tier's cap is `Infinity`.
3. **Cache**: Parsed tiers are saved to a local JSON file (`conf/risk_limits_cache.json` by default) with a timestamp for TTL-based freshness checks.
4. **Fallback**: If the API is unreachable, stale cached tiers are used. If no cache exists, hardcoded tier tables from `gridcore.pnl.MM_TIERS` serve as the final fallback.

### Security

Cache files may contain tier data fetched from production APIs and should be
treated as sensitive in multi-user environments:

- **File permissions** — Cache and lock files are created with mode `0o600`
  (owner-only read/write). Do not weaken these permissions.
- **Shared directories** — Avoid storing cache files in world-readable
  directories (e.g. `/tmp`). Prefer a project-local `conf/` directory or a
  user-owned path.
- **Symlink protection** — The provider rejects symlinks for both cache and
  lock file paths (`O_NOFOLLOW` + inode verification). This prevents symlink
  attacks that redirect cache reads/writes to unintended locations.
- **Path traversal** — The `allowed_cache_root` parameter (set by default to
  `conf/`) restricts cache file placement. It should not be set to `None` in
  production.

### Key Files

- `apps/backtest/src/backtest/risk_limit_info.py` — `RiskLimitProvider` orchestrator
- `apps/backtest/src/backtest/cache_lock.py` — In-process and cross-process locking
- `apps/backtest/src/backtest/tier_serialization.py` — MMTiers ↔ JSON dict conversion
- `apps/backtest/src/backtest/cache_validation.py` — Symlink, size, and inode validation
- `packages/gridcore/src/gridcore/pnl.py` — `parse_risk_limit_tiers()`, hardcoded `MM_TIERS`, margin calculation functions
- `packages/bybit_adapter/src/bybit_adapter/rest_client.py` — `BybitRestClient.get_risk_limit()` API call

### API Reference

- Bybit Risk Limit endpoint: https://bybit-exchange.github.io/docs/v5/market/risk-limit
