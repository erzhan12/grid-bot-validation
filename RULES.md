# Project Rules and Guidelines

## Project Overview

Grid trading bot system with pure strategy engine (gridcore), exchange adapter (bybit_adapter), database layer (grid_db), data capture (event_saver), live bot (gridbot), backtest engine, comparator, recorder, replay engine, and PnL checker.

Successfully extracted pure strategy logic from `bbu2-master` into `packages/gridcore/` with zero exchange dependencies.

**Documentation**: See `docs/features/0001_IMPLEMENTATION_SUMMARY.md` for complete implementation summary and usage examples.

### Key Implementation Notes

1. **Zero Exchange Dependencies**
   - NO imports from `pybit`, `bybit`, or any exchange-specific libraries
   - Validation command: `grep -r "^import pybit\|^from pybit" packages/gridcore/src/` should return nothing
   - File: All modules in `packages/gridcore/src/gridcore/`

2. **Grid Module (`grid.py`)**
   - Extracted from: `bbu_reference/bbu2-master/greed.py`
   - Key transformation: Replaced `BybitApiUsdt.round_price()` with internal `_round_price(tick_size)`
   - **IMPORTANT**: `tick_size` must be passed as `Decimal` parameter, not looked up from exchange
   - Removed: `read_from_db()`, `write_to_db()`, `strat` dependency
   - Added: `is_price_sorted()`, `is_greed_correct()` validation methods
   - **GridSideType enum (2026-01-23)**: Renamed from `GridSide` to `GridSideType` for clarity (BUY, SELL, WAIT are type constants)
   - **is_grid_correct() Pattern Support (2026-01-10)**: Method accepts both BUY→WAIT→SELL and BUY→SELL patterns. Sometimes there's no WAIT state between BUY and SELL levels, which is now considered valid.
   - **Extended Comparison Tests (2026-01-20)**: Added 8 new direct comparison tests to `test_comparison.py::TestGridComparisonExtended` covering:
     - Sell-heavy rebalancing (opposite direction of existing buy-heavy test)
     - Various grid_count values: small grids (10, 20) and large grids (100, 200)
     - Various grid_step values (0.05%, 0.1%, 0.5%, 1.0%)
     - Extreme prices with realistic tick_size pairs (0.0001 to 999999)
     - None/empty input handling (defensive edge cases)
     - Rebuild grid clearing verification (prevents doubling bug)
     - Grid boundary conditions and consecutive rebuilds
     - Total: 45 comparison tests (29 existing + 16 new with parametrization), all passing in 0.04s
     - **Validation confidence**: Complete behavioral parity confirmed across full parameter space
   - File: `packages/gridcore/src/gridcore/grid.py`

