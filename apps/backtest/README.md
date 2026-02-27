# backtest

Grid trading strategy backtester using historical market data.

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

### Force Cache Refresh

To bypass the cache and fetch fresh tiers from the API:

```python
tiers = provider.get("BTCUSDT", force_fetch=True)
```

**Warning:** The default `cache_ttl` of 24 hours means tier changes on Bybit may not be reflected until the cache expires. For critical systems (live trading, production backtests), use `force_fetch=True` on startup to ensure you always start with the latest tiers from the API.

### Without API Access

When no `rest_client` is provided (e.g., offline backtesting), the provider uses cached data or falls back to hardcoded tier tables. No API calls are attempted.

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

**Using hardcoded fallback tiers**

When both the API and cache are unavailable, the provider uses hardcoded tier tables from `gridcore.pnl.MM_TIERS`. These are static snapshots and may become outdated if Bybit changes their risk limits. If you see the log message "using hardcoded fallback", ensure API access is restored to get accurate margin calculations.
