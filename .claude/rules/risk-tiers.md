---
paths:
  - "packages/gridcore/src/gridcore/pnl.py"
  - "apps/backtest/src/backtest/risk_limit_info.py"
  - "apps/backtest/src/backtest/cache_lock.py"
  - "apps/backtest/src/backtest/tier_serialization.py"
  - "apps/backtest/src/backtest/cache_validation.py"
  - "packages/bybit_adapter/src/bybit_adapter/rest_client.py"
  - "apps/pnl_checker/src/pnl_checker/calculator.py"
  - "apps/pnl_checker/src/pnl_checker/fetcher.py"
  - "scripts/check_tier_drift.py"
---

## Dynamic Risk Limit Tiers

Per-symbol maintenance-margin tiers are now fetched from Bybit's `/v5/market/risk-limit` API instead of relying solely on hardcoded tables. This fixed LTCUSDT MM mismatch (our DEFAULT used 1% MMR at $1M, Bybit actual is 1% at $200k).

### Architecture

- **`gridcore/pnl.py`** — Single source of truth. `calc_maintenance_margin()` accepts optional `tiers: MMTiers` param. When `None`, falls back to hardcoded lookup. Hardcoded tables (`MM_TIERS_BTCUSDT`, `MM_TIERS_ETHUSDT`, `MM_TIERS_DEFAULT`) remain as fallback.
- **`MMTiers`** type alias: `list[tuple[Decimal, Decimal, Decimal, Decimal]]` — `(max_position_value, mmr_rate, deduction, imr_rate)`
- **`parse_risk_limit_tiers()`** — Converts Bybit API response to `MMTiers`. Sorts by `riskLimitValue`, handles empty/missing `mmDeduction`/`initialMargin`, replaces last tier cap with `Infinity`. Validates MMR/IMR rates are in `[0, 1]` and `riskLimitValue` is a valid positive number or "Infinity".

### Consumers

| Consumer | How tiers are fetched | Fallback |
|----------|----------------------|----------|
| pnl_checker | `BybitRestClient.get_risk_limit()` in `fetcher.py` → passed as `tiers=` to `calc_maintenance_margin` | Hardcoded tables |
| backtest | `RiskLimitProvider` with local JSON cache (24h TTL) | Cache → hardcoded tables |

### Key patterns

1. **`RiskLimitProvider` uses dependency injection** — accepts `rest_client: Optional[BybitRestClient]` in `__init__()`. Without a client, it uses cache-only/hardcoded fallback (no API calls). File: `apps/backtest/src/backtest/risk_limit_info.py`
2. **Non-fatal failure** — Risk limit fetch failures return `None` everywhere. `calc_maintenance_margin(tiers=None)` gracefully falls back to hardcoded tables. No crash path.
3. **Cache strategy** — `RiskLimitProvider.get()`: fresh cache → API → stale cache → hardcoded fallback. Cache at `conf/risk_limits_cache.json`, 24h TTL. Force refresh: `provider.get("BTCUSDT", force_fetch=True)`.
4. **`get_risk_limit()` is a public endpoint** — No API keys needed. In pnl_checker it goes through the authenticated `BybitRestClient` (shared rate limiter). In backtest it uses the injected client.

### Files Involved
- `packages/gridcore/src/gridcore/pnl.py` — `MMTiers` type, hardcoded fallback tiers (`MM_TIERS_BTCUSDT`, etc.), `parse_risk_limit_tiers()`, `calc_maintenance_margin()`, `calc_initial_margin()`
- `apps/backtest/src/backtest/risk_limit_info.py` — `RiskLimitProvider` orchestrator (fetch, cache, fallback)
- `apps/backtest/src/backtest/cache_lock.py` — In-process and cross-process locking helpers
- `apps/backtest/src/backtest/tier_serialization.py` — MMTiers ↔ JSON dict serialization
- `apps/backtest/src/backtest/cache_validation.py` — Symlink, size, and inode file validation
- `packages/bybit_adapter/src/bybit_adapter/rest_client.py` — `get_risk_limit()` API call (`_unwrap_risk_limit_response` raises `ValueError` on unexpected structure)
- `apps/pnl_checker/src/pnl_checker/calculator.py` — Uses tiers for IM/MM calculation
- `apps/pnl_checker/src/pnl_checker/fetcher.py` — Fetches risk limits per symbol
- `scripts/check_tier_drift.py` — Compares hardcoded tiers against live API (weekly CI via `.github/workflows/risk-tier-monitor.yml`). Lint-covered via `make lint`'s explicit path despite the `scripts/` ruff exclude (issue #215 / feature 0091).

