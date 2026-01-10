# Feature 0003 - MEDIUM Priority Fixes (Partial)

## Summary

Fixed MEDIUM priority issues identified in code review. Completed deduplication and uniqueness constraints, plus repository infrastructure for position/wallet persistence.

---

## ✅ COMPLETED FIXES

### 1. Database Uniqueness Constraints & Deduplication (MEDIUM - COMPLETE)

**Problem**: No uniqueness constraints on high-volume tables, allowing duplicates from snapshots/reconnects/gap-fills.

**Files Modified**:
- `shared/db/src/grid_db/models.py`
- `shared/db/src/grid_db/repositories.py`
- `apps/event_saver/src/event_saver/reconciler.py`

**Changes**:

#### Models - Added Unique Indexes
```python
# PublicTrade model
__table_args__ = (
    Index("ix_public_trades_symbol_exchange_ts", "symbol", "exchange_ts"),
    Index("ix_public_trades_trade_id", "trade_id", unique=True),  # NEW
)

# PrivateExecution model
__table_args__ = (
    Index("ix_private_executions_account_exchange_ts", "account_id", "exchange_ts"),
    Index("ix_private_executions_run_id", "run_id"),
    Index("ix_private_executions_exec_id", "exec_id", unique=True),  # NEW
)
```

#### Repositories - ON CONFLICT DO NOTHING
Updated `bulk_insert()` methods to use database-native conflict resolution:

```python
# Before: bulk_save_objects() fails on duplicates
self.session.bulk_save_objects(trades)

# After: INSERT with ON CONFLICT DO NOTHING
stmt = insert(PublicTrade).values(trades_data)
stmt = stmt.on_conflict_do_nothing(index_elements=["trade_id"])
result = self.session.execute(stmt)
return result.rowcount  # Returns actual inserted count, excluding duplicates
```

Benefits:
- **Single query** instead of N existence checks
- Database enforces uniqueness atomically
- Returns accurate count of inserted rows (excluding skipped duplicates)
- Works across SQLite and PostgreSQL

#### Reconciler - Simplified Logic
Removed manual N-query deduplication loops:

```python
# Before: Manual deduplication (SLOW - N queries)
existing_ids = set()
for model in models:
    if repo.exists_by_trade_id(model.trade_id):  # N queries!
        existing_ids.add(model.trade_id)
new_models = [m for m in models if m.trade_id not in existing_ids]
count = repo.bulk_insert(new_models)

# After: Database handles duplicates (FAST - 1 query)
count = repo.bulk_insert(models)
skipped = len(models) - count
logger.info(f"Reconciled {count} trades (skipped {skipped} via unique constraint)")
```

**Impact**:
- Eliminates N-query performance bottleneck during gap reconciliation
- Prevents duplicate data from entering database
- Simplifies code and improves reliability

---

### 2. Repository Infrastructure for Position/Wallet Snapshots (MEDIUM - COMPLETE)

**Problem**: Position and wallet snapshots were only logged, not persisted to database.

**Files Modified**:
- `shared/db/src/grid_db/repositories.py`
- `shared/db/src/grid_db/__init__.py`

**Changes**:

#### Added PositionSnapshotRepository
```python
class PositionSnapshotRepository(BaseRepository[PositionSnapshot]):
    def bulk_insert(self, snapshots: List[PositionSnapshot]) -> int:
        # Bulk insert position snapshots
        ...

    def get_latest_by_account_symbol(
        self,
        account_id: str,
        symbol: str
    ) -> Optional[PositionSnapshot]:
        # Get most recent position for account/symbol
        ...
```

#### Added WalletSnapshotRepository
```python
class WalletSnapshotRepository(BaseRepository[WalletSnapshot]):
    def bulk_insert(self, snapshots: List[WalletSnapshot]) -> int:
        # Bulk insert wallet snapshots
        ...

    def get_latest_by_account_coin(
        self,
        account_id: str,
        coin: str
    ) -> Optional[WalletSnapshot]:
        # Get most recent wallet balance for account/coin
        ...
```

Both repositories exported in `grid_db.__init__.py`.

