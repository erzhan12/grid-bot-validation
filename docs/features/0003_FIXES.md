# Feature 0003 Fixes (Phase D: Data Capture)

## Summary

This document tracks the fixes applied after the initial review in `docs/features/0003_REVIEW.md`.

Implemented:
- HIGH: startup/runtime blockers in `EventSaver` and `GapReconciler`
- MEDIUM: position/wallet persistence, uniqueness indexes for trades/executions, and new snapshot repositories
- LOW: collector cleanup (unused imports/fields)

Remaining issues are called out at the end (notably: gap detection callbacks still don’t fire, and the current `bulk_insert(...).on_conflict_do_nothing(...)` implementation needs follow-up).

## Fixes Applied

### 1. DatabaseFactory Initialization (HIGH)
**File**: `apps/event_saver/src/event_saver/main.py`

**Problem**: `DatabaseFactory` expects a `DatabaseSettings` object, but was receiving a raw URL string.
```python
# Before (BROKEN)
db = DatabaseFactory(config.database_url)

# After (FIXED)
db = DatabaseFactory(DatabaseSettings(database_url=config.database_url))
```

**Impact**: Application would crash at startup with `AttributeError` when `DatabaseFactory` tried to call `self.settings.get_database_url()`.

---

### 2. BybitRestClient Initialization (HIGH)
**File**: `apps/event_saver/src/event_saver/main.py`

**Problem**: `BybitRestClient` requires `api_key` and `api_secret` parameters, but was initialized without them.
```python
# Before (BROKEN)
self._rest_client = BybitRestClient(testnet=self._config.testnet)

# After (FIXED)
self._rest_client = BybitRestClient(
    api_key="",
    api_secret="",
    testnet=self._config.testnet,
)
```

**Impact**: Application would crash at startup with `TypeError: missing required arguments`.

**Note**: Empty credentials work for public endpoints. Future enhancement needed for per-account private reconciliation.

---

### 3. Async/Sync Mismatch - get_recent_trades (HIGH)
**File**: `apps/event_saver/src/event_saver/reconciler.py`

**Problem**: Called `await` on synchronous method `get_recent_trades()`.
```python
# Before (BROKEN)
trades_data = await self._rest_client.get_recent_trades(
    symbol=symbol,
    limit=1000,
)

# After (FIXED)
trades_data = self._rest_client.get_recent_trades(
    symbol=symbol,
    limit=1000,
)
```

**Impact**: Runtime error: `TypeError: object list can't be used in 'await' expression`.

---

### 4. Return Type Mismatch - get_executions (HIGH)
**File**: `apps/event_saver/src/event_saver/reconciler.py`

**Problem**: `get_executions()` returns a tuple `(list, cursor)`, but code treated it as a single list.
```python
# Before (BROKEN)
executions_data = await self._rest_client.get_executions(
    symbol=symbol,
    start_time=start_ms,
    end_time=end_ms,
    limit=100,
)

# After (FIXED)
executions_data, next_cursor = self._rest_client.get_executions(
    symbol=symbol,
    start_time=start_ms,
    end_time=end_ms,
    limit=100,
)
```

**Impact**: Runtime error when trying to iterate over tuple as if it were a list. Also combined with async/sync fix (removed `await`).

---

### 5. Repository API Parameter Mismatch (HIGH)
**File**: `apps/event_saver/src/event_saver/reconciler.py`

**Problem**: Called `exists_by_trade_id(symbol, trade_id)` with 2 parameters, but method signature only accepts 1 parameter.
```python
# Before (BROKEN)
if repo.exists_by_trade_id(symbol, model.trade_id):
    existing_ids.add(model.trade_id)

# After (FIXED)
if repo.exists_by_trade_id(model.trade_id):
    existing_ids.add(model.trade_id)
```

**Impact**: Runtime error: `TypeError: exists_by_trade_id() takes 2 positional arguments but 3 were given`.

---

## Additional Fixes / Improvements

### 6. Position + Wallet Persistence (MEDIUM)
**Files**:
- Writers: `apps/event_saver/src/event_saver/writers/position_writer.py`, `apps/event_saver/src/event_saver/writers/wallet_writer.py`
- Orchestrator wiring: `apps/event_saver/src/event_saver/main.py`
- Repos: `shared/db/src/grid_db/repositories.py`
- Models: `shared/db/src/grid_db/models.py`

**Change**: Position and wallet WebSocket messages are now persisted to the DB via buffered writers (not just logged).

### 7. Uniqueness Indexes for Deduplication (MEDIUM)
**File**: `shared/db/src/grid_db/models.py`

**Change**:
- `public_trades.trade_id` has a unique index
- `private_executions.exec_id` has a unique index

This enables DB-enforced deduplication during normal ingestion and reconciliation.

### 8. Collector Cleanup (LOW)
**Files**:
- `apps/event_saver/src/event_saver/collectors/public_collector.py`
- `apps/event_saver/src/event_saver/collectors/private_collector.py`

**Change**: Removed unused imports/fields identified in the initial review.

## Verification Notes

- The test suite should be re-verified after the reconciler changes.
- `apps/event_saver/tests/test_reconciler.py` still uses `AsyncMock` for REST methods even though production code calls them synchronously; tests should be updated to use `MagicMock`/plain mocks.

## Remaining Issues (As Of Current Code)

### MEDIUM Priority
- Gap detection callbacks still never fire (WebSocket client does not invoke `on_disconnect`/`on_reconnect`)
- Private reconciliation does not yet use per-account REST credentials (auth likely required)
- Private reconciliation does not paginate beyond the first REST page

### LOW Priority
- Ticker capture not wired in `EventSaver` (no `on_ticker` callback passed)
- Orders are not persisted (only logged)

### HIGH Priority Follow-up
- `shared/db/src/grid_db/repositories.py` uses `sqlalchemy.insert(...)` with `.on_conflict_do_nothing(...)`, which should be switched to dialect-specific inserts (`sqlite_insert`/`postgresql_insert`) to avoid runtime errors.