3. **Engine Module (`engine.py`)**
   - Extracted from: `bbu_reference/bbu2-master/strat.py` (Strat50 class)
   - Event-driven pattern: `on_event(event) → list[Intent]`
   - **CRITICAL**: Engine NEVER makes network calls or has side effects
   - Returns intents (PlaceLimitIntent, CancelIntent), execution layer handles actual orders
   - **Helper Methods Pattern (2026-01-23)**: Use `_cancel_limit()` and `_cancel_all_limits()` for DRY CancelIntent creation
     - `_cancel_limit(limit, reason)` - Creates single CancelIntent from limit dict
     - `_cancel_all_limits(limits, reason)` - Creates list of CancelIntents
     - Benefits: Eliminates code duplication, centralizes field extraction pattern, improves readability
     - Usage: `intents.extend(self._cancel_all_limits(limits, 'rebuild'))` instead of loop
     - Applies to all CancelIntent creation: rebuild, side_mismatch, outside_grid
   - **OrderUpdateEvent Handling (2026-01-23)**: Tracks order lifecycle to prevent duplicate placements
     - Original bbu2: Used `handle_order()` to accumulate WebSocket updates in buffer, merged with cached state in `get_limit_orders()`
     - gridcore: Engine tracks `pending_orders` dict (client_order_id → order_id) to know what IT placed
     - Statuses tracked: 'New', 'PartiallyFilled' (pending), 'Filled', 'Cancelled', 'Rejected' (terminal)
     - **IMPORTANT**: Does NOT track 'Active' status (see Pitfall #15 below)
     - Returns empty intent list (order tracking is internal state management only)
     - Execution layer provides full `limit_orders` dict to `on_event()` - engine doesn't maintain order state
   - File: `packages/gridcore/src/gridcore/engine.py`

4. **Position Risk Management Module (`position.py`)**
   - Extracted from: `bbu_reference/bbu2-master/position.py`
   - Manages position sizing multipliers based on liquidation risk, margin levels, and position ratios
   - **TWO-POSITION ARCHITECTURE (2026-01-14)**: Matches Bybit's dual-position model
     - Each trading pair has TWO separate Position objects (one for long, one for short)
     - Positions are linked via `set_opposite()` to enable cross-position multiplier adjustments
     - Example: Long position with moderate liq risk calls `opposite.set_amount_multiplier()` to modify SHORT multipliers
     - **Usage pattern**:
       ```python
       long = Position('long', risk_config)
       short = Position('short', risk_config)
       long.set_opposite(short)
       short.set_opposite(long)
       ```
     - `PositionRiskManager` is backward-compatibility alias for `Position`
   - **CRITICAL BUG FIX (2026-01-01)**: Reference code had incorrect liquidation risk logic for short positions
     - Reference used `liq_ratio < 0.95 * max_liq_ratio` which is backwards
     - Correct logic: `liq_ratio > 0.95 * max_liq_ratio` (higher ratio = closer to liquidation for shorts)
   - **MISSING LOGIC FIX (2026-01-03)**: Added moderate liquidation risk logic for short positions
     - Original bbu2 code at `position.py:81-86` handles moderate liq risk for shorts via opposite position
     - When short has moderate liq risk, modifies opposite (long) position's SELL multiplier to 0.5
   - **SAFER PRIORITY ORDER (2026-01-14)**: Changed to liquidation-first approach matching original bbu2
     - **Long positions**: High liq risk → Moderate liq risk (modifies opposite) → Low margin → Position ratio checks
     - **Short positions**: High liq risk → Position ratio/margin → Moderate liq risk (modifies opposite)
     - **Rationale**: Capital preservation > strategy optimization. Liquidation = 100% loss, missed trade = 0% loss
     - Prevents position sizing strategies from executing during liquidation danger
   - File: `packages/gridcore/src/gridcore/position.py`

5. **Events and Intents**
   - Events (`events.py`): Immutable dataclasses representing market data and order updates
   - Intents (`intents.py`): Immutable dataclasses representing desired actions
   - **PITFALL**: All event dataclass fields that extend Event must have default values (Python dataclass inheritance requirement)
   - **OrderUpdateEvent vs Original bbu2 (2026-01-23)**:
     - **Original bbu2**: `handle_order()` accumulated WebSocket updates in `order_data` buffer, `get_limit_orders()` merged buffer with cached state and cleared buffer
     - **gridcore**: Engine receives `OrderUpdateEvent` per order status change, tracks minimal `pending_orders` state, execution layer maintains full order list
     - **Transformation**: Pull model (poll `get_limit_orders()`) → Push model (receive `OrderUpdateEvent`)
     - **Responsibility split**: Engine tracks what it placed, execution layer provides current order state via `limit_orders` parameter
     - **Status filtering**: Original checked `['Active', 'New', 'PartiallyFilled']` (V3 legacy), gridcore checks `['New', 'PartiallyFilled']` (V5 correct)
   - Files: `packages/gridcore/src/gridcore/events.py`, `packages/gridcore/src/gridcore/intents.py`

6. **Testing**
   - Must maintain ≥80% test coverage
   - Run tests: `uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v`
   - Current coverage: 93% (updated 2026-01-01)
   - Test files: `packages/gridcore/tests/test_*.py`

7. **Package Structure**
   ```
   packages/gridcore/
   ├── pyproject.toml           # Zero external dependencies
   ├── src/gridcore/
   │   ├── __init__.py
   │   ├── events.py            # Event models
   │   ├── intents.py           # Intent models
   │   ├── config.py            # GridConfig
   │   ├── grid.py              # Grid calculations (from greed.py)
   │   ├── engine.py            # GridEngine (from strat.py)
   │   ├── position.py          # Position risk management
   │   ├── pnl.py               # Pure PnL formulas + MMTiers + parse_risk_limit_tiers
   │   └── persistence.py       # Full grid state persistence (GridStateStore)
   └── tests/
       ├── test_grid.py         # Grid calculation tests
       ├── test_engine.py       # Engine event processing tests
       ├── test_position.py     # Position risk tests
       ├── test_persistence.py  # Grid state persistence tests
       └── test_comparison.py   # Comparison with original (optional)
   ```

8. **Reference Code Location**
   - Original code: `bbu_reference/bbu2-master/`
   - Keep reference code for comparison tests
   - Never modify reference code
   - **WARNING**: Reference code may contain bugs (e.g., short position liquidation risk logic)

## Package Management with uv

This project uses [uv](https://github.com/astral-sh/uv) for package management.

### Installation and Setup

```bash
uv sync                                    # Sync workspace
uv pip install -e packages/gridcore        # Install gridcore editable
```

## Running Tests

```bash
# Run ALL tests (recommended — runs each package separately to avoid conftest conflicts)
make test

# Run per-package
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v
uv run pytest packages/bybit_adapter/tests -v
uv run pytest shared/db/tests -v
uv run pytest apps/event_saver/tests -v
uv run pytest apps/gridbot/tests -v
uv run pytest apps/backtest/tests -v
uv run pytest apps/comparator/tests --cov=comparator --cov-report=term-missing -v
uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -v

# Integration tests only
make test-integration
```

**`make test` note**: Runs pytest separately per package to avoid `conftest` ImportPathMismatchError. Coverage is appended; final run prints `term-missing`. `--cov-fail-under` not applied to merged total (~73%).

## Development Workflow

1. Define task clearly
2. Research codebase and RULES.md
3. Create plan and get confirmation
4. Implement with testing
5. Update RULES.md with learnings
6. Verify and commit

---

## gridcore — Pure Strategy Engine

**Path**: `packages/gridcore/` | **Coverage**: 93% | **Dependencies**: ZERO external

### Architecture Rules

- **NO** imports from `pybit`, `bybit`, or any exchange-specific libraries
- **NO** network calls or database calls
- Validation: `grep -r "^import pybit\|^from pybit" packages/gridcore/src/` should return nothing
- `tick_size` must be passed as `Decimal` parameter, never looked up from exchange

### Grid Module (`grid.py`)

- Extracted from `bbu_reference/bbu2-master/greed.py`
- Uses internal `_round_price(tick_size)` instead of `BybitApiUsdt.round_price()`
- `build_greed()` clears `self.greed = []` before building (prevents doubling on rebuilds)
- `is_grid_correct()` accepts both BUY→WAIT→SELL and BUY→SELL patterns
- **GridSideType enum**: `GridSideType.BUY`, `.SELL`, `.WAIT` — always use enum, never raw strings

### Engine Module (`engine.py`)

- Event-driven: `on_event(event) → list[Intent]` — NEVER makes network calls or has side effects
- Returns intents (`PlaceLimitIntent`, `CancelIntent`); execution layer handles actual orders
- **Helper methods**: `_cancel_limit(limit, reason)` and `_cancel_all_limits(limits, reason)` for DRY CancelIntent creation
- **OrderUpdateEvent**: Tracks `pending_orders` dict (client_order_id → order_id). Statuses: 'New'/'PartiallyFilled' (pending), 'Filled'/'Cancelled'/'Rejected' (terminal). Does NOT track 'Active' (V3 legacy, see Bybit V5 note below)
- **GridEngine emits `qty=0`** — qty is always computed by execution layer's `qty_calculator`
- **InstrumentInfo** lives in `gridcore/instrument_info.py` (shared by backtest, replay, gridbot). Provider/fetcher stays in each app layer.
- **Live gridbot qty resolution**: `StrategyRunner._resolve_qty()` composes `_qty_calculator` (from config amount) with `get_amount_multiplier()` (risk). `PlaceLimitIntent` is frozen, so `dataclasses.replace()` creates a new intent with resolved qty.
- **Wallet balance for qty**: Stored on `StrategyRunner._wallet_balance`, updated each `on_position_update()`. Tests must set `runner._wallet_balance` or orders resolve to qty=0 and get skipped.

### Position Risk Module (`position.py`)

- **TWO-POSITION ARCHITECTURE**: Each pair has TWO Position objects (long + short), linked via `set_opposite()`
- **RECOMMENDED**: `Position.create_linked_pair(risk_config)` — or manual link with `set_opposite()` both ways
- `calculate_amount_multiplier()` validates opposite is linked, raises `ValueError` if not
- **Priority order**: Liquidation risk FIRST, then position sizing. Liquidation = 100% loss > missed trade = 0% loss
  - Long: High liq → Moderate liq (modifies opposite) → Low margin → Position ratios
  - Short: High liq → Position ratios/margin → Moderate liq (modifies opposite)
- **SHORT position bug**: Reference code had incorrect liq risk logic (`<` instead of `>`). Higher ratio = closer to liquidation for shorts.
- **Position.size**: Stored on `Position` object, updated in `StrategyRunner.on_position_update()` from both REST and WS paths. Used by `_is_good_to_place()` to validate reduce-only orders.

### Pre-placement Validation (`_is_good_to_place`)

- **Reference**: `bbu_reference/bbu2-master/bybit_api_usdt.py:295-313`
- **Purpose**: Prevents placing reduce-only close orders when total reduce-only qty on the book would exceed position size. Without this, Bybit rejects with error 110017 ("orderQty will be truncated to zero") and the retry queue keeps retrying.
- **Logic**: Open orders always pass. For reduce-only orders: sum all placed reduce-only orders for that direction + new order qty, reject if `position_size <= total_reduce_qty` (strict `>`).
- **Location**: `StrategyRunner._is_good_to_place(intent, limits)` in `apps/gridbot/src/gridbot/runner.py`, called from `_execute_place_intent()` after qty resolution. Accepts an explicit `limits` dict (same format as `get_limit_orders()`) so the data source is injectable — live can pass exchange data, backtest can pass simulated data.
- **Position size source**: `Position.size` attribute set in `on_position_update()`. Defaults to `Decimal('0')` until first `on_position_update()` call, which safely rejects reduce-only orders during startup.
- **Decimal conversion safety**: Always use `Decimal(str(value))` — never bare `Decimal(value)` — when converting order dict fields (`price`, `qty`) or any variable that might be a float. `Decimal(0.5)` produces `0.500000000000000027...` which silently breaks equality checks. The `Decimal(str(...))` pattern is safe for strings, floats, and Decimals alike.
- **Zero-size rejection is intentional, not a bug**: When `position_size == Decimal('0')` the reduce-only order is silently rejected (debug log only). This is bbu2-faithful — bbu2 expresses the same behavior implicitly via `position_size > limits_qty` arithmetic. A race can occur when the engine emits a close intent in the sub-tick window after a fill but before the position update lands; it self-heals on the next tick because the engine re-emits the same reduce-only intent every tick from scratch. **Do NOT "allow through on staleness"** — that would place orders against known-stale state and make things worse. If the position feed itself dies, fix it in the position-update path (heartbeat, REST reconcile), not here. See `runner.py:748-753`.

### Enums

| Enum | Module | Values | Notes |
|------|--------|--------|-------|
| `GridSideType` | `grid.py` | BUY, SELL, WAIT | Renamed from `GridSide` |
| `DirectionType` | `position.py` | LONG, SHORT | StrEnum, backward-compatible |
| `SideType` | `position.py` | BUY, SELL | StrEnum, backward-compatible |

### Events and Intents

- All event dataclass fields extending `Event` must have default values (Python dataclass inheritance)
- **PlaceLimitIntent identity**: SHA256 hash of `_IDENTITY_PARAMS = ['symbol', 'side', 'price', 'direction']`
  - `grid_level` removed from hash — orders survive grid rebalancing when price stays same
  - `qty`, `reduce_only`, `grid_level` excluded (not identity-affecting)
  - `build_grid()` validates no duplicate prices
  - When adding params: if it affects uniqueness → add to `_IDENTITY_PARAMS`; if not → don't
  - See `docs/features/ORDER_IDENTITY_DESIGN.md`

### PnL Calculations (`pnl.py`)

### Grid State Persistence (`persistence.py`)

`GridStateStore` (renamed from the legacy `GridAnchorStore` in feature 0021) persists the **full** ordered grid per strategy across restarts, replacing the old anchor-only scheme. This restores per-fill WAIT zones, side reassignments, and `__center_grid` drift that were previously lost.

**Usage**

- File location: `db/grid_anchor.json` (filename preserved for deploy-config compatibility — orchestrator constructor still accepts `anchor_store_path`).
- Wired by `Orchestrator → StrategyRunner` (`apps/gridbot/src/gridbot/orchestrator.py`, `apps/gridbot/src/gridbot/runner.py`). Runner registers `_on_grid_change` as a callback into `Grid` via `GridEngine(on_grid_change=...)`.
- `Grid.build_grid()` and `Grid.update_grid()` invoke the callback at the end of every mutation; `Grid.restore_grid()` does NOT (loading is not a mutation worth re-persisting).

**Schema**

```json
{
  "ltcusdt_test": {
    "grid": [
      {"side": "Buy",  "price": 53.4},
      {"side": "Wait", "price": 55.4},
      {"side": "Sell", "price": 57.4}
    ],
    "grid_step": 0.3,
    "grid_count": 20
  }
}
```

`side` values are `GridSideType` enum values (`"Buy"`, `"Sell"`, `"Wait"`). `grid_step` and `grid_count` are kept alongside the grid only for config-mismatch invalidation (see below).

**Thread-safety + atomic write**

- `save()` is a **sync API but non-blocking**. It computes a cheap fingerprint (tuple of `(side, price)` pairs + grid_step + grid_count), short-circuits if equal to the last-enqueued payload (dedupe BEFORE deepcopy), then dispatches via a per-strat pending slot.
- **Single-writer-per-strat**: each `strat_id` has at most one daemon `threading.Thread` writing at a time. A new save while a writer is in flight overwrites the slot; the in-flight writer drains it on its next loop iteration. Coalesces rapid bursts into one final disk write per strat with **latest-wins ordering** (a naive `threading.Lock`-per-write would not be FIFO and could write older payloads after newer ones).
- **Atomic on disk**: every write goes through tmp file + `f.flush()` + `os.fsync()` + `os.replace()`. A `kill -9` mid-write cannot leave a corrupted half-written file. Failed writes (disk full, permission denied) clean up the `.tmp` file before propagating the exception, so stale tmp files do not accumulate.
- **Two locks**: `_io_lock` (`threading.Lock`) serializes disk I/O across strats — the file is shared. `_cv` (`threading.Condition`) gates dedupe state, the active-writer set, and `flush()` wait/notify.
- **Failure semantics**: a write failure inside the writer is logged (`logger.error("Save failed for %s: %s", ...)`) and the dedupe fingerprint is rolled back (only if no newer payload arrived since), so the next identical save can retry. The writer thread continues to drain any newer pending payload — failures do not crash strategy logic.

**Legacy format migration**

Pre-0021 files contain `{anchor_price, grid_step, grid_count}` per strat (no `grid` key). On `load()`, missing-`grid` is detected and treated as no-saved-state; one info log fires (`"Legacy anchor format ignored, building fresh grid at market price"`) and the engine builds a fresh grid from market price on the first ticker. **No data-preserving conversion** is needed (a converter would produce the same result as building fresh from the anchor).

**Config-mismatch invalidation**

If the saved `grid_step` or `grid_count` differs from the current strategy config, the runner discards the saved grid and logs `"Config changed, will build fresh grid"`. Done in `runner._load_grid_state()` before passing `restored_grid` to `GridEngine`.

**Self-healing on corruption**

`_read_all_data()` returns `{}` on any error: missing file, JSON parse failure, or **non-dict root** (e.g. hand-edited `[]` / `"x"` / `1`). The next `save()` silently overwrites a corrupt file. Per-entry corruption (entry that isn't a dict, or grid that fails `is_grid_correct()`) also returns None / fresh build — the bot never crashes on a bad persistence file.

**Pitfalls**

- **Why threads, not asyncio?** Gridbot's `Orchestrator.run()` is a synchronous main loop using `time.sleep` — there is no event loop in the live runtime. `asyncio.create_task()` would always raise `RuntimeError` and fall through to synchronous fsync, blocking the main loop. Daemon threads work in both sync and async caller contexts. **Do not "modernize" to asyncio** without first making the orchestrator async end-to-end.
- **`GridStateStore.flush()`** blocks until all pending writes complete. Use it in tests (deterministic instead of `time.sleep`) and keep `Orchestrator.stop()` flushing after WS disconnects; without the graceful-shutdown flush, daemon writer threads can be killed before persisting the latest post-fill grid.
- **Drift guard on restore**: `engine._handle_ticker_event` rebuilds if `last_close` is outside `[grid.min_grid, grid.max_grid]`. Uses `Grid.bounds` (single-pass min+max) for the per-tick check — do not call `min_grid` and `max_grid` separately in hot paths.
- **`anchor_price` parameter on `GridEngine` is retained for backtest compatibility**, separate from `restored_grid`. Backtest pins grid origin via `anchor_price`; live runner uses `restored_grid` for full-state restore. They serve different use cases.
- **Known limitation**: an in-flight writer thread that has already popped a payload from `_pending_payload` and is waiting on `_io_lock` cannot be cancelled by a concurrent `delete()`. The writer will eventually re-persist the entry after the delete. Acceptable for current usage (delete is for "strat removed from config" — no concurrent saves expected); not currently fixed.

## Logging Configuration

Gridcore uses Python's standard library `logging` module. Loggers are named after their modules (`gridcore.grid`, `gridcore.engine`, `gridcore.position`).

### Log Levels
- `INFO` - Important events: grid rebuild, position adjustments
- `DEBUG` - Detailed state info: position calculations

### Configuration Example
```python
import logging

# Configure gridcore logging
logging.getLogger('gridcore').setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s'))
logging.getLogger('gridcore').addHandler(handler)
```

### Logged Events
- **grid.py**: Grid rebuild when price moves out of bounds
- **engine.py**: Grid build from anchor/market price, rebuild due to too many orders
- **position.py**: Position ratio adjustments, risk management triggers

## Phase C: Multi-Tenant Database Layer (grid_db)

### Completed: 2026-01-07

Successfully implemented a multi-tenant database layer supporting SQLite (development) and PostgreSQL (production).

### Key Implementation Notes

1. **Package Structure**
   ```
   shared/db/
   ├── pyproject.toml           # Dependencies: sqlalchemy, pydantic-settings
   ├── src/grid_db/
   │   ├── __init__.py          # Package exports
   │   ├── settings.py          # DatabaseSettings (Pydantic)
   │   ├── models.py            # SQLAlchemy ORM models (7 tables)
   │   ├── database.py          # DatabaseFactory
   │   ├── repositories.py      # CRUD with multi-tenant filtering
   │   └── init_db.py           # CLI initialization script
   └── tests/
       ├── conftest.py          # Test fixtures
       ├── test_models.py
       ├── test_database.py
       └── test_repositories.py
   ```

2. **Database Tables**
   - `users` - User accounts for multi-tenant access control
   - `bybit_accounts` - Exchange accounts per user
   - `api_credentials` - API keys (plaintext for now)
   - `strategies` - Grid strategy configurations (JSON config)
   - `runs` - Live/backtest/shadow run tracking
   - `public_trades` - Market trade data for fill simulation
   - `private_executions` - Ground truth execution data

3. **SQLite/PostgreSQL Compatibility**
   - Use `String(36)` for UUIDs (not native UUID type)
   - Use `BigInteger().with_variant(Integer, "sqlite")` for high-volume primary keys (trades/executions)
   - SQLite requires `PRAGMA foreign_keys=ON` on every connection
   - JSON type works as text in SQLite, native JSONB in PostgreSQL
   - SQLite connection pooling must use `StaticPool` ONLY for `:memory:` databases
   - **PostgreSQL URL encoding**: Connection components (user, password, host, db_name) are URL-encoded using `urllib.parse.quote_plus()` to handle special characters (e.g., `@`, `:`, `/`, `#`, `%` in passwords). The port is NOT encoded per RFC 3986 (ports are numeric-only URI components). Without encoding, passwords with special characters would break the connection string parsing.

4. **Testing**
   - Run tests: `uv run pytest shared/db/tests/ --cov=grid_db -v`
   - Test fixtures use in-memory SQLite (`:memory:`)

5. **Session Management**
   - Use `DatabaseFactory.get_session()` context manager for auto commit/rollback
   - For tests expecting IntegrityError, session fixture uses manual rollback

6. **Multi-Tenant Access Control**
   - **CRITICAL**: All queries MUST filter by `user_id` to enforce data isolation
   - `RunRepository` enforces `user_id` filtering on all methods
   - `ApiCredentialRepository` and `StrategyRepository` enforce `user_id` filtering (join via `BybitAccount`)
   - `BaseRepository` does NOT expose `get_by_id`/`get_all` (removed for safety)
   - `UserRepository` explicitly implements admin-style access methods

7. **Cascade Deletes**
   - All foreign keys have `ondelete="CASCADE"` to ensure referential integrity
   - ORM relationships use `cascade="all, delete-orphan"` for automatic cleanup
   - **Run model**: Foreign keys (`user_id`, `account_id`, `strategy_id`) cascade on parent deletion
   - **PrivateExecution model**: `run_id` foreign key cascades when Run is deleted
   - Deleting a User/BybitAccount/Strategy automatically deletes associated Runs
   - Deleting a Run automatically deletes associated PrivateExecution records
   - Tests verify cascade behavior: `test_*_cascade_delete` in `shared/db/tests/test_models.py`

### Usage Example
```python
from grid_db import DatabaseFactory, DatabaseSettings
from grid_db import User, BybitAccount, Strategy, Run
from grid_db import UserRepository, RunRepository

# Initialize database
# Env vars loaded with GRIDBOT_ prefix (e.g. GRIDBOT_DB_TYPE)
settings = DatabaseSettings()
db = DatabaseFactory(settings)
db.create_tables()

# Use session context manager
with db.get_session() as session:
    # Create user
    user = User(username="trader1", email="trader@example.com")
    session.add(user)
    session.flush()

    # Create account
    account = BybitAccount(
        user_id=user.user_id,
        account_name="main",
        environment="testnet"
    )
    session.add(account)

# Use repositories for multi-tenant queries
with db.get_session() as session:
    repo = RunRepository(session)
    runs = repo.get_by_user_id(user_id)  # Only returns user's runs
```

### Environment Variables
- `GRIDBOT_DB_TYPE` - "sqlite" or "postgresql"
- `GRIDBOT_DB_NAME` - Database name or file path
- `GRIDBOT_DB_HOST`, `GRIDBOT_DB_PORT`, `GRIDBOT_DB_USER`, `GRIDBOT_DB_PASSWORD` - PostgreSQL config

## Phase D: Data Capture (bybit_adapter + event_saver)

### Completed: 2026-01-07

Successfully implemented Bybit data capture infrastructure with WebSocket normalization and bulk persistence.

### Key Components

1. **bybit_adapter Package** (`packages/bybit_adapter/`)
   - `normalizer.py` - Converts Bybit WebSocket messages to gridcore events
   - `ws_client.py` - PublicWebSocketClient and PrivateWebSocketClient
   - `rest_client.py` - REST API for gap reconciliation
   - `rate_limiter.py` - Sliding window rate limiting with exponential backoff
   - Dependencies: `pybit>=5.8`, `gridcore` (workspace)

2. **event_saver Application** (`apps/event_saver/`)
   - `config.py` - Pydantic settings with `EVENTSAVER_*` env vars
   - `collectors/` - PublicCollector, PrivateCollector with symbol filtering
   - `writers/` - TradeWriter, ExecutionWriter with batch bulk insert
   - `reconciler.py` - GapReconciler for REST gap filling
   - `main.py` - EventSaver orchestrator with signal handling

3. **Database Extensions** (`shared/db/`)
   - Added `PositionSnapshot`, `WalletSnapshot` models
   - Added `PublicTradeRepository`, `PrivateExecutionRepository`

### Event Normalization Mapping

**publicTrade.{symbol} → PublicTradeEvent**
| Bybit | gridcore |
|-------|----------|
| `data[].i` | trade_id |
| `data[].T` | exchange_ts (ms→datetime) |
| `data[].S` | side ("Buy"/"Sell") |
| `data[].p` | price (Decimal) |
| `data[].v` | size (Decimal) |

**execution → ExecutionEvent**
| Bybit | gridcore |
|-------|----------|
| `data[].execId` | exec_id |
| `data[].orderId` | order_id |
| `data[].orderLinkId` | order_link_id |
| `data[].execTime` | exchange_ts |
| `data[].execPrice` | price |
| `data[].execQty` | qty |
| `data[].execFee` | fee |
| `data[].closedPnl` | closed_pnl |

**Filters**: `category=="linear"`, `execType=="Trade"`, `orderType=="Limit"`

### Environment Variables
- `EVENTSAVER_SYMBOLS` - Comma-separated symbol list (e.g., "BTCUSDT,ETHUSDT")
- `EVENTSAVER_TESTNET` - Use testnet endpoints (default: true)
- `EVENTSAVER_BATCH_SIZE` - Trades to batch before bulk insert (default: 100)
- `EVENTSAVER_FLUSH_INTERVAL` - Seconds between forced flushes (default: 5.0)
- `EVENTSAVER_GAP_THRESHOLD_SECONDS` - Trigger reconciliation if gap > this (default: 5.0)
- `EVENTSAVER_DATABASE_URL` - Database connection URL (default: sqlite:///gridbot.db)

### Usage Example
```python
from event_saver import EventSaver, EventSaverConfig, AccountContext
from grid_db import DatabaseFactory
from uuid import uuid4

config = EventSaverConfig()
db = DatabaseFactory(config.database_url)

saver = EventSaver(config=config, db=db)

# Add account for private data collection
saver.add_account(AccountContext(
    account_id=uuid4(),
    user_id=uuid4(),
    run_id=uuid4(),
    api_key="your_api_key",
    api_secret="your_api_secret",
    environment="testnet",
    symbols=["BTCUSDT"],
))

await saver.start()
await saver.run_until_shutdown()
```

### Key Implementation Notes

1. **Workspace Dependencies**
   - Use `[tool.uv.sources]` to declare workspace dependencies
   - Example: `gridcore = { workspace = true }` in bybit_adapter's pyproject.toml

2. **Model Field Mapping**
   - `PrivateExecution` model uses `exec_price`, `exec_qty`, `exec_fee` (not `price`, `qty`, `fee`)
   - `run_id` is REQUIRED for PrivateExecution (foreign key to runs table)
   - Events without `run_id` are filtered out during model conversion

3. **Config Symbols Parsing**
   - `symbols` field is stored as string, use `config.get_symbols()` to get list
   - Pydantic-settings parses list[str] as JSON by default, use string + method pattern

4. **Test Coverage**
   - bybit_adapter: 37 tests (normalizer, rate_limiter)
   - event_saver: 46 tests (config, writers, reconciler)
   - Run separately to avoid conftest conflicts: `uv run pytest packages/bybit_adapter/tests -v`

### Critical Pitfalls Fixed (2026-01-08)

**IMPORTANT**: When initializing components in event_saver, avoid these errors:

1. **WebSocket Handler Thread Safety (2026-01-08)**
   - **Issue**: Handler methods (`_handle_ticker`, `_handle_trades`, etc.) called `asyncio.create_task()` but are invoked from pybit's WebSocket background thread (not asyncio event loop thread)
   - **Error**: `RuntimeError: no running event loop` when WebSocket messages arrive
   - **Fix**: Store event loop reference in `EventSaver.start()`, use `asyncio.run_coroutine_threadsafe()` instead of `asyncio.create_task()` in all handlers
   - **Pattern**: When callbacks come from non-asyncio threads, use `run_coroutine_threadsafe(coro, loop)` to schedule work on the event loop
   - **Files**: `apps/event_saver/src/event_saver/main.py:83,154-155,296-427`

2. **DatabaseFactory Requires Settings Object**
   - `DatabaseFactory` expects `DatabaseSettings` object, NOT a raw string
   - **WRONG**: `db = DatabaseFactory(config.database_url)` ❌
   - **CORRECT**: `db = DatabaseFactory(DatabaseSettings(database_url=config.database_url))` ✅
   - File: `apps/event_saver/src/event_saver/main.py:363`

2. **BybitRestClient Requires Credentials**
   - `BybitRestClient` requires `api_key` and `api_secret` parameters (even if empty for public endpoints)
   - **WRONG**: `BybitRestClient(testnet=True)` ❌
   - **CORRECT**: `BybitRestClient(api_key="", api_secret="", testnet=True)` ✅
   - File: `apps/event_saver/src/event_saver/main.py:118-124`

3. **REST Client Methods Are Synchronous, Not Async**
   - `BybitRestClient.get_recent_trades()` and `get_executions()` are synchronous `def` methods
   - **WRONG**: `await self._rest_client.get_recent_trades(...)` ❌
   - **CORRECT**: `self._rest_client.get_recent_trades(...)` ✅
   - Files: `apps/event_saver/src/event_saver/reconciler.py:120, 216`

4. **get_executions() Returns Tuple (list, cursor)**
   - **WRONG**: `executions_data = self._rest_client.get_executions(...)` ❌
   - **CORRECT**: `executions_data, next_cursor = self._rest_client.get_executions(...)` ✅
   - File: `apps/event_saver/src/event_saver/reconciler.py:216`

5. **PublicTradeRepository.exists_by_trade_id() Takes Only trade_id**
   - Method signature: `exists_by_trade_id(trade_id: str) -> bool`
   - **WRONG**: `repo.exists_by_trade_id(symbol, model.trade_id)` ❌
   - **CORRECT**: `repo.exists_by_trade_id(model.trade_id)` ✅
   - File: `apps/event_saver/src/event_saver/reconciler.py:150`

See `docs/features/0003_FIXES.md` for detailed documentation of these fixes.

### MEDIUM Priority Improvements (2026-01-08)

**Database Uniqueness & Deduplication**:

1. **Unique Constraints Added**
   - `public_trades.trade_id` has unique index (prevents duplicate trades)
   - `private_executions.exec_id` has unique index (prevents duplicate executions)
   - Files: `shared/db/src/grid_db/models.py:235, 273`

2. **Bulk Insert with ON CONFLICT DO NOTHING**
   - `PublicTradeRepository.bulk_insert()` uses `INSERT ... ON CONFLICT DO NOTHING`
   - `PrivateExecutionRepository.bulk_insert()` uses `INSERT ... ON CONFLICT DO NOTHING`
   - Returns actual inserted count (excluding skipped duplicates)
   - Works across SQLite and PostgreSQL
   - File: `shared/db/src/grid_db/repositories.py:439-481, 571-619`

3. **Simplified Reconciliation Logic**
   - Removed N-query manual deduplication loops
   - Database enforces uniqueness at insert time (single query)
   - **BEFORE**: Loop through models calling `exists_by_trade_id()` for each (N queries) ❌
   - **AFTER**: Bulk insert all, database skips duplicates (1 query) ✅
   - File: `apps/event_saver/src/event_saver/reconciler.py:143-157, 229-243`

4. **Position/Wallet Persistence - COMPLETE**
   - Added `PositionSnapshotRepository` with `bulk_insert()` and `get_latest_by_account_symbol()`
   - Added `WalletSnapshotRepository` with `bulk_insert()` and `get_latest_by_account_coin()`
   - Exported in `grid_db.__init__.py`
   - File: `shared/db/src/grid_db/repositories.py:646-772`
   - Created `PositionWriter` and `WalletWriter` with buffering and auto-flush
   - Files: `apps/event_saver/src/event_saver/writers/{position_writer,wallet_writer}.py`
   - Wired into `EventSaver` main orchestrator
   - File: `apps/event_saver/src/event_saver/main.py:66-67, 150-162, 207-211, 265-305, 376-380`
   - Position and wallet snapshots are now persisted to database (not just logged)

See `docs/features/0003_MEDIUM_PRIORITY_FIXES.md` for detailed documentation of MEDIUM priority fixes.

### LOW Priority Cleanup (2026-01-08)

**Code Quality Improvements**:

1. **PublicCollector Cleanup**
   - Removed unused `import asyncio`
   - Removed unused field `_task: Optional[asyncio.Task]`
   - Kept `_last_trade_ts` (actually used for tracking)
   - File: `apps/event_saver/src/event_saver/collectors/public_collector.py:3, 69`

2. **PrivateCollector Cleanup**
   - Removed unused `import asyncio`
   - Removed unused `UTC` from `datetime` import
   - File: `apps/event_saver/src/event_saver/collectors/private_collector.py:3, 6`

3. **__pycache__ Artifacts**
   - Verified: No `__pycache__` files tracked in git
   - `.gitignore` correctly excludes `__pycache__/`
   - Runtime cache directories exist but are properly ignored

See `docs/features/0003_LOW_PRIORITY_CLEANUP.md` for detailed documentation.

### Critical Fixes (2026-01-09)

**Phase D Code Review Resolutions**:

1. **Heartbeat Watchdog Thread Safety**
   - **Issue**: `disconnect_ts` variable referenced outside lock without initialization, causing `UnboundLocalError`
   - **Fix**: Initialize `disconnect_ts = None` before lock block, assign within lock, use flag `should_fire_disconnect`
   - Files: `packages/bybit_adapter/src/bybit_adapter/ws_client.py:219-249, 463-493`

2. **Heartbeat Deadlock Prevention**
   - **Issue**: `disconnect()` called `_stop_heartbeat_watchdog()` while holding `_lock`, causing deadlock during `join()`
   - **Fix**: Call `_stop_heartbeat_watchdog()` BEFORE acquiring `_lock`
   - Files: `packages/bybit_adapter/src/bybit_adapter/ws_client.py:143-148, 369-374`

3. **Private Reconciliation Authentication**
   - **Issue**: `reconcile_executions()` accepted `api_key`/`api_secret` but didn't use them (used shared client with empty credentials)
   - **Fix**: Create per-account authenticated `BybitRestClient` inside `reconcile_executions()` using provided credentials
   - File: `apps/event_saver/src/event_saver/reconciler.py:203-210`

4. **Private Reconciliation Pagination**
   - **Issue**: Only fetched first 100 executions, ignored `next_cursor`
   - **Fix**: Use `get_executions_all()` with pagination loop (max 10 pages)
   - File: `apps/event_saver/src/event_saver/reconciler.py:217-227`

5. **REST Calls Blocking Event Loop**
   - **Issue**: Synchronous `HTTP` calls in `async` methods blocked event loop
   - **Fix**: Wrap sync REST calls in `asyncio.to_thread()` to run in thread executor
   - Files: `apps/event_saver/src/event_saver/reconciler.py:3, 133-138, 221-227`

6. **Reconciliation Algorithm Enhancement**
   - **Issue**: Didn't query DB for `last_persisted_ts`, only used gap timestamps
   - **Fix**: Query `PublicTradeRepository.get_last_trade_ts()` and `PrivateExecutionRepository.get_last_execution_ts()` before reconciliation
   - Files: `apps/event_saver/src/event_saver/reconciler.py:116-131, 218-229`

7. **Order Persistence Implementation**
   - **Issue**: Order updates normalized and logged but not persisted to DB
   - **Fix**: Created `Order` model, `OrderRepository`, and `OrderWriter` with bulk insert and conflict handling
   - Files:
     - Model: `shared/db/src/grid_db/models.py:277-316`
     - Repository: `shared/db/src/grid_db/repositories.py:650-768`
     - Writer: `apps/event_saver/src/event_saver/writers/order_writer.py`
     - Wired: `apps/event_saver/src/event_saver/main.py:66, 89, 151-156, 215-216, 266-279, 392-393`
   - **Order Table Features**:
     - Stores latest state for each `order_id` + `exchange_ts` combination
     - Uses `ON CONFLICT DO UPDATE` to keep latest `status`, `leaves_qty`, `raw_json`
     - Cascades delete when parent `Run` is deleted
     - Indexed by `account_id`, `exchange_ts`, and `order_id`+`exchange_ts`

8. **Ticker Capture Wiring**
   - **Issue**: `PublicCollector` implemented ticker support but `EventSaver` didn't supply `on_ticker` callback
   - **Fix**: Added `_handle_ticker()` method and wired to `PublicCollector`
   - File: `apps/event_saver/src/event_saver/main.py:177, 248-256`

### Code Review Fixes (2026-01-09)

**HIGH Priority - Order Persistence Unique Constraint**:
- **Issue**: `OrderRepository.bulk_insert()` used `ON CONFLICT DO UPDATE` on `["order_id", "exchange_ts"]` but table only had non-unique index, causing runtime errors
- **Fix**: Added composite unique constraint `(account_id, order_id, exchange_ts)` and updated conflict target
- **Rationale**: Prevents same order_id across different accounts from conflicting; allows upsert to keep latest order status
- Files: `shared/db/src/grid_db/models.py:315-320`, `shared/db/src/grid_db/repositories.py:741,754`

**HIGH Priority - Reconciler Test REST Client Mocking**:
- **Issue**: Test patched `reconciler._rest_client.get_executions_all` but method creates new local `BybitRestClient`, so mock was ineffective
- **Fix**: Patch `BybitRestClient` constructor instead: `patch('event_saver.reconciler.BybitRestClient', return_value=mock_client)`
- **Pattern**: When code creates instances locally, mock the constructor not instance methods
- File: `apps/event_saver/tests/test_reconciler.py:247-254`

**MEDIUM Priority - Private Reconciliation Environment Handling**:
- **Issue**: `reconcile_executions()` used global `self._rest_client.testnet` instead of account's actual environment
- **Fix**: Added `testnet` boolean parameter to `reconcile_executions()`, caller passes `context.environment == "testnet"`
- **Impact**: Prevents mixed-environment reconciliation when accounts differ from global config
- Files: `apps/event_saver/src/event_saver/reconciler.py:188,239`, `main.py:371`

**MEDIUM Priority - run_id Requirement Documentation**:
- **Issue**: Writers silently dropped events without `run_id`, behavior not documented
- **Fix**: Added docstring to `AccountContext` explaining persistence requirements, added warning in `EventSaver.add_account()`
- **Behavior**: Executions/orders REQUIRE `run_id` for persistence; position/wallet snapshots do NOT
- Files: `apps/event_saver/src/event_saver/collectors/private_collector.py:24-28`, `main.py:86-90`

### Test Commands
```bash
# Run bybit_adapter tests
uv run pytest packages/bybit_adapter/tests -v

# Run event_saver tests
uv run pytest apps/event_saver/tests -v

# Run all tests (separate commands due to conftest conflicts)
uv run pytest packages/gridcore/tests -v
uv run pytest packages/bybit_adapter/tests -v
uv run pytest shared/db/tests -v
uv run pytest apps/event_saver/tests -v
```

## Phase E: Live Bot Rewrite (gridbot)

### Completed: 2026-01-24

Successfully implemented a multi-tenant grid trading bot using gridcore strategy engine.

### Key Components

1. **Package Structure**
   ```
   apps/gridbot/
   ├── pyproject.toml           # Dependencies: gridcore, bybit-adapter, grid-db, event-saver
   ├── conf/
   │   └── gridbot.yaml.example # Example configuration
   ├── src/gridbot/
   │   ├── __init__.py
   │   ├── config.py            # Pydantic config models
   │   ├── executor.py          # Intent → Bybit API calls
   │   ├── retry_queue.py       # Failed intent retry (3 attempts, 30s max)
   │   ├── runner.py            # StrategyRunner wrapping GridEngine
   │   ├── reconciler.py        # Exchange state sync
   │   ├── orchestrator.py      # Multi-strategy coordinator
   │   └── main.py              # Entry point with signal handling
   └── tests/
       └── test_*.py            # 101 tests
   ```

2. **Architecture Decisions**
   - Single process handles all accounts
   - YAML configuration file
   - Hybrid event loop: async WebSocket + periodic polling (~63s positions)
   - In-memory order tracking, reconcile from exchange on startup
   - Failed intents: queue for retry (3 attempts, 30s max, exponential backoff)
   - Shadow mode: log intents without executing
   - Startup sync: adopt existing orders from exchange

3. **Data Flow**
   ```
   WebSocket Events → Orchestrator → StrategyRunner → GridEngine.on_event() → Intents
                                                                                  ↓
                                                              Executor (shadow: log | live: execute)
                                                                                  ↓
                                                                         Bybit REST API
   ```

4. **Key Files**
   - `config.py`: `AccountConfig`, `StrategyConfig`, `GridbotConfig` Pydantic models
   - `executor.py`: `IntentExecutor.execute_place()`, `execute_cancel()`, `execute_batch()`
   - `retry_queue.py`: `RetryQueue` with exponential backoff (1s, 2s, 4s)
   - `runner.py`: `StrategyRunner` wraps `GridEngine`, tracks orders, handles position updates
   - `reconciler.py`: `Reconciler.reconcile_startup()`, `reconcile_reconnect()`
   - `orchestrator.py`: `Orchestrator` coordinates multiple strategies, routes events

5. **Configuration Example**
   ```yaml
   accounts:
     - name: "main_account"
       api_key: "YOUR_API_KEY"
       api_secret: "YOUR_API_SECRET"
       testnet: true

   strategies:
     - strat_id: "btcusdt_main"
       account: "main_account"
       symbol: "BTCUSDT"
       tick_size: "0.1"
       grid_count: 50
       grid_step: 0.2
       amount: "x0.001"  # 0.1% of wallet per order
       max_margin: 8
       shadow_mode: false
   ```

6. **Running the Bot**
   ```bash
   # Run with default config (conf/gridbot.yaml)
   uv run python -m gridbot.main

   # Run with custom config
   uv run python -m gridbot.main --config path/to/config.yaml

   # Run with debug logging
   uv run python -m gridbot.main --debug
   ```

7. **Testing**
   ```bash
   uv run pytest apps/gridbot/tests -v
   ```

### Key Implementation Notes

1. **Order Tracking Pattern**
   - `TrackedOrder` dataclass tracks order lifecycle (pending → placed → filled/cancelled)
   - Single primary dict `_tracked_orders` (keyed by client_order_id), no secondary indexes
   - Lookups by exchange `order_id` use linear scan — fine for ~20-40 grid orders (matches bbu2 pattern)
   - `_is_good_to_place(intent, limits)` accepts explicit order list — injectable data source (bbu2 style)
   - Deterministic `client_order_id` (16-char hex from SHA256) enables deduplication
   - `runner.inject_open_orders()` for startup reconciliation

2. **Position Risk Management**
   - `StrategyRunner` owns linked `Position` pair (long/short)
   - `on_position_update()` calculates `position_ratio` and `amount_multiplier`
   - Initial position fetch via REST at startup (`_fetch_and_update_positions()` in `start()`) so runners have multipliers before first ticker
   - Periodic position check (default 63s) via `orchestrator._position_check_loop()` which calls the same `_fetch_and_update_positions()`

3. **Event Routing**
   - `_symbol_to_runners`: routes ticker events by symbol
   - `_account_to_runners`: routes position/order/execution events by account
   - WebSocket callbacks use `asyncio.run_coroutine_threadsafe()` for thread safety

4. **Shadow Mode**
   - `shadow_mode=True` in strategy config → intents logged but not executed
   - `IntentExecutor` returns shadow order IDs: `shadow_{client_order_id}`
   - Useful for validating strategy behavior before live trading

5. **Reconciliation**
   - **Startup**: fetch all open limit orders for the symbol, inject into runner via `inject_open_orders()`
   - **Inject is NOT durable adoption (bbu2-faithful)**: Injected orders live for exactly one ticker event. On the first `on_ticker` after startup, `GridEngine._place_grid_orders` (`packages/gridcore/src/gridcore/engine.py:319-325`) cancels any injected order whose price is not in the current `grid_price_set` (`'outside_grid'` reason), and `engine.py:305-312` cancels any at a grid price with the wrong side (`'side_mismatch'` reason). Over-limit cases (`engine.py:237-243`) trigger a full rebuild that cancels everything. Direct port of bbu2 `strat.py:154-160`, `:145-149`, `:103-104`. This means: (a) a "silent adoption of manual orders" security review is a false alarm — the bot does not keep manual orders around, it destroys them on the next tick; (b) the **real** operational concern is the opposite — the bot will **cancel** any limit order on the symbol that doesn't match the grid; (c) do NOT add a refuse-to-start check in `reconcile_startup` — it would re-break normal crash-restart (the bot's own prior orders look identical to manual ones) and was already removed in commit `138737a` for that reason.
   - **(account, symbol) uniqueness is enforced unconditionally at config load**: Since `orderLinkId` is not sent to Bybit, there is no way at runtime to tell one strategy's orders from another's. Two strategies on the same `(account, symbol)` pair would cancel each other's orders every tick via the cancel-on-mismatch pass described above. `GridbotConfig.validate_no_shared_symbol` (`apps/gridbot/src/gridbot/config.py`) rejects any such configuration at load time with **no escape hatch** — there is no flag to disable it. bbu2 enforces the same invariant structurally: its `amounts[].strat` field is a scalar pointing at a single `pair_timeframes[]` entry, and each `pair_timeframe` has a single `symbol`, so the bad configuration is physically unrepresentable in bbu2's config schema. grid-bot's schema is more flexible (independent `accounts` and `strategies` lists, FK goes `strategy.account → account.name`), so the constraint must be reconstructed as a pydantic validator — but it is enforced just as strictly. If you need a second strategy on the same symbol, use a different account.
   - **Operational consequence (manual orders get cancelled)**: Any limit order on the symbol that is not at a current grid price, or is at a grid price with the wrong side, will be cancelled by the engine on the next ticker event after it is seen (see the "Inject is NOT durable adoption" bullet above for the exact mechanism). This applies to manual orders placed via the Bybit UI while the bot is running, orders from other tools/scripts on the same account, and stale orders left over from a prior run with different grid parameters. **Manual orders and the grid cannot coexist on the same symbol** — the bot treats "not in my grid" as "cancel it." To manually intervene, stop the bot, make your changes, restart, and accept that anything not matching the grid on restart will be cancelled on the first tick.
   - **Before first start**: Closing existing orders for the symbol before the first start is recommended for operator clarity (otherwise the bot will cancel them within ~1 second of startup, which is surprising but not incorrect). There is no config flag to disable either the cancel-on-mismatch behavior or the `(account, symbol)` uniqueness check — both are unconditional.
   - **Reconnect**: compare exchange state with in-memory, update tracked orders
   - **Periodic Order Sync (2026-02-16)**: Matches bbu2's `LIMITS_READ_INTERVAL` pattern (61s by default)
     - **Purpose**: Continuously reconcile order state via REST API to catch missed WebSocket updates
     - **Implementation**: `Orchestrator._order_sync_loop()` runs as asyncio task, calls `reconciler.reconcile_reconnect()` for each runner
     - **Configuration**: `order_sync_interval` in `GridbotConfig` (default 61.0 seconds, 0 to disable)
     - **Behavior**: Logs discrepancies (orders missing on exchange, orders missing in memory), injects missing orders, marks cancelled orders
     - **bbu2 reference**: Original bot used `LIMITS_READ_INTERVAL = 61` with hybrid WebSocket + REST pattern (WebSocket updates between REST syncs)
     - **Files**: `apps/gridbot/src/gridbot/config.py:90-93`, `apps/gridbot/src/gridbot/orchestrator.py:620-672`, `apps/gridbot/src/gridbot/reconciler.py:138-226`

6. **Wallet Balance Caching (2026-02-16)**: Reduces REST API load by caching wallet balance
   - **Purpose**: Minimize wallet balance API calls while maintaining reasonable freshness for position risk calculations
   - **Implementation**: `Orchestrator._get_wallet_balance()` checks cache before fetching from REST
   - **Configuration**: `wallet_cache_interval` in `GridbotConfig` (default 300.0 seconds = 5 minutes, 0 to disable)
   - **Behavior**: Returns cached balance if age < interval, otherwise fetches fresh and updates cache
   - **API call reduction**: ~79% fewer calls (from 57/hour to 12/hour per account at default settings)
   - **bbu2 reference**: Original bot cached for 620s (`GET_WALLET_INTERVAL = 10 * 62`); gridbot defaults to 300s for better freshness
   - **Lock design**: Single `asyncio.Lock` covers all accounts. Acceptable for low account counts; if many accounts run concurrently, consider per-account locks (`dict[str, asyncio.Lock]`) to avoid unnecessary serialization
   - **Files**: `apps/gridbot/src/gridbot/config.py:94-97`, `apps/gridbot/src/gridbot/orchestrator.py:479-525`

### Common Pitfalls

1. **BybitNormalizer Import**: Use `from bybit_adapter.normalizer import BybitNormalizer`, not `Normalizer`
2. **RiskConfig Parameters**: `max_margin`, not `min_margin` (see `gridcore.position.RiskConfig`)
3. **PositionState.margin is a RATIO, not a dollar amount**: `margin = positionValue / walletBalance` (bbu2 pattern). NOT Bybit's `positionIM`. All threshold configs (`max_margin`, `min_total_margin`) are ratio-based. See bbu2-master/position.py:105.
4. **PositionState.direction**: Required parameter, use `"long"` or `"short"`
4. **Test Isolation**: Run test suites separately due to conftest conflicts
5. **Position WebSocket-First Pattern (2026-01-26)**: Position updates use WebSocket as primary source, REST as fallback
   - **Original bbu2 pattern**: `handle_position()` stores WS data → `__get_position_status()` uses WS first, REST fallback
   - **Orchestrator implementation**: `_on_position()` stores WS data in `_position_ws_data[account][symbol][side]`
   - `_position_check_loop()` calls `_get_position_from_ws()` first, falls back to REST if None
   - **Benefits**: Real-time position updates vs 63s polling delay
   - File: `apps/gridbot/src/gridbot/orchestrator.py:337-391, 424-500`

6. **Same-Order Detection & Blocking (2026-02-01)**: Detects AND blocks duplicate orders at same price level (bbu2-style safety)
   - **Purpose**: Detect if two DIFFERENT orders at the SAME price got filled (indicates grid duplication bug). BLOCKS all new order placement when detected to prevent position accumulation that could cause liquidation.
   - **Implementation**: `StrategyRunner._check_same_orders()` monitors execution events
   - **Buffers**: Separate deques for long/short direction (maxlen=2, matches bbu2 `[:2]` per side)
   - **Direction logic**: Uses `closed_size != 0` (Bybit's `closedSize` field, not `closed_pnl`) to determine closing trades
     - `closed_pnl` can be 0 for break-even closes; `closed_size` is always non-zero for closing trades
     - Buy + not closing = opening long → long buffer
     - Sell + closing = closing long → long buffer
     - Buy + closing = closing short → short buffer
     - Sell + not closing = opening short → short buffer
   - **Blocking behavior** (matches bbu2 `_check_pair_step` returning early):
     - `on_ticker()`: Always passes event to engine (keeps `last_close` fresh), but skips intent execution
     - `on_execution()`: Still passes event to engine (grid state update) but skips intent execution
     - `on_order_update()`: Still passes event to engine (order state update) but skips intent execution
     - **Pattern**: All three handlers follow the same structure: engine always runs, only `_execute_intents()` is gated by `not self._same_order_error`
   - **CRITICAL: Both-side check** (matches bbu2): `_check_same_orders()` always checks BOTH long and short buffers on every execution event. If only the current side's buffer were checked, a clean fill on the opposite side would reset `_same_order_error` and silently clear the error. Pattern: check long → if error, return → check short.
   - **Auto-recovery** (bbu2-style): `_check_same_orders_side()` resets `_same_order_error=False` at the start before re-evaluating. Error auto-clears when a new fill at a different price arrives (1 clean fill pushes the older entry out of the 2-entry buffer, matching bbu2 `[:2]` behavior).
   - **Telegram alert**: Sends throttled Telegram notification on first detection via `Notifier`
   - **Error state**: `runner.same_order_error` property, manual reset with `reset_same_order_error()`
   - **NOT an error**: Same order_id at same price (partial fills are OK)
   - **leavesQty filter** (2026-02-02): Only fully filled orders (`leaves_qty == 0`) enter the buffer, matching bbu2 `handle_execution` filter (`leavesQty == '0'`). Partial fills are skipped to prevent buffer dilution.
   - **ExecutionEvent.closed_size / leaves_qty**: Added to `gridcore.events.ExecutionEvent`, extracted from Bybit `closedSize` and `leavesQty` in `bybit_adapter.normalizer`
   - **Retry queue dispatcher**: `_init_strategy()` creates a `_dispatch_intent()` closure that routes `CancelIntent` to `executor.execute_cancel` and `PlaceLimitIntent` to `executor.execute_place`. Without this, failed cancels would be retried via `execute_place`, causing stale orders to persist.
   - Files: `apps/gridbot/src/gridbot/runner.py`, `apps/gridbot/src/gridbot/orchestrator.py`, `packages/gridcore/src/gridcore/events.py`, `packages/bybit_adapter/src/bybit_adapter/normalizer.py`

7. **Exception Handling at WS Dispatch Boundary (2026-02-01)**: Two-layer exception handling prevents WS events from crashing the bot
   - **Orchestrator layer**: All WS callbacks (`_on_ticker`, `_on_position`, `_on_order`, `_on_execution`) wrapped with `try/except Exception` → catches normalization errors, logs + sends Telegram alert via `Notifier`
   - **Runner layer**: All event handlers (`on_ticker`, `on_execution`, `on_order_update`, `on_position_update`) wrapped with `try/except Exception` → logs with `exc_info=True` + re-raises so orchestrator can handle notification
   - **Pattern**: Runner logs + re-raises; orchestrator catches + notifies
   - Files: `apps/gridbot/src/gridbot/orchestrator.py`, `apps/gridbot/src/gridbot/runner.py`

8. **Telegram Notifier (2026-02-01)**: Thread-safe alert sender with throttling
   - **Config**: `notification.telegram.bot_token` and `notification.telegram.chat_id` in YAML config (gitignored)
   - **Throttle**: Max 1 Telegram alert per error key per 60 seconds (always logs)
   - **Thread-safe**: Sends in background daemon thread (WS callbacks run on pybit's thread)
   - **Graceful degradation**: If no Telegram config or `pyTelegramBotAPI` not installed, falls back to log-only
   - **telebot token format**: Token must contain a colon (e.g., `123456:ABC-DEF...`), otherwise `TeleBot()` raises
   - **Dependency**: `pytelegrambotapi>=4.24.0` in `apps/gridbot/pyproject.toml`
   - File: `apps/gridbot/src/gridbot/notifier.py`

9. **WebSocket Health Check Loop (2026-02-01)**: 10-second polling for connection health
   - **Orchestrator**: `_health_check_loop()` runs as asyncio task, checks `is_connected()` on each public/private WS
   - **Reconnect**: Only disconnected connections are reconnected (not all), re-subscribes all channels
   - **Lifecycle**: Started in `start()`, cancelled in `stop()` alongside `_position_check_task`
   - **Failure handling**: Reconnect failures are caught and alerted (don't crash the loop)
   - **WS client API**: `ws_client.is_connected()` and `ws_client.get_connection_state()` already existed in bybit_adapter
   - File: `apps/gridbot/src/gridbot/orchestrator.py`

### Test Commands
```bash
# Run gridbot tests
uv run pytest apps/gridbot/tests -v

# Run all project tests (separately due to conftest conflicts)
uv run pytest packages/gridcore/tests -v
uv run pytest packages/bybit_adapter/tests -v
uv run pytest shared/db/tests -v
uv run pytest apps/event_saver/tests -v
uv run pytest apps/gridbot/tests -v
uv run pytest apps/backtest/tests -v
```

## Phase F: Backtest Rewrite (backtest)

### Completed: 2026-02-03

Successfully implemented a backtest system using gridcore's GridEngine with trade-through fill model.

### Key Components

1. **Package Structure**
   ```
   apps/backtest/
   ├── pyproject.toml           # Dependencies: gridcore, grid-db (NO bybit_adapter)
   ├── conf/
   │   └── backtest.yaml.example
   ├── src/backtest/
   │   ├── __init__.py
   │   ├── config.py             # BacktestConfig, BacktestStrategyConfig
   │   ├── fill_simulator.py     # TradeThroughFillSimulator
   │   ├── position_tracker.py   # BacktestPositionTracker
   │   ├── order_manager.py      # BacktestOrderManager
   │   ├── executor.py           # BacktestExecutor
   │   ├── runner.py             # BacktestRunner
   │   ├── engine.py             # BacktestEngine
   │   ├── session.py            # BacktestSession
   │   ├── data_provider.py      # HistoricalDataProvider, InMemoryDataProvider
   │   └── main.py               # CLI entry point
   └── tests/
       └── test_*.py             # 60 tests
   ```

2. **Trade-Through Fill Model**
   - BUY fills when `current_price <= limit_price`
   - SELL fills when `current_price >= limit_price`
   - Fill price = limit price (conservative assumption)
   - Reference: `bbu_reference/backtest_reference/bbu_backtest-main/src/backtest_order_manager.py`

3. **Architecture**
   - Reuses `GridEngine` directly from gridcore (no modifications)
   - Separate from gridbot (no WebSocket, no bybit_adapter dependency)
   - In-memory order book simulation
   - Position tracking with PnL calculations
   - Funding simulation (8-hour intervals)

4. **Running Backtest**
   ```bash
   # Run with default config
   uv run python -m backtest.main --config conf/backtest.yaml

   # Run with date range
   uv run python -m backtest.main --config conf/backtest.yaml \
       --start "2025-01-01" --end "2025-01-31"

   # Export results to CSV
   uv run python -m backtest.main --config conf/backtest.yaml --export results.csv

   # Strict mode: exit on first symbol failure in multi-symbol runs
   uv run python -m backtest.main --config conf/backtest.yaml --strict
   ```
   - **Exit codes**: `0` = success, `1` = config/startup error, `2` = execution error

5. **Testing**
   ```bash
   uv run pytest apps/backtest/tests -v
   ```

### Key Implementation Notes

1. **Order Format for GridEngine**: Use camelCase keys (`orderId`, `orderLinkId`, `price` as string)
   - GridEngine expects Bybit API response format
   - `get_limit_orders()` returns `{"long": [...], "short": [...]}`
   - Keys: `orderId`, `orderLinkId`, `price` (string), `qty` (string), `side`

2. **Position Tracker vs gridcore.Position**
   - `BacktestPositionTracker`: Tracks size, entry price, realized/unrealized PnL
   - `gridcore.Position`: Handles risk multipliers (different purpose)
   - Backtest does NOT use gridcore.Position for PnL tracking

3. **Quantity Calculation**
   - Uses same amount format as gridbot: `"100"` (fixed USDT), `"x0.001"` (wallet fraction), `"b0.001"` (base currency)
   - `qty_calculator` function passed to executor

4. **Data Sources**
   - `HistoricalDataProvider`: From database (TickerSnapshot or PublicTrade tables)
   - `InMemoryDataProvider`: For testing with pre-created events

5. **Funding Simulation**
   - 8-hour intervals (00:00, 08:00, 16:00 UTC)
   - Long pays, short receives when rate > 0
   - Configurable via `enable_funding` and `funding_rate`

### Bug Fixes (2026-02-03)

**HIGH Priority:**

1. **Filled Orders Lose Direction**
   - **Issue**: `get_order_by_client_id()` only searched `active_orders`, but filled orders are moved to `filled_orders` before `_process_fill()` can look them up
   - **Fix**: Extended `get_order_by_client_id()` to also search `filled_orders`
   - **File**: `apps/backtest/src/backtest/order_manager.py:236-247`

2. **client_order_id Reuse Blocked Forever**
   - **Issue**: `_client_order_ids` set never removed IDs on fill/cancel, blocking deterministic ID reuse
   - **Fix**: Added `_client_order_ids.discard()` in `cancel_order()` and `check_fills()`
   - **Files**: `apps/backtest/src/backtest/order_manager.py:133-137, 178-185`

3. **Multi-Symbol Runs Contaminated**
   - **Issue**: `BacktestEngine.run()` didn't reset `_runners` or `_funding_simulator` state between runs
   - **Fix**: Added state reset at start of `run()`: `self._runners = {}`, `_last_prices = {}`, `_last_timestamp = None`, `_last_funding_time = None`
   - **File**: `apps/backtest/src/backtest/engine.py:138-144`

**MEDIUM Priority:**

4. **Funding Sign Tracking Inverted**
   - **Issue**: `get_total_pnl()` used `+ funding_paid` but `funding_paid` is positive when we paid (should decrease PnL)
   - **Fix**: Changed to `- funding_paid` in `get_total_pnl()` with corrected comment
   - **File**: `apps/backtest/src/backtest/position_tracker.py:196-203`

5. **close_all Wind-Down Mode Not Implemented**
   - **Issue**: `_wind_down()` only logged open positions, didn't actually close them
   - **Fix**: Implemented `_force_close_position()` that realizes PnL and records closing trades
   - **File**: `apps/backtest/src/backtest/engine.py:336-395`

6. **Test String-Decimal Type Error**
   - **Issue**: `test_process_tick_fills_order` did `buy_price - Decimal("100")` where `buy_price` was a string
   - **Fix**: Converted to `Decimal(buy_price)`
   - **File**: `apps/backtest/tests/test_runner.py:72`

7. **Missing Tests for Core Modules**
   - **Issue**: No tests for BacktestEngine, BacktestExecutor, InMemoryDataProvider, FundingSimulator
   - **Fix**: Added `test_engine.py` (14 tests) and `test_executor.py` (7 tests)
   - **Files**: `apps/backtest/tests/test_engine.py`, `apps/backtest/tests/test_executor.py`

### Bug Fixes (2026-02-04)

**HIGH Priority:**

8. **Multi-Strategy Equity Incorrect**
   - **Issue**: Each runner called `session.update_equity()` with only its own unrealized PnL; multi-strategy runs had incorrect equity/balance
   - **Fix**: Moved equity update to engine level in `_process_tick()` BEFORE runners execute (aggregates all runners' unrealized PnL)
   - **Files**: `apps/backtest/src/backtest/engine.py:327-330`, `apps/backtest/src/backtest/runner.py:186-188`

**MEDIUM Priority:**

9. **close_all Leaves Stale Unrealized PnL**
   - **Issue**: After force-closing, `unrealized_pnl` in tracker wasn't recalculated, causing non-zero final unrealized in metrics
   - **Fix**: Added `tracker.calculate_unrealized_pnl(price)` after `process_fill()` in `_force_close_position()`
   - **File**: `apps/backtest/src/backtest/engine.py:410-411`

10. **Wallet-Fraction Sizing Uses Stale Balance**
    - **Issue**: Orders used `session.current_balance` before equity was updated, so sizing lagged by one tick
    - **Fix**: Engine now updates equity BEFORE runners process tick (fix #8 addresses this)
    - **File**: `apps/backtest/src/backtest/engine.py:327-330`

11. **Equity Updates Before Fills Processed**
    - **Issue**: Equity updated before fills processed, so fills from current tick weren't reflected until next tick
    - **Fix**: Split `runner.process_tick()` into two phases: `process_fills()` and `execute_tick()`. Engine now: (1) processes fills for all runners, (2) updates equity, (3) executes tick intents
    - **Files**: `apps/backtest/src/backtest/runner.py:134-199`, `apps/backtest/src/backtest/engine.py:316-347`

### Design Decisions (2026-02-04)

1. **DB Persistence Skipped** - Backtest results are kept in-memory + CSV export only
   - No `BacktestExecution` or `Run` persistence models implemented
   - Use `BacktestReporter` for CSV export instead of database storage
   - Rationale: Simpler architecture, backtests are typically one-off runs not requiring persistent storage
   - Future: Can add DB persistence if multi-run comparison or long-term storage becomes necessary

2. **Strict Cross Fill Model** - Orders only fill when price CROSSES the limit (not touches)
   - BUY fills when `current_price < limit_price` (price must go BELOW)
   - SELL fills when `current_price > limit_price` (price must go ABOVE)
   - At limit price (`==`), order does NOT fill
   - Rationale: Conservative assumption - at limit price, fill is not guaranteed (queue position, volume at level)
   - Better for grid trading where orders sit at common price levels with competition
   - File: `apps/backtest/src/backtest/fill_simulator.py`

### Metrics & Reporting (2026-02-04)

1. **BacktestMetrics** - Full performance metrics in `session.py`:
   - Trade stats: total_trades, winning_trades, losing_trades, win_rate, avg_win, avg_loss
   - PnL: total_realized_pnl, total_unrealized_pnl, total_commission, total_funding, net_pnl
   - Risk: max_drawdown, max_drawdown_pct, max_drawdown_duration (ticks), sharpe_ratio
   - Balance: initial_balance, final_balance, return_pct
   - Activity: total_volume, turnover (volume / initial_balance)
   - Direction breakdown: long_trades, short_trades, long_pnl, short_pnl, long_profit_factor, short_profit_factor

2. **Sharpe Ratio Calculation** - `_calculate_sharpe_ratio()` in `session.py`:
   - Raw tick data has irregular spacing, so equity is resampled to fixed intervals before computing returns
   - Interval is parameterized via `finalize(sharpe_interval=timedelta(hours=1))` (default: 1 hour)
   - `_resample_equity(interval)`: Bins equity points into fixed-width buckets, takes last value per bucket, skips empty buckets
   - Annualization uses 365.25 days/year (crypto 24/7)
   - Formula: `(mean_return / std_return) * sqrt(periods_per_year)`

3. **BacktestReporter** - CSV export in `reporter.py`:
   - `export_trades(path)`: Trade history with notional values
   - `export_equity_curve(path)`: Equity and return % over time
   - `export_metrics(path)`: All metrics as key-value CSV
   - `export_all(output_dir)`: All exports to directory
   - `get_summary_dict()`: Metrics as Python dict (useful for programmatic access)

### Bug Fixes (2026-02-05)

**Unrealized PnL % (ROE) for Risk Management:**

1. **Added `calculate_unrealized_pnl_percent(current_price, leverage)` to BacktestPositionTracker**
   - Uses standard Bybit ROE formula: `(close - entry) / entry * leverage * 100` (long)
   - Short formula: `(entry - close) / entry * leverage * 100`
   - Added `unrealized_pnl_percent` field to `PositionState` dataclass
   - **File**: `apps/backtest/src/backtest/position_tracker.py:167-203`

**Instrument Info Fetching & Quantity Rounding:**

2. **`InstrumentInfoProvider` class (OOP refactor of `instrument_info.py`)**
   - Encapsulates `fetch_from_bybit`, `load_from_cache`, `save_to_cache`, `get` into a class
   - `cache_path` and `cache_ttl` are instance attributes (no more module-level state)
   - `pybit` import is lazy (inside `fetch_from_bybit` method only)
   - **API validation**: Rejects zero `qty_step`/`tick_size` from API (returns `None` → triggers cache fallback)
   - **24h cache TTL**: Each cache entry stores `cached_at` ISO timestamp; stale entries trigger API refresh
   - **TTL configurable**: `BacktestConfig.instrument_cache_ttl_hours` (default 24, passed to provider via engine)
   - **Fallback cascade**: fresh cache → API → stale cache → hardcoded defaults
   - **Backward-compatible**: Old cache files without `cached_at` are treated as stale (triggers refresh)
   - **Tests**: 30 tests in `test_instrument_info.py` (96% coverage)
   - **Files**: `apps/backtest/src/backtest/instrument_info.py`, `apps/backtest/tests/test_instrument_info.py`

3. **Integrated quantity rounding in BacktestEngine**
   - `BacktestEngine.__init__` creates `InstrumentInfoProvider` with TTL from config
   - `_init_runner()` calls `self._instrument_provider.get(symbol)` for instrument info
   - `_create_qty_calculator()` applies `instrument_info.round_qty()` to all qty calculations
   - **Files**: `apps/backtest/src/backtest/engine.py:112-114,235`, `apps/backtest/src/backtest/config.py:103-108`

4. **Added `pybit>=5.8` dependency**
   - Required for `HTTP().get_instruments_info()` public API call
   - **File**: `apps/backtest/pyproject.toml`

**Key Notes:**
- Quantity rounding uses `math.ceil` (round UP), not round to nearest
- Rationale: Ensures orders meet minimum size requirements, safer than rounding down
- bbu2 reference: `bbu_reference/bbu2-master/bybit_api_usdt.py:271-273`

### Test Improvements (2026-02-06)

**State Reset Test Fix:**

1. **Fixed `test_run_resets_state_between_runs` to actually validate reset**
   - **Issue**: Test ran same symbol twice, checking `len(runners) == 1` - would pass even without reset (same key overwrite)
   - **Fix**: Changed to run BTCUSDT then ETHUSDT (no strategy), assert `len(runners) == 0`
   - **Validates**: Multi-symbol runs don't accumulate old runners
   - **File**: `apps/backtest/tests/test_engine.py:145-169`

2. **Added `test_run_creates_new_runner_instances` for object identity check**
   - **Validates**: Each run creates new runner instances, not reusing old ones
   - **Pattern**: Store first runner reference, run again, assert `first_runner is not second_runner`
   - **File**: `apps/backtest/tests/test_engine.py:171-191`

**Testing Pattern for State Reset:**
- Use **multi-symbol approach** when testing cross-contamination between runs
- Use **object identity (`is not`)** when verifying fresh instances are created
- Both patterns together provide comprehensive validation of state reset

**WindDownMode Enum Refactoring:**

3. **Refactored `wind_down_mode` from string validation to StrEnum**
   - **Before**: `wind_down_mode: str` with `@field_validator` checking valid strings
   - **After**: `WindDownMode(StrEnum)` with values `LEAVE_OPEN`, `CLOSE_ALL`
   - **Benefits**: Type safety, IDE autocomplete, refactorability, consistency with gridcore enums
   - **Pattern**: Following established StrEnum pattern (DirectionType, SideType, GridSideType)
   - **Files**:
     - Enum: `apps/backtest/src/backtest/config.py:16-20`
     - Usage: `apps/backtest/src/backtest/engine.py:15,385`
     - Tests: `apps/backtest/tests/test_config.py:75`, `apps/backtest/tests/test_engine.py:82,93`
     - Export: `apps/backtest/src/backtest/__init__.py`

### Improvements (2026-02-11)

1. **Direction Inference Warning** - `runner.py:_infer_direction()`
   - Added `logger.warning` when fallback is used (indicates order tracking gap)
   - Should never trigger in normal operation — every fill comes from an order we placed
   - **File**: `apps/backtest/src/backtest/runner.py:254-271`

2. **Sharpe Ratio Resampling** - `session.py:_calculate_sharpe_ratio()`
   - Equity curve resampled to fixed intervals before computing returns (raw ticks are irregular)
   - Parameterized via `finalize(sharpe_interval=timedelta(hours=1))`
   - **Files**: `apps/backtest/src/backtest/session.py:302-390`

3. **Reporter DRY** - `reporter.py:_ensure_path()`
   - Extracted path creation helper to avoid repeating `Path(path)` + `mkdir(parents=True)` in every export method
   - **File**: `apps/backtest/src/backtest/reporter.py:48-52`

4. **CLI Exit Codes & --strict** - `main.py`
   - Exit code `1` = config/startup error, `2` = execution error
   - `--strict` flag: exit on first symbol failure in multi-symbol runs
   - **File**: `apps/backtest/src/backtest/main.py`

5. **Input Validation** - `position_tracker.py`
   - Commission rate bounds check in `__init__` (must be in `[0, 0.01]`)
   - Price/qty positive validation in `process_fill()`
   - Warning log for unusually high funding rates (> 1%)
   - **File**: `apps/backtest/src/backtest/position_tracker.py`

### Test Commands
```bash
# Run backtest tests
uv run pytest apps/backtest/tests -v

# Run all project tests (separately due to conftest conflicts)
uv run pytest packages/gridcore/tests -v
uv run pytest packages/bybit_adapter/tests -v
uv run pytest shared/db/tests -v
uv run pytest apps/event_saver/tests -v
uv run pytest apps/gridbot/tests -v
uv run pytest apps/backtest/tests -v
```

## Phase G: Comparator (backtest-vs-live validation)

### Completed: 2026-02-12

Successfully implemented a comparator package that validates backtest results against live trade data.

### Key Components

1. **Package Structure**
   ```
   apps/comparator/
   ├── pyproject.toml           # Dependencies: grid-db, backtest (workspace)
   ├── src/comparator/
   │   ├── __init__.py
   │   ├── config.py            # ComparatorConfig (Pydantic)
   │   ├── loader.py            # LiveTradeLoader, BacktestTradeLoader, NormalizedTrade
   │   ├── matcher.py           # TradeMatcher (join on client_order_id)
   │   ├── metrics.py           # ValidationMetrics, calculate_metrics()
   │   ├── equity.py            # Equity curve comparison (resample, divergence)
   │   ├── reporter.py          # CSV export and console summary
   │   └── main.py              # CLI with --backtest-trades / --backtest-config
   └── tests/
       ├── conftest.py          # Shared fixtures (db, make_trade, sample data)
       ├── test_loader.py       # 14 tests (partial fills, direction inference)
       ├── test_matcher.py      # 7 tests
       ├── test_metrics.py      # 19 tests
       ├── test_equity.py       # 13 tests
       ├── test_reporter.py     # 9 tests (incl. reused-ID CSV regression)
       └── test_main.py         # 17 tests (CLI args, datetime, equity paths)
   ```

2. **Trade Matching**
   - Joins on `(client_order_id, occurrence)` composite key (handles deterministic ID reuse)
   - `occurrence` = nth time the same `client_order_id` appears chronologically (sorted by `timestamp, client_order_id, side` for deterministic tie-breaking)
   - Produces: matched pairs, live-only (missed by backtest), backtest-only (phantom fills)
   - Live trades: aggregates partial fills by `(order_link_id, order_id)` using VWAP price
   - Same `order_link_id` + different `order_id` = lifecycle reuse (separate trades, not partial fills)

3. **Direction Inference (Live Trades)**
   - Live `PrivateExecution` has no direction field; inferred from `side` + `closed_pnl`:
     - `closed_pnl != 0` → closing trade: Buy+closing = short, Sell+closing = long
     - `closed_pnl == 0` → opening trade: Buy = long, Sell = short
   - **LIMITATION**: Break-even closes (`closed_pnl==0`) are misclassified as opening trades
   - Backtest trades carry direction from `BacktestTrade.direction`
   - **For matched pairs**: Metrics direction breakdown prefers backtest direction (always correct) over inferred live direction

4. **Validation Metrics**
   - Coverage: match_rate, phantom_rate, live/backtest trade counts
   - Price accuracy: mean/median/max absolute delta across matched pairs
   - Quantity accuracy: mean/median/max absolute delta
   - PnL: cumulative totals, delta, Pearson correlation of cumulative PnL curves
   - Fees: total comparison and delta
   - Volume: from both matched and unmatched trades
   - Direction breakdown: long/short match counts and PnL deltas
   - Timing: mean absolute time delta between matched pairs
   - Tolerance breaches: `price_tolerance=0` means exact match required (flags any non-zero delta); `qty_tolerance` same semantics
   - Equity curve: max/mean divergence, correlation

5. **Equity Curve Comparison** (`EquityComparator` class in `equity.py`)
   - OOP class following same pattern as `TradeMatcher` (no constructor args, methods are operations)
   - `load_live(session, ...)`: from `WalletSnapshot.wallet_balance` via `WalletSnapshotRepository.get_by_account_range()`
   - `load_backtest_from_csv(path)` / `load_backtest_from_session(equity_curve)`: from CSV export or `BacktestSession.equity_curve`
   - `resample(live, backtest, interval)`: resampled to common 1-hour grid
   - `compute_metrics(resampled)`: computes max/mean divergence and correlation
   - `export(resampled, path)`: exports `equity_comparison.csv`

6. **CLI Modes**
   - `--backtest-trades path.csv`: Load pre-existing backtest CSV
   - `--backtest-config path.yaml`: Run backtest from config, then compare
   - `--backtest-equity path.csv`: Optional equity curve CSV for equity comparison
   - `--coin USDT`: Coin for live wallet balance lookup
   - Date parsing: date-only `--end` values auto-set to 23:59:59.999999

7. **Testing**
   ```bash
   uv run pytest apps/comparator/tests --cov=comparator --cov-report=term-missing -v
   ```
   105 tests, 96% coverage (main.py: 83%)

### Key Implementation Notes

1. **OOP Consistency** - All comparator modules use classes, not standalone functions
   - `LiveTradeLoader`, `BacktestTradeLoader` (loader.py)
   - `TradeMatcher` (matcher.py)
   - `EquityComparator` (equity.py)
   - `ComparatorReporter` (reporter.py)
   - Pattern: No-arg constructors for pure computation classes (`TradeMatcher`, `EquityComparator`); session/state passed per-method

2. **NormalizedTrade Dataclass** - Common format bridging live and backtest data
   - Fields: `client_order_id`, `symbol`, `side`, `price`, `qty`, `fee`, `realized_pnl`, `timestamp`, `source`, `direction`, `occurrence`
   - `source` is "live" or "backtest"
   - `direction` is "long" or "short" (inferred for live, carried for backtest)
   - `occurrence` is 0-based index for client_order_id reuse handling

2. **Pearson Correlation** - Custom implementation in `metrics.py`
   - Used for both PnL curve and equity curve correlation
   - Returns 0.0 for insufficient data or zero variance

3. **End-Date Truncation Fix**
   - `_parse_datetime("2025-01-31")` returns midnight, dropping the final day
   - Fix: `end_of_day=True` param sets time to `23:59:59.999999` for date-only inputs

4. **DB Layer Extension**
   - Added `WalletSnapshotRepository.get_by_account_range()` for time-range equity queries
   - File: `shared/db/src/grid_db/repositories.py`

### Common Pitfalls

1. **SQLite Strips Timezone Info** - Compare datetimes with `.replace(tzinfo=None)` in tests. In integration tests with in-memory SQLite, use naive timestamps for test data to match DB output.
2. **Direction != Side** - A Sell can close a long position; use `direction` field, not `side`
3. **Volume from Unmatched Trades** - Must be computed before early return on zero matched pairs
4. **client_order_id Reuse** - Deterministic SHA256 produces same ID for same (symbol, side, price, direction). After fill/cancel, the ID can be reused for a new order lifecycle. Live data: use `(order_link_id, order_id)` to distinguish partial fills (same order_id) from reuse (different order_id). Matcher: use `(client_order_id, occurrence)` composite key.
5. **Tolerance Semantics** - `tolerance=0` means exact match (any non-zero delta is flagged). Don't add `> 0` guards that would disable the check at zero.
6. **Break-Even Close Direction** - Live direction inferred from `closed_pnl != 0` is fragile for break-even closes. Always prefer backtest direction for matched pair analysis.
7. **Datetime UTC Normalization** - `_parse_datetime()` normalizes all inputs to UTC. Aware non-UTC inputs are converted via `.astimezone(timezone.utc)`. Naive inputs get UTC assigned.
8. **Reporter Delta Lookup** - Use `zip(matched, trade_deltas)` not a dict keyed by `client_order_id` — the dict approach fails when IDs are reused. The deltas list is 1:1 with matched pairs by construction.
9. **Breach Tuples** - `ValidationMetrics.breaches` stores `(client_order_id, occurrence)` tuples, not bare strings. This avoids ambiguity when IDs are reused.
10. **Occurrence Tie-Breaking Limitation** - If two trades share exact `(timestamp, client_order_id, side)`, occurrence assignment is non-deterministic across sources. Extremely unlikely in practice (requires same hash reused at same millisecond with same side).
11. **Timestamp Normalization** - All loader/equity entry points call `_normalize_ts()` from `loader.py` to strip timezone info, producing naive UTC datetimes. This prevents `TypeError` when comparing/subtracting timestamps from different sources (SQLite returns naive, CSV `fromisoformat()` can return aware). When adding new data entry points, always wrap timestamps with `_normalize_ts()`.
12. **Config Mode Requires --symbol** - `--symbol` is required when using `--backtest-config` (returns exit code 1 if omitted). This prevents silent defaulting to BTCUSDT for non-BTC strategies. In CSV mode, `--symbol` remains optional.
13. **Symmetric Symbol Filtering** - `run()` filters `backtest_trades` by `config.symbol` before matching. This ensures both live and backtest sides use the same symbol filter, whether trades come from CSV (which may contain multiple symbols) or config mode.

## Phase H: Testing & Validation

### Completed: 2026-02-14

Successfully implemented comprehensive unit test coverage improvements, cross-package integration tests, and shadow-mode validation pipeline tests.

### Key Components

1. **Coverage Improvements (Unit Tests)**
   - `bybit_adapter/rest_client.py`: 16% → 100% (45 tests in `test_rest_client.py`)
   - `event_saver/main.py`: 18% → 70%+ (30 tests in `test_main.py`)
   - `event_saver/collectors`: 26-29% → 70%+ (tests in `test_public_collector.py`, `test_private_collector.py`)
   - `gridbot/main.py`: 0% → 60%+ (14 tests in `test_main.py`)
   - `bybit_adapter/ws_client.py`: 6 new edge case tests added

2. **Cross-Package Integration Tests** (`tests/integration/`)
   ```
   tests/integration/
   ├── __init__.py
   ├── conftest.py                    # Shared fixtures (make_ticker_event, generate_price_series)
   ├── test_engine_to_executor.py     # 15 tests: GridEngine → IntentExecutor pipeline + REST payload mapping
   ├── test_backtest_to_comparator.py # 5 tests: BacktestEngine → Comparator round-trip
   ├── test_runner_lifecycle.py       # 9 tests: StrategyRunner full lifecycle (fills, position, same-order)
   ├── test_eventsaver_db.py          # 10 tests: EventSaver → Database pipeline + writer integration
   └── test_shadow_validation.py      # 6 tests: Shadow-mode dual-path validation
   ```

3. **Shadow-Mode Validation Pipeline** (`test_shadow_validation.py`)
   - Feeds identical price data through two independently constructed paths:
     - **Path A**: `BacktestEngine` (orchestrated, high-level)
     - **Path B**: Manual `GridEngine + BacktestOrderManager` (low-level, mimics shadow mode)
   - Validates: trade count match, deterministic client_order_ids, 100% comparator match rate, zero price/qty deltas, identical PnL totals
   - Uses `generate_price_series()` for reproducible sine-wave price oscillation

### Running Tests

```bash
# Run all tests (unit + integration)
make test

# Run integration tests only
make test-integration

# Run specific integration test
uv run pytest tests/integration/test_shadow_validation.py -v
```

### Key Implementation Notes

1. **Event Constructor Requirements**
   - All gridcore event dataclasses require `event_type` and `local_ts` fields (Python dataclass inheritance)
   - Example: `TickerEvent(event_type=EventType.TICKER, symbol=..., exchange_ts=..., local_ts=..., last_price=...)`

2. **BybitNormalizer Does Not Fail on Invalid Input**
   - `normalize_ticker({"invalid": "data"})` creates a default event instead of raising
   - To test error paths, mock the normalizer: `collector._normalizer.normalize_ticker = MagicMock(side_effect=Exception(...))`

3. **PrivateExecution Has No `user_id` Field**
   - SQLAlchemy model does not have `user_id` column (only `run_id` and `account_id`)
   - Cascade delete via `Run` → `PrivateExecution` (not direct user linkage)

4. **IntentExecutor Attribute Names**
   - REST client stored as `self._client` (not `self._rest_client`)
   - File: `apps/gridbot/src/gridbot/executor.py`

5. **Backtest Fill Parameters**
   - Amplitude=2000 and num_ticks=500 needed for reliable trade generation with grid_step=0.5 and grid_count=20
   - Smaller amplitudes may not cross enough grid levels to trigger fills

6. **TradeMatcher Returns MatchResult Object**
   - `matcher.match()` returns `MatchResult`, not a tuple
   - Access via: `result.matched`, `result.live_only`, `result.backtest_only`

7. **ValidationMetrics Field Names**
   - Uses `price_mean_abs_delta` (not `price_mean_delta`)
   - Uses `cumulative_pnl_delta` (not `pnl_delta`)

8. **Qty Rounding for Shadow-Mode Tests**
   - Must match `InstrumentInfo.round_qty()` which rounds UP via `math.ceil`
   - Default qty_step for BTCUSDT: `Decimal("0.001")`
   - Pattern: `steps = math.ceil(float(raw_qty) / float(qty_step)); return Decimal(str(steps)) * qty_step`

9. **Makefile Integration Test Target**
   - Integration tests run as the final step in `make test` (after all per-package tests)
   - Coverage is appended (`--cov-append`) so integration test coverage counts toward total
   - `make test-integration` runs integration tests in isolation

### Common Pitfalls

1. **conftest ImportPathMismatchError**: Run test suites separately (per-directory) to avoid conftest conflicts across packages
2. **SQLite Strips Timezone**: Use naive UTC timestamps in test data for in-memory SQLite tests
3. **Shadow-Mode Qty Calculator**: Must replicate `BacktestEngine._create_qty_calculator()` exactly, including `InstrumentInfo.round_qty()` ceil rounding
4. **generate_price_series**: Uses sine-wave oscillation; period = `num_ticks / 4` (4 complete oscillations). Increase `amplitude` for more fills.
5. **Mocking `async def` functions in cli() tests**: When `cli()` calls `asyncio.run(main(...))`, patching `main` with `return_value=0` auto-creates an `AsyncMock` that still returns a coroutine. Use `_close_dangling_coro(mock_run)` helper (in `test_main.py`) to close the unawaited coroutine after assertions, silencing warnings.
6. **`asyncio.get_event_loop()` deprecation in tests**: Use `asyncio.new_event_loop()` instead of `asyncio.get_event_loop()` when setting up event loops in non-async test methods (e.g., `saver._event_loop = asyncio.new_event_loop()`).
7. **PlaceLimitIntent constructor**: Requires `qty` and `grid_level` positional args — cannot construct with just symbol/side/price/direction/client_order_id.
8. **Integration test discovery**: Must add `"tests/integration"` to `testpaths` in `pyproject.toml` for pytest to discover them.
9. **Import ordering in test files**: Never place class/dataclass definitions between import blocks. All imports must be grouped at the top of the file before any class or function definitions (e.g., `test_eventsaver_db.py` had `SeededDb` splitting import blocks).
10. **`asyncio.CancelledError` is a `BaseException`**: In nested try/except patterns, `CancelledError` passes through `except Exception` blocks. Always catch it in the outer loop with a comment explaining why. Also wrap any `await asyncio.sleep()` inside `except Exception` recovery handlers with its own `except asyncio.CancelledError: break` (see `orchestrator.py:_order_sync_loop`).
11. **Blocking I/O in async code**: Use `asyncio.to_thread()` to wrap blocking calls (e.g., SQLAlchemy `session.commit()`) in async methods. Requires Python 3.9+ (`pyproject.toml` declares `>=3.11`).
12. **Dict iteration in async loops**: Snapshot mutable dicts with `list(d.items())` before iterating in background tasks (`_position_check_loop`, `_order_sync_loop`). The main event loop can mutate `_account_to_runners` between `await` points, causing `RuntimeError: dictionary changed size during iteration`.
13. **Logging style in orchestrator loops**: Use `%s`-style format args (`logger.error("msg: %s", var)`) not f-strings (`logger.error(f"msg: {var}")`) in loop error/warning/info/debug handlers. Avoids string interpolation when log level is disabled. File: `apps/gridbot/src/gridbot/orchestrator.py`.
14. **`integration_helpers.py` import path**: `tests/integration/conftest.py` adds `tests/integration/` to `sys.path` explicitly so `import integration_helpers` works even when pytest is invoked without the root `pyproject.toml` `pythonpath` setting (e.g., per-app test runs).
15. **`_fetch_wallet_balance` fallback**: Returns `0.0` when no USDT balance is found in the wallet API response, but now logs `logger.warning` first so unexpected API structures are visible in logs.

## Phase I-1: Standalone Data Recorder (`apps/recorder/`)

### Completed: 2026-02-18

Standalone app that captures raw Bybit mainnet WebSocket data to SQLite for multi-day unattended runs, independent of any trading activity. Reuses `event_saver` collectors + writers directly.

**Documentation**: See `docs/features/0008_PLAN.md` for architecture and `docs/features/0008_REVIEW.md` for review notes.

### Key Implementation Notes

1. **App Structure**
   - Path: `apps/recorder/` (workspace package)
   - Entry point: `recorder.main:cli` (`--config PATH`, `--debug`), registered in `pyproject.toml` `[project.scripts]`
   - Run via: `uv run recorder --config path/to/config.yaml`
   - Config: YAML-based with Pydantic validation (`recorder.config`)
   - Core orchestrator: `recorder.recorder.Recorder`

2. **Reuse Pattern**
   - Imports `PublicCollector`, `PrivateCollector`, `AccountContext` from `event_saver.collectors`
   - Imports all 6 writers from `event_saver.writers`
   - Imports `GapReconciler` from `event_saver.reconciler`
   - No code duplication — recorder is a thin orchestration wrapper

3. **Fixed UUIDs for DB Seeding**
   - Uses stable UUIDs (`_RECORDER_USER_ID`, `_RECORDER_ACCOUNT_ID`, `_RECORDER_STRATEGY_ID`) across restarts
   - `_seed_db_records()` upserts User/BybitAccount/Strategy via `session.merge()`, creates new Run per session
   - Required because execution/order writers need valid `run_id` FK chain

4. **Strategy.symbol VARCHAR(20) Limit**
   - Store only the first/primary symbol in `Strategy.symbol`
   - Store full symbol list in `config_json["symbols"]` for reference
   - Avoids overflow when recording multiple symbols

5. **Private Gap Reconciliation**
   - `_handle_private_gap()` calls `reconcile_executions()` per symbol (not just logging)
   - Requires account credentials + run_id + symbols to be set

6. **Thread-Safe Async Routing**
   - All WS handlers use `asyncio.run_coroutine_threadsafe()` to route from WS thread to event loop
   - `self._event_loop` captured during `start()` via `asyncio.get_running_loop()`
   - **Every future gets a `_log_future_error()` done-callback** — never discard futures silently, especially in multi-day unattended tools

7. **Config Search Order**
   - `RECORDER_CONFIG_PATH` env var → `conf/recorder.yaml` → `recorder.yaml`
   - Handles `yaml.YAMLError` (raises ValueError), empty YAML (defaults to `{}`)

8. **SecretStr for API Credentials**
   - `AccountConfig.api_key` and `api_secret` use Pydantic `SecretStr` to prevent accidental logging
   - Access secrets via `.get_secret_value()` at the call site (e.g., `config.account.api_key.get_secret_value()`)
   - Database URLs are sanitized via `redact_db_url()` from `grid_db.utils` before logging (strips passwords from PostgreSQL URLs)

9. **Lifecycle Safety Patterns**
   - `self._running = True` is set at the **top** of `start()` (before resource init), inside a `try/except` that re-raises. This ensures `stop()` can clean up partially-initialized resources (e.g. writer flush-loop tasks) if `start()` raises midway. The `except` block intentionally leaves `_running = True` so `main.py`'s `stop(error=True)` proceeds with cleanup.
   - `stop(error=True)` marks the DB run as `"error"` instead of `"completed"` — called from the error path in `main.py`
   - `_seed_db_records()` wraps DB ops in try/except → raises `RuntimeError` with clear message
   - `_mark_run_status()` wraps DB ops in try/except → **logs** error (doesn't raise) to avoid interrupting shutdown
   - Signal handlers in `run_until_shutdown()` are cleaned up via `try/finally` + `loop.remove_signal_handler()`

10. **`setup_logging` Handler Guard**
    - `root_logger.handlers.clear()` before `addHandler()` prevents handler accumulation on repeated calls

11. **Logging Style: f-strings**
    - Use f-strings for all `logger.*()` calls: `logger.info(f"Starting {name}")`
    - Exception: %-style is acceptable in hot-path callbacks (e.g., `_log_future_error`) where deferred formatting matters

### Common Pitfalls (Recorder-Specific)

1. **TickerEvent fields**: Does NOT have `index_price` or `next_funding_time` — check `gridcore.events.TickerEvent` dataclass definition before constructing test fixtures.
2. **Mock collectors need `stop = AsyncMock()`**: When mocking `PublicCollector`/`PrivateCollector`, must set `stop` as `AsyncMock()` since `Recorder.stop()` awaits them.
3. **`_close_dangling_coro()` pattern**: When testing `cli()` that calls `asyncio.run(main(...))`, the mock creates an unawaited coroutine. Use the helper to close it after assertions (same pattern as gridbot `test_main.py`).
4. **Testnet default differs**: Recorder defaults to `testnet=False` (mainnet), unlike gridbot which defaults to `testnet=True`.
5. **Position/wallet test data format**: `PositionWriter` and `WalletWriter` expect Bybit-formatted dicts with `"data"` keys (e.g., `{"data": [{"symbol": "BTCUSDT", ...}]}`). Flat dicts silently produce zero snapshots.
6. **Test fixture deduplication**: Shared `db` fixture lives in `conftest.py` — do not duplicate in individual test files. Same for `basic_config` and `config_with_account`.
7. **Mock config completeness**: When using `MagicMock()` for config in tests, set all attributes that `main()` accesses before the code path under test. E.g., `mock_config.database_url = "sqlite:///test.db"` — bare MagicMock attributes break `urlparse()`.

## Phase J: Replay Engine (`apps/replay/`)

### Completed: 2026-02-21

Replay engine that reads recorded mainnet data from the recorder's database, feeds it through GridEngine + simulated order book, and compares simulated trades against real recorded executions. Core shadow-mode validation pipeline: `record → replay → compare → report`.

**Documentation**: See `docs/features/0009_PLAN.md` for architecture and `docs/features/0009_REVIEW.md` for review notes.

### Key Implementation Notes

1. **App Structure**
   - Path: `apps/replay/` (workspace package)
   - Entry point: `python -m replay.main` (`--config PATH`, `--run-id UUID`, `--start/--end`, `--debug`)
   - Config: YAML-based with Pydantic validation (`replay.config`)
   - Core orchestrator: `replay.engine.ReplayEngine`

2. **Massive Reuse — No New Data/Matching Logic**
   - `HistoricalDataProvider` (backtest) — reads TickerSnapshots from recorder DB
   - `BacktestRunner` (backtest) — two-phase tick processing with GridEngine
   - `BacktestOrderManager`, `TradeThroughFillSimulator`, `BacktestPositionTracker` (backtest)
   - `LiveTradeLoader`, `BacktestTradeLoader`, `TradeMatcher`, `calculate_metrics` (comparator)
   - `ComparatorReporter` (comparator) — CSV + console report
   - Replay engine is a thin orchestrator wiring these together

3. **Config Shape: Root-Level Replay Parameters**
   - `initial_balance`, `enable_funding`, `wind_down_mode` live at **root level** of `ReplayConfig`, NOT nested under `strategy`
   - They are backtest/replay parameters, not grid-strategy parameters
   - `strategy:` block only contains grid config (tick_size, grid_count, grid_step, amount, commission_rate)
   - File: `apps/replay/src/replay/config.py`

4. **Run Resolution (`_resolve_run()`)**
   - Auto-discover: queries `RunRepository.get_latest_by_type("recording")` — filters to `("completed", "running")` status by default
   - Explicit `run_id`: looks up Run row from DB if timestamps are missing (no hard-fail)
   - Active runs (`end_ts=None`): falls back to `datetime.now(timezone.utc)` instead of failing
   - File: `apps/replay/src/replay/engine.py`

5. **ISO Datetime Parsing**
   - `parse_datetime()` uses `datetime.fromisoformat()` as primary parser — handles `T` separator, `Z` suffix, `+00:00` offsets
   - Falls back to strptime for non-ISO formats (slash separators)
   - Parsing happens inside `try/except ValueError` block so invalid CLI input returns exit code 1
   - File: `apps/replay/src/replay/main.py`

6. **Credential Redaction in Logs — `redact_db_url()`**
   - Shared utility: `from grid_db import redact_db_url` (or `from grid_db.utils import redact_db_url`)
   - Replaces password with `***`, preserves username/host/port/path — SQLite URLs pass through unchanged
   - Used by: `apps/replay/src/replay/main.py`, `apps/replay/src/replay/engine.py`, `apps/recorder/src/recorder/main.py`
   - **Always use this** when logging database URLs — never log `config.database_url` directly
   - File: `shared/db/src/grid_db/utils.py`, tests: `shared/db/tests/test_utils.py`

7. **Config Search Order**
   - `--config` CLI arg → `REPLAY_CONFIG_PATH` env var → `conf/replay.yaml` → `replay.yaml`
   - Same pattern as recorder

8. **`RunRepository.get_latest_by_type()` Status Filter**
   - Added `statuses` parameter defaulting to `("completed", "running")` — skips failed/errored runs
   - Pass `statuses=()` to disable filtering (returns any status)
   - File: `shared/db/src/grid_db/repositories.py`

### Common Pitfalls (Replay-Specific)

1. **InMemoryDataProvider for tests**: Use `data_provider=` parameter in `engine.run()` to bypass DB reads — avoids needing real TickerSnapshot rows in test DB.
2. **InstrumentInfoProvider must be mocked**: Tests use `@patch("replay.engine.InstrumentInfoProvider")` — the provider tries to fetch real instrument info otherwise.
3. **Run resolution needs full FK chain**: When seeding test DB for run resolution tests, must create User → BybitAccount → Strategy → Run (foreign key constraints).
4. **`datetime.fromisoformat()` requires Python 3.11+** for full timezone offset support. Earlier versions don't handle `+00:00`.
5. **Test for `ValidationError` not `Exception`**: Pydantic config validation tests should use `from pydantic import ValidationError` for specific assertions.

## PnL Calculation Functions (`packages/gridcore/src/gridcore/pnl.py`) — Added 2026-02-24

Pure PnL calculation functions extracted into gridcore as the single source of truth.

**Functions exported from gridcore:**
- `calc_unrealised_pnl(direction, entry_price, current_price, size)` — Absolute PnL
- `calc_unrealised_pnl_pct(direction, entry_price, current_price, leverage)` — Standard Bybit ROE %
- `calc_position_value(size, entry_price)` — Notional value (entry-based, matches Bybit)
- `calc_initial_margin(position_value, leverage)` — Initial margin
- `calc_liq_ratio(liq_price, current_price)` — Liquidation ratio
- `calc_maintenance_margin(position_value, symbol, tiers=None)` — Tier-based MM (supports dynamic tiers)
- `calc_imr_pct(total_im, margin_balance)` — Account IMR %
- `calc_mmr_pct(total_mm, margin_balance)` — Account MMR %
- `calc_margin_ratio(position_value, wallet_balance)` — Per-position margin ratio (positionValue / walletBalance)
- `parse_risk_limit_tiers(api_tiers)` — Bybit API response → `MMTiers`

All take Decimal inputs; `position.py` keeps float copy for risk mgmt performance.

### Grid Anchor Persistence

Grid levels preserved across restarts via `GridAnchorStore` + `anchor_price` parameter.

- On startup: load saved anchor for `strat_id`
- If `grid_step` AND `grid_count` match → use saved anchor; otherwise rebuild fresh
- After first grid build: save new anchor data
- Storage: `db/grid_anchor.json` keyed by `strat_id`
- `GridEngine` requires `strat_id` parameter

### Logging

Uses Python `logging` module. Loggers: `gridcore.grid`, `gridcore.engine`, `gridcore.position`.
- `INFO`: grid rebuild, position adjustments
- `DEBUG`: detailed state/calculations

---

## grid_db — Multi-Tenant Database Layer

**Path**: `shared/db/` | **Tables**: users, bybit_accounts, api_credentials, strategies, runs, public_trades, private_executions, plus position/wallet snapshots and orders

### Key Rules

- **CRITICAL**: All queries MUST filter by `user_id` for data isolation
- `BaseRepository` does NOT expose `get_by_id`/`get_all` (removed for safety)
- Use `String(36)` for UUIDs, `BigInteger().with_variant(Integer, "sqlite")` for high-volume PKs
- SQLite: requires `PRAGMA foreign_keys=ON` on every connection; `StaticPool` ONLY for `:memory:`
- PostgreSQL URL encoding: use `urllib.parse.quote_plus()` for connection components (not port)
- All FKs have `ondelete="CASCADE"` + ORM `cascade="all, delete-orphan"`
- Use `DatabaseFactory.get_session()` context manager for auto commit/rollback
- Bulk inserts use `ON CONFLICT DO NOTHING` (trades/executions) or `ON CONFLICT DO UPDATE` (orders)
- `redact_db_url()` from `grid_db.utils` — **always** use when logging DB URLs

### Enums

- `RunType`: `RunType.LIVE`, `RunType.BACKTEST`, `RunType.SHADOW` — StrEnum in `grid_db.enums`

### Environment Variables

`GRIDBOT_DB_TYPE`, `GRIDBOT_DB_NAME`, `GRIDBOT_DB_HOST`, `GRIDBOT_DB_PORT`, `GRIDBOT_DB_USER`, `GRIDBOT_DB_PASSWORD`

---

## bybit_adapter — Exchange Interface

**Path**: `packages/bybit_adapter/` | **Dependencies**: `pybit>=5.8`, `gridcore`

### Components

- `normalizer.py` — Converts Bybit WebSocket messages to gridcore events
- `ws_client.py` — Public/Private WebSocket clients with heartbeat watchdog
- `rest_client.py` — REST API with rate limiting
- `rate_limiter.py` — Sliding window with exponential backoff

### Event Normalization

| Source | Target | Key Fields |
|--------|--------|------------|
| `publicTrade.{symbol}` | `PublicTradeEvent` | trade_id, exchange_ts, side, price, size |
| `execution` | `ExecutionEvent` | exec_id, order_id, order_link_id, price, qty, fee, closed_pnl |

Filters: `category=="linear"`, `execType=="Trade"`, `orderType=="Limit"`

### Key Rules

- Import as `from bybit_adapter.normalizer import BybitNormalizer` (not `Normalizer`)
- `BybitRestClient` requires `api_key` and `api_secret` (even if empty for public endpoints)
- REST methods are synchronous `def` (not async) — wrap with `asyncio.to_thread()` in async code
- `get_executions()` returns `tuple[list, cursor]`
- WebSocket handlers run on pybit's thread — use `asyncio.run_coroutine_threadsafe()` not `asyncio.create_task()`

### Bybit V5 API Status

Valid: `New`, `PartiallyFilled`, `Filled`, `Cancelled`, `Rejected`, `Untriggered`, `Triggered`, `Deactivated`

**`Active` is V3 legacy** — bbu2 checked it but V5 never returns it. gridcore only checks V5 statuses.

---

## event_saver — Data Capture

**Path**: `apps/event_saver/`

### Key Rules

- `DatabaseFactory` expects `DatabaseSettings` object, NOT a raw URL string
- `PrivateExecution` model uses `exec_price`, `exec_qty`, `exec_fee` (not `price`, `qty`, `fee`)
- `run_id` is REQUIRED for PrivateExecution FK; events without it are filtered out
- `symbols` field is string — use `config.get_symbols()` to get list
- `PublicTradeRepository.exists_by_trade_id()` takes only `trade_id` (no symbol param)

### Environment Variables

`EVENTSAVER_SYMBOLS`, `EVENTSAVER_TESTNET`, `EVENTSAVER_BATCH_SIZE`, `EVENTSAVER_FLUSH_INTERVAL`, `EVENTSAVER_GAP_THRESHOLD_SECONDS`, `EVENTSAVER_DATABASE_URL`

---

## gridbot — Live Trading Bot

**Path**: `apps/gridbot/`

### Architecture

- Single process, all accounts
- YAML config, hybrid event loop (async WebSocket + periodic polling)
- Data flow: `WebSocket → Orchestrator → StrategyRunner → GridEngine.on_event() → Intents → Executor → Bybit REST`
- Shadow mode: `shadow_mode=True` → intents logged, not executed; returns `shadow_{client_order_id}`

### Key Patterns

- **Order tracking**: `TrackedOrder` dataclass, deterministic 16-char hex `client_order_id`
- **Position risk**: `StrategyRunner` owns linked `Position` pair; periodic check (63s default)
- **Event routing**: `_symbol_to_runners` (ticker), `_account_to_runners` (position/order/execution)
- **Reconciliation**: Startup (adopt existing orders) + reconnect (compare exchange vs in-memory) + periodic (61s, `order_sync_interval`)
- **Wallet caching**: `wallet_cache_interval` (300s default), reduces API calls ~79%
- **Position updates**: WebSocket-first, REST fallback (`_position_ws_data` cache)

### Same-Order Detection & Blocking

Detects duplicate orders at same price level → BLOCKS all new order placement to prevent liquidation.
- Separate deques per direction (maxlen=2, matches bbu2)
- Direction: uses `closed_size != 0` (not `closed_pnl`) to detect closing trades
- Only fully filled orders (`leaves_qty == 0`) enter buffer
- Engine always runs; only `_execute_intents()` is gated by `not self._same_order_error`
- Always checks BOTH sides on every execution event
- Auto-recovers when new fill at different price arrives

### Exception Handling

Two-layer: Runner logs + re-raises → Orchestrator catches + sends Telegram alert via `Notifier`.

### Telegram Notifier

- Config: `notification.telegram.bot_token` + `chat_id` in YAML
- Throttle: 1 alert per error key per 60s
- Thread-safe (daemon thread), graceful degradation if not configured
- Dependency: `pytelegrambotapi>=4.24.0`

### Embedded EventSaver (`--save-events`)

- CLI flag `--save-events` or config `enable_event_saver: true` starts an embedded `EventSaver` alongside the trading bot
- EventSaver maintains its own WS connections (separate from orchestrator's) for raw data capture
- Startup order matters: Run records → EventSaver → gridbot WS connect (no capture gap)
- `_create_run_records()` creates User/BybitAccount/Strategy/Run rows with deterministic UUIDs via `uuid5(namespace, "type:name")`
- `_run_ids` dict is keyed by `strat_id` (not account name) — Run is strategy-scoped
- **Multi-strategy accounts**: `run_id` is set to `None` because `AccountContext` is account-scoped but `Run` is strategy-scoped. Executions/orders are captured but not persisted to DB. Positions/wallet/tickers still work. Fixing this requires per-symbol run_id mapping in EventSaver's normalizer pipeline.
- Accounts with zero strategies are skipped (empty `symbols=[]` means "no filter" in `PrivateCollector`, which would over-collect)
- Plan/review docs: `docs/features/0014_PLAN.md`, `docs/features/0014_REVIEW.md`
- **Debug walkthrough (architecture + breakpoint checklist)**: `docs/architecture/gridbot-save-events-debug.md`

### Key Pitfalls

- `RiskConfig` uses `max_margin` (not `min_margin`)
- **`PositionState.margin` is a RATIO** (`positionValue / walletBalance`), NOT Bybit's `positionIM` dollar amount
- `PositionState.direction` is required
- Retry queue needs `_dispatch_intent()` closure to route Cancel vs Place correctly
- `asyncio.CancelledError` is `BaseException` — passes through `except Exception`
- Snapshot mutable dicts with `list(d.items())` before async iteration

---

## backtest — Backtest Engine

**Path**: `apps/backtest/` | **Dependencies**: gridcore, grid-db (NO bybit_adapter)

### Architecture

- Reuses `GridEngine` directly, no modifications
- In-memory order book, trade-through fill model, position tracking with PnL
- Funding simulation (8-hour intervals)
- **Strict cross fill**: BUY fills when `price < limit` (not `<=`), SELL when `price > limit`

### Key Patterns

- **Order format for GridEngine**: camelCase keys (`orderId`, `orderLinkId`, `price` as string)
- **`BacktestPositionTracker`** tracks PnL; **`gridcore.Position`** handles risk multipliers (different purposes)
- **Quantity**: same amount format as gridbot (`"100"`, `"x0.001"`, `"b0.001"`); rounding uses `math.ceil`
- **Two-phase tick**: `process_fills()` → equity update → `execute_tick()` (fills reflected before sizing)
- **Equity update**: Engine level, not runner level (aggregates all runners' unrealized PnL)
- **`WindDownMode` StrEnum**: `LEAVE_OPEN`, `CLOSE_ALL`
- **InstrumentInfoProvider**: Fetches from Bybit API, 24h cache, fallback cascade: fresh cache → API → stale cache → defaults

### Risk Multiplier Composition (CRITICAL)

- GridEngine emits `qty=0` — risk callback must COMPOSE with base `qty_calculator`, not replace it
- **WRONG**: `executor.qty_calculator = risk_callback` (overwrites; `0 * multiplier = 0`)
- **RIGHT**: Save base calculator, compose: `base_qty = base_calc(intent, balance); return base_qty * multiplier`
- Risk recalculation uses `last_price` (market), NOT fill price
- Tests with synthetic `qty=Decimal("0.001")` hide the zero-qty bug — always test with `qty=0`
- Conditional assertions (`if limit_orders["long"]:`) silently pass — use unconditional `assert len(...) > 0`
- Defensive guard: check `self._long_position is not None` before calling `.reset_amount_multiplier()`
- Division-by-zero: when `position_value > 0` but `wallet_balance == 0`, raise `ValueError`
- ALL test fixtures creating `BacktestExecutor` MUST include a `qty_calculator`

### CLI

```bash
uv run python -m backtest.main --config conf/backtest.yaml
uv run python -m backtest.main --config conf/backtest.yaml --start "2025-01-01" --end "2025-01-31"
uv run python -m backtest.main --config conf/backtest.yaml --export results.csv
uv run python -m backtest.main --config conf/backtest.yaml --strict
```

Exit codes: `0` = success, `1` = config error, `2` = execution error

### Metrics & Reporting

- `BacktestMetrics`: trades, PnL, risk (max drawdown, Sharpe), balance, volume, direction breakdown
- Sharpe ratio: equity resampled to fixed intervals (default 1h), annualized 365.25 days
- `BacktestReporter`: CSV exports (trades, equity curve, metrics, all)

---

## comparator — Backtest vs Live Validation

**Path**: `apps/comparator/`

### Key Concepts

- **Trade matching**: Joins on `(client_order_id, occurrence)` composite key (handles deterministic ID reuse)
- **Occurrence**: nth time same client_order_id appears chronologically
- **Live partial fills**: Aggregated by `(order_link_id, order_id)` using VWAP price
- **Direction inference** (live): `closed_pnl != 0` → closing trade. Limitation: break-even closes misclassified
- **For matched pairs**: Prefer backtest direction (always correct) over inferred live direction
- **Tolerance**: `tolerance=0` means exact match (any non-zero delta flagged)

### NormalizedTrade

Fields: `client_order_id`, `symbol`, `side`, `price`, `qty`, `fee`, `realized_pnl`, `timestamp`, `source`, `direction`, `occurrence`. Uses `SideType`/`DirectionType` enums.

### Key Pitfalls

- SQLite strips timezone — compare with `.replace(tzinfo=None)` in tests
- Direction != Side (a Sell can close a long position)
- Use `zip(matched, trade_deltas)` not dict keyed by client_order_id (fails on reuse)
- `breaches` stores `(client_order_id, occurrence)` tuples
- All timestamps normalized via `_normalize_ts()` to naive UTC
- `--symbol` required with `--backtest-config` mode
- `run()` filters backtest_trades by symbol before matching (symmetric filtering)

---

## recorder — Standalone Data Recorder

**Path**: `apps/recorder/`

Records raw Bybit mainnet WebSocket data to SQLite. Reuses `event_saver` collectors + writers directly.

### Key Rules

- Run via: `uv run recorder --config path/to/config.yaml`
- Fixed UUIDs for DB seeding (stable across restarts); new Run per session
- `Strategy.symbol` VARCHAR(20) — store only first symbol; full list in `config_json["symbols"]`
- All WS handlers use `asyncio.run_coroutine_threadsafe()` — every future gets `_log_future_error()` callback
- `SecretStr` for API credentials — access via `.get_secret_value()`
- Defaults to `testnet=False` (mainnet), unlike gridbot
- Config search: `RECORDER_CONFIG_PATH` env → `conf/recorder.yaml` → `recorder.yaml`
- Lifecycle: `self._running = True` at top of `start()` inside try/except; `stop(error=True)` marks run as "error"

---

## replay — Replay Engine

**Path**: `apps/replay/`

Reads recorded data, feeds through GridEngine + simulated order book, compares against real executions.

### Key Rules

- Massive reuse: `HistoricalDataProvider`, `BacktestRunner`, order manager, fill simulator, comparator modules
- Config: root-level `initial_balance`/`enable_funding`/`wind_down_mode` (not nested under strategy)
- Run resolution: auto-discovers latest recording run, or explicit `--run-id`
- Active runs (`end_ts=None`): falls back to `datetime.now(UTC)` instead of failing
- `RunRepository.get_latest_by_type()` has `statuses` filter (default: completed + running)
- `datetime.fromisoformat()` requires Python 3.11+ for full timezone support
- Config search: `--config` → `REPLAY_CONFIG_PATH` env → `conf/replay.yaml` → `replay.yaml`

---

## pnl_checker — Live PnL Validation

**Path**: `apps/pnl_checker/`

Read-only tool comparing our PnL/margin calculations against Bybit exchange values.

### Key Rules

- Use `pos.mark_price` (position endpoint) NOT `ticker.mark_price` for unrealized PnL
- Funding data is informational only (no tolerance check)
- Rate limiting: 10 req/sec (well under Bybit's 50)
- `BYBIT_API_KEY`/`BYBIT_API_SECRET` env vars override YAML config
- `liqPrice` can be empty string — use `Decimal(pos.get("liqPrice", "0") or "0")`
- Initial Margin comparison will show FAIL in hedge mode (expected — Bybit UTA uses mark_price + hedge optimization)

---

## Margin Ratio vs Bybit positionIM — Critical Distinction

**`PositionState.margin` = `positionValue / walletBalance`** (a ratio, e.g., 0.26) — bbu2 pattern.

All risk config thresholds (`max_margin=8`, `min_total_margin=0.15`) are ratios. **Bybit's `positionIM`** is a dollar amount — completely different. Do NOT use `positionIM` as `margin`.

| Consumer | Margin calculation | Correct? |
|----------|-------------------|----------|
| gridbot (live) | `positionValue / walletBalance` | Yes |
| pnl_checker | `positionValue / walletBalance` | Yes (fixed) |
| backtest | `positionValue / walletBalance` | Yes |

---

## Common Pitfalls (Cross-Cutting)

1. **DO NOT** import exchange libraries in gridcore
2. **DO NOT** make network/DB calls in gridcore modules
3. **DO NOT** use raw strings for enums — use `GridSideType`, `DirectionType`, `SideType`, `RunType`
4. **ALWAYS** pass `tick_size` as Decimal to Grid
5. **ALWAYS** run tests before committing (`make test`)
6. **ALWAYS** use `redact_db_url()` when logging database URLs
7. **ALWAYS** use `asyncio.run_coroutine_threadsafe()` for WS thread → event loop routing
8. **Grid rebuild**: `build_greed()` clears grid first — prevents doubling
9. **Duplicate orders**: Deterministic `client_order_id` (SHA256) for dedup
10. **Event dataclasses**: All fields must have defaults (Python inheritance requirement)
11. **CancelIntent**: Use `_cancel_limit()`/`_cancel_all_limits()` helpers, not direct construction
12. **Test anchor/grid state**: Verify against actual grid structure, not just input values
13. **conftest conflicts**: Run test suites per-directory (or use `make test`)
14. **SQLite strips timezone**: Use naive UTC timestamps in test data
15. **Blocking I/O in async**: Wrap with `asyncio.to_thread()` (Python 3.9+)
16. **Dict iteration in async**: Snapshot with `list(d.items())` before iterating
17. **`asyncio.CancelledError`**: Is `BaseException`, passes through `except Exception`
18. **Logging style**: Use `%s`-style in hot-path loops; f-strings elsewhere acceptable
19. **PlaceLimitIntent constructor**: Requires `qty` and `grid_level` positional args

## Dynamic Risk Limit Tiers (Feature/dynamic risk limit tiers)

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

### Pitfalls

1. **`claude-code-action` workflow file must match default branch** — The `.github/workflows/claude-code-review.yml` on a PR branch must be identical to the version on `main`. Modify it on `main` first, then all future PRs pick it up. If you change it on a feature branch, the OIDC token validation fails with "Workflow validation failed."
2. **`_margin_ratio` in calculator.py** — Distinguishes `pos is None` (no position, returns 0 silently) from `wallet_balance <= 0` (logs warning then returns 0). This aids debugging when wallet data is missing.
3. **`grid.py __center_grid` rebalancing** — `lowest_buy_price` must be tracked in the loop (not just initialized from `grid[0]`). After `update_grid` changes sides, grid[0] may be WAIT, not BUY. Fixed 2026-04-11.
4. **f-string division in `runner.py _process_fill`** — Decimal division by `session.current_balance` inside f-strings crashes even when debug logging is disabled. Always guard balance divisions with `> 0` check outside the f-string. Fixed 2026-04-11.
5. **`reconciler.py` public trade reconciliation** — Bybit's `/v5/market/recent-trade` only returns the most recent trades; it does NOT support time-range queries. The reconciler logs a warning when fetched data doesn't cover the gap. Execution reconciliation (`get_executions_all`) correctly passes `start_time`/`end_time`. Fixed 2026-04-11.
6. **`runner.py _execute_intents` stale limits snapshot** — `_execute_intents()` must refresh the `limits` snapshot after each successful `_execute_place_intent()` call. Without this, multiple reduce-only intents in the same batch all check against the same stale snapshot, over-covering the position and causing Bybit 110017 reduce-only rejections. Path: `apps/gridbot/src/gridbot/runner.py`. Fixed 2026-04-11.
7. **Backtest `_should_place_close` must resolve intent qty** — Engine emits `qty=0`; the gate must resolve it via `executor.qty_calculator` before checking `pos_size > (pending + intent_qty)`. Without this, the backtest gate is weaker than live `_is_good_to_place()` and allows over-closing positions. Path: `apps/backtest/src/backtest/runner.py`. Fixed 2026-04-11.
8. **Backtest `_apply_risk_to_qty` must re-round after multiplier** — Base qty is rounded to `qty_step`, but multiplying by the risk multiplier can produce sub-step values (e.g., 0.001 * 0.5 = 0.0005). Must call `instrument_info.round_qty()` after multiplying, matching live `_resolve_qty`. Path: `apps/backtest/src/backtest/runner.py`. Fixed 2026-04-11.

## Phase K: PnL Checker (`apps/pnl_checker/`)

## Reference Code

- Location: `bbu_reference/bbu2-master/`
- Keep for comparison tests; never modify
- **WARNING**: Contains bugs (e.g., short position liq risk logic)

## Docs

Feature documentation lives in `docs/features/` — see `0001_IMPLEMENTATION_SUMMARY.md`, `ORDER_IDENTITY_DESIGN.md`, `0003_FIXES.md`, `0008_PLAN.md`, `0009_PLAN.md`, etc.

### Key Implementation Notes

1. **Mark Price Source**: Use `pos.mark_price` (from position endpoint) NOT `ticker.mark_price` (from ticker endpoint) for unrealized PnL — matches what Bybit uses for `unrealisedPnl` in the same API response.

2. **Funding Data Is Informational**: Funding records from transaction log are display-only (no tolerance check). Attach funding fields to the first position per symbol only (avoid duplication in hedge mode).

3. **Rate Limiting**: `BybitRestClient` integrates `RateLimiter` with 10 req/sec for queries (well under Bybit's 50 req/sec). All API methods call `_wait_for_rate_limit()` before making requests.

4. **Division Guard Constants**: `MIN_POSITION_IM` and `MIN_LEVERAGE` in `calculator.py` prevent division by near-zero values. Warnings are logged when these guards activate.

5. **Environment Variable Credentials**: `BYBIT_API_KEY`/`BYBIT_API_SECRET` env vars override YAML config values via Pydantic `model_validator`. Config file uses `default=""` to allow empty values when env vars are set.

6. **Symbol Validation**: `_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{4,20}$")` in `config.py`. Bybit symbols are uppercase alphanumeric only.

7. **`get_transaction_log_all()` Return Type**: Returns `tuple[list[dict], bool]` — the bool indicates whether data was truncated at `max_pages`. Callers must handle the truncation flag.

8. **Config Redaction**: `_redact_config()` in `reporter.py` replaces API credentials with `[REDACTED]` before writing to JSON output. Never serialize raw `AccountConfig` to files.

### Common Pitfalls (PnL Checker)

1. **`liqPrice` can be empty string**: Bybit returns `""` for liq price when not applicable. Use `Decimal(pos.get("liqPrice", "0") or "0")` — the `or "0"` handles empty string.
2. **Tolerance scaling for percentages**: PnL % values are 100x USDT values. Use `PERCENTAGE_TOLERANCE_MULTIPLIER = 100` in `comparator.py` to scale tolerance for ROE comparisons.
3. **Test coverage**: Currently at 92%. Run: `uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -v`
4. **Workspace dependency**: `pnl-checker` must be in root `pyproject.toml` dev deps AND `tool.uv.sources` for test discovery to work.
5. **Initial Margin comparison (known mismatch)**: Our `calc_initial_margin` uses `positionValue / leverage` (entry-based). Bybit UTA cross-margin uses mark_price and hedge optimization. The IM comparison will show FAIL in hedge mode — this is expected. The comparison exists for visibility, not accuracy validation.

## Dynamic Risk Limit Tiers

### Overview
Risk limit tiers determine maintenance margin (MM) and initial margin (IM) rates based on position size. These tiers are fetched dynamically from Bybit API and cached locally.

### Files Involved
- `packages/gridcore/src/gridcore/pnl.py` — `MMTiers` type, hardcoded fallback tiers (`MM_TIERS_BTCUSDT`, etc.), `parse_risk_limit_tiers()`, `calc_maintenance_margin()`, `calc_initial_margin()`
- `apps/backtest/src/backtest/risk_limit_info.py` — `RiskLimitProvider` orchestrator (fetch, cache, fallback)
- `apps/backtest/src/backtest/cache_lock.py` — In-process and cross-process locking helpers
- `apps/backtest/src/backtest/tier_serialization.py` — MMTiers ↔ JSON dict serialization
- `apps/backtest/src/backtest/cache_validation.py` — Symlink, size, and inode file validation
- `packages/bybit_adapter/src/bybit_adapter/rest_client.py` — `get_risk_limit()` API call (`_unwrap_risk_limit_response` raises `ValueError` on unexpected structure)
- `apps/pnl_checker/src/pnl_checker/calculator.py` — Uses tiers for IM/MM calculation
- `apps/pnl_checker/src/pnl_checker/fetcher.py` — Fetches risk limits per symbol
- `scripts/check_tier_drift.py` — Compares hardcoded tiers against live API (weekly CI via `.github/workflows/risk-tier-monitor.yml`)

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

## Next Steps (Future Phases)

- Phase I: Deployment & Monitoring