**Status**: Repository infrastructure complete. Writers and wiring needed (see below).

---

##  ⏳ REMAINING WORK

### 3. Position/Wallet Writers & Wiring (MEDIUM - TODO)

**What's Needed**:

1. **Create Position Writer**
   - File: `apps/event_saver/src/event_saver/writers/position_writer.py`
   - Pattern: Similar to `TradeWriter` / `ExecutionWriter`
   - Parse position messages and convert to `PositionSnapshot` models
   - Batch and flush to `PositionSnapshotRepository`

2. **Create Wallet Writer**
   - File: `apps/event_saver/src/event_saver/writers/wallet_writer.py`
   - Pattern: Similar to `TradeWriter` / `ExecutionWriter`
   - Parse wallet messages and convert to `WalletSnapshot` models
   - Batch and flush to `WalletSnapshotRepository`

3. **Wire into EventSaver**
   - File: `apps/event_saver/src/event_saver/main.py`
   - Initialize writers in `EventSaver.start()`
   - Update `_handle_position()` to call writer instead of just logging
   - Update `_handle_wallet()` to call writer instead of just logging

**Example Position Message Structure** (from Bybit WS):
```json
{
  "topic": "position",
  "data": [{
    "symbol": "BTCUSDT",
    "side": "Buy",
    "size": "0.5",
    "entryPrice": "50000.0",
    "liqPrice": "45000.0",
    "unrealisedPnl": "500.0",
    "updatedTime": "1704067200000"
  }]
}
```

**Example Wallet Message Structure**:
```json
{
  "topic": "wallet",
  "data": [{
    "coin": [{
      "coin": "USDT",
      "walletBalance": "10000.0",
      "availableBalance": "9500.0"
    }],
    "updateTime": "1704067200000"
  }]
}
```

---

### 4. WebSocket Gap Detection Callbacks (MEDIUM - DEFERRED)

**Problem**: `on_disconnect` / `on_reconnect` callbacks defined but never called. Gap reconciliation never triggered.

**Current State**:
- `PublicWebSocketClient` and `PrivateWebSocketClient` accept callbacks
- Collectors pass gap detection callbacks to clients
- But pybit `WebSocket` class doesn't expose disconnect/reconnect hooks
- `reconnect_count` never increments

**Possible Solutions**:

1. **Heartbeat Monitor (Recommended)**
   - Implement timeout-based detection: if no messages for N seconds, assume disconnect
   - On reconnect (next message received), trigger callbacks
   - Track `last_message_ts` and check periodically

2. **Pybit Callback Hooks (If Available)**
   - Investigate if pybit exposes `on_close`, `on_error`, `on_open` callbacks
   - Hook into these if available
   - May require pybit version upgrade or custom fork

3. **Connection State Polling**
   - Periodically check `ws._ws.connected` (if exposed)
   - Trigger callbacks on state transitions
   - Less reliable, adds polling overhead

**Recommendation**: Defer until heartbeat monitoring can be properly designed and tested. Gap reconciliation still works when triggered manually.

---

## Test Results

All tests passing after changes:

```bash
# Database tests
uv run pytest shared/db/tests -v
# 81 passed

# Event saver tests
uv run pytest apps/event_saver/tests -v
# 46 passed (4 warnings about test mocks)

# Writers tests
uv run pytest apps/event_saver/tests/test_writers.py -v
# 16 passed

# Reconciler tests
uv run pytest apps/event_saver/tests/test_reconciler.py -v
# 18 passed
```

---

## Summary of Impact

**Performance Improvements**:
- Eliminated N-query deduplication loops → single bulk insert
- Database-enforced uniqueness prevents duplicate data at source

**Code Quality**:
- Simplified reconciler logic (removed manual dedup)
- Repository infrastructure ready for position/wallet persistence

**Remaining Work**:
- Position/wallet writers (~200 lines of code)
- EventSaver wiring (~50 lines of code)
- Gap detection heartbeat monitoring (~150 lines of code + tests)

**Estimated Effort**: ~2-3 hours to complete position/wallet persistence, ~4-6 hours for gap detection monitoring.