### Caching Strategy (3-Tier Fallback)
1. **Cache** — Local JSON file, default TTL 24 hours. Stale cache is still used when API fails.
2. **Bybit API** — `/v5/market/risk-limit` via `BybitRestClient`.
3. **Hardcoded** — Static tiers in `gridcore.pnl` (last resort, verified 2025-02-27).

### Error Handling
- Corrupted cache → logged, skipped (non-fatal)
- API errors → fallback to cache, then hardcoded
- Cache >10MB → rejected (DoS prevention), `save_to_cache()` catches `ValueError` and logs warning
- Empty tier list from API → returns None, triggers fallback
- `get()` never raises — always returns valid `MMTiers`
- Invalid `riskLimitValue` format → `parse_risk_limit_tiers` raises `ValueError` with descriptive message
- MMR/IMR rates outside `[0, 1]` → `parse_risk_limit_tiers` raises `ValueError`

### Key Pitfalls
1. **Empty tier list**: `parse_risk_limit_tiers([])` raises `ValueError`. Always check for empty before calling.
2. **Corrupted cache**: Handled gracefully — `load_from_cache()` catches `json.JSONDecodeError` and `ValueError`.
3. **Stale hardcoded values**: The hardcoded tiers in `pnl.py` should be periodically verified against the Bybit API. Check the "Last verified" timestamp comment.
4. **None risk_limit_tiers**: When fetcher returns `None`, calculator must fallback to `MM_TIERS.get(symbol, MM_TIERS_DEFAULT)`.
5. **Negative prices**: `calc_unrealised_pnl_pct` validates prices > 0; negative prices log a warning and return 0.
6. **Input validation**: `parse_risk_limit_tiers` rejects negative, zero, and NaN `riskLimitValue`, invalid Decimal formats, MMR/IMR rates outside `[0, 1]`, negative `mmDeduction`, and duplicate/out-of-order tier boundaries. Zero MMR/IMR rates log a warning (infinite leverage indicator).
7. **Cache path security**: `cache_path` is resolved via `.resolve()` in `__init__` to prevent directory traversal via `..` components. `DEFAULT_CACHE_PATH` uses `Path(__file__)` (not `Path.cwd()`) so the path is relative to package location.
8. **Cache skip-write optimization**: Uses direct dict equality (`==`) instead of SHA-256 hashing for comparing tier data. Simpler and faster for small tier dicts.
9. **Decimal conversion safety**: All Decimal conversions in `parse_risk_limit_tiers` are wrapped in try-except to catch `InvalidOperation` from malformed API responses. Error messages include field name and value for debugging.
10. **Negative leverage guard**: `calc_initial_margin` uses `leverage <= 0` (not `== 0`) in fallback path. The calculator also guards at the call site via `MIN_LEVERAGE` threshold.
11. **_is_cache_fresh optimization**: Uses file mtime as a quick pre-check before parsing JSON. If the file hasn't been modified within the TTL window, skips parsing entirely.
12. **Conditional position manager resets**: In `_calc_risk_multipliers`, only reset managers with open positions to avoid unnecessary work.
13. **rest_client `get_risk_limit()` structure**: Bybit API returns nested `{"list": [{"list": [tier, ...]}]}`. The parser unwraps the first symbol's inner list. Flat lists (missing inner `"list"` key) return empty `[]` and log an error — they are never passed through as-is.
14. **_open_lock_file TOCTOU**: Uses `os.lstat()` (not `is_symlink()`) for pre-check and always validates path identity post-open via inode/device comparison, regardless of O_NOFOLLOW support.
15. **Negative position_value**: `calc_initial_margin` logs a warning and returns zero for negative `position_value` (likely a data error).
16. **In-process lock registry location**: `_IN_PROCESS_LOCKS` and `acquire_in_process_lock` / `release_in_process_lock` live in `cache_lock.py`, not `risk_limit_info.py`. Integration tests that assert ref-counts must import `backtest.cache_lock`; oversized-cache warnings log `CacheSizeExceededError` text (`"Cache file size"` … `"exceeds"`), not the legacy `"Cache file exceeds"` substring.

## Risk Limit Cache Format Evolution

**Cache format versions** (apps/backtest/conf/risk_limits_cache.json):
- v1 (pre-2026-02-28): `{max_value, mmr_rate, deduction}` (3 fields)
- v2 (2026-02-28): Added `imr_rate` field (4 fields total)

**Backward compatibility**: `tier_serialization.tiers_from_dict()` defaults `imr_rate="0"` for old cache entries.

**Migration**: Old cache files are automatically upgraded on next write. No manual intervention needed.

**Symlink Attack Prevention**: The TOCTOU defense pattern in `cache_lock.py` and `cache_validation.py`:
1. Open with `O_NOFOLLOW` to atomically reject symlinks
2. Post-open `fstat` vs `lstat` inode/device comparison detects symlink swaps
This pattern should be used for all security-sensitive file operations.
