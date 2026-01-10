# Project Rules and Guidelines

## Phase B: Core Library Extraction (gridcore)

### Completed: 2025-12-30

Successfully extracted pure strategy logic from `bbu2-master` into `packages/gridcore/` with zero exchange dependencies.

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
   - File: `packages/gridcore/src/gridcore/grid.py`

3. **Engine Module (`engine.py`)**
   - Extracted from: `bbu_reference/bbu2-master/strat.py` (Strat50 class)
   - Event-driven pattern: `on_event(event) → list[Intent]`
   - **CRITICAL**: Engine NEVER makes network calls or has side effects
   - Returns intents (PlaceLimitIntent, CancelIntent), execution layer handles actual orders
   - File: `packages/gridcore/src/gridcore/engine.py`

4. **Position Risk Management Module (`position.py`)**
   - Extracted from: `bbu_reference/bbu2-master/position.py`
   - Manages position sizing multipliers based on liquidation risk, margin levels, and position ratios
   - **CRITICAL BUG FIX (2026-01-01)**: Reference code had incorrect liquidation risk logic for short positions
     - Reference used `liq_ratio < 0.95 * max_liq_ratio` which is backwards
     - Correct logic: `liq_ratio > 0.95 * max_liq_ratio` (higher ratio = closer to liquidation for shorts)
   - **MISSING LOGIC FIX (2026-01-03)**: Added moderate liquidation risk logic for short positions
     - Original bbu2 code at `position.py:81-86` handles moderate liq risk for shorts
     - This logic was missing from initial gridcore extraction
     - Added in `position.py:220-224` with correct priority ordering
     - When short position has moderate liq risk, decreases short sells to increase long position
   - **Rule Priority Order**: Specific conditions (low margin, position ratio) must be checked BEFORE general liquidation risk
     - This prevents liquidation risk from masking intended position sizing adjustments
     - Order: emergency liq → low margin → position ratio → moderate liq
   - File: `packages/gridcore/src/gridcore/position.py`

5. **Events and Intents**
   - Events (`events.py`): Immutable dataclasses representing market data and order updates
   - Intents (`intents.py`): Immutable dataclasses representing desired actions
   - **PITFALL**: All event dataclass fields that extend Event must have default values (Python dataclass inheritance requirement)
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
   │   └── persistence.py       # Grid anchor persistence
   └── tests/
       ├── test_grid.py         # Grid calculation tests
       ├── test_engine.py       # Engine event processing tests
       ├── test_position.py     # Position risk tests
       ├── test_persistence.py  # Anchor persistence tests
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
# Sync workspace (from project root)
uv sync

# Install gridcore in editable mode
uv pip install -e packages/gridcore
```

### Running Tests

```bash
# Run all gridcore tests with coverage
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v

# Run specific test file
uv run pytest packages/gridcore/tests/test_grid.py -v
```

### Key Files

- `pyproject.toml` (root) - Workspace configuration with dev dependencies
- `uv.lock` - Lockfile for reproducible builds (committed to git)
- `packages/gridcore/pyproject.toml` - Package-specific configuration

## Development Workflow

1. Always follow the CLAUDE.md workflow:
   - Define task clearly
   - Research codebase and RULES.md
   - Create plan and get confirmation
   - Implement with testing
   - Update RULES.md with learnings
   - Verify and commit

2. When working with gridcore:
   - Use `uv run pytest` for running tests
   - Always run tests after changes
   - Verify zero exchange dependencies
   - Check that tick_size is passed as Decimal parameter
   - Ensure event-driven pattern is maintained (no side effects in engine)

## Common Pitfalls to Avoid

1. **DO NOT** import pybit, bybit, or exchange-specific libraries in gridcore
2. **DO NOT** make network calls or database calls in gridcore modules
3. **DO NOT** use BybitApiUsdt.round_price() - use Grid._round_price(tick_size) instead
4. **DO NOT** forget default values in event dataclass fields (Python inheritance requirement)
5. **DO NOT** make GridEngine.on_event() have side effects - it must be pure (except internal state)
6. **DO NOT** blindly copy reference code - verify logic correctness (reference had bugs in position risk management)
7. **ALWAYS** pass tick_size as Decimal parameter to Grid, never look it up from exchange
8. **ALWAYS** run tests before committing changes to gridcore
9. **Grid Rebuild**: `build_greed()` clears `self.greed = []` before building to prevent doubling on rebuilds
10. **Duplicate Orders**: `PlaceLimitIntent.create()` uses deterministic `client_order_id` (SHA256 hash of symbol+side+price+grid_level+direction) so execution layer can detect/skip duplicates
11. **Position Risk Management**: For SHORT positions, higher liquidation ratio means closer to liquidation (use `>` not `<` in conditions)
12. **Risk Rule Priority**: Check specific conditions (low margin, position ratios) BEFORE general liquidation risk conditions

## Grid Anchor Persistence

Grid levels can be preserved across restarts using the `GridAnchorStore` and `anchor_price` parameter.

### Problem Solved
When restarting the app after non-grid-related code changes, the grid normally rebuilds from the current market price, losing the original grid levels.

### How It Works
1. **On startup**: Load saved anchor data for the `strat_id`
2. **Check config match**: If `grid_step` AND `grid_count` match saved values → use saved anchor price
3. **Config changed**: If either changed → rebuild fresh from market price
4. **After grid build**: Save new anchor data

### Usage Pattern
```python
from gridcore import GridEngine, GridConfig, GridAnchorStore
from decimal import Decimal

strat_id = "btcusdt_main"
config = GridConfig(grid_step=0.2, grid_count=50)
store = GridAnchorStore('db/grid_anchor.json')

# Load anchor if config matches
anchor_data = store.load(strat_id)
use_anchor = (
    anchor_data
    and anchor_data['grid_step'] == config.grid_step
    and anchor_data['grid_count'] == config.grid_count
)

engine = GridEngine(
    symbol='BTCUSDT',
    tick_size=Decimal('0.1'),
    config=config,
    strat_id=strat_id,
    anchor_price=anchor_data['anchor_price'] if use_anchor else None
)

# After first grid build (after first ticker event), save anchor:
# (typically in your event loop after grid is built)
store.save(
    strat_id=strat_id,
    anchor_price=engine.get_anchor_price(),
    grid_step=config.grid_step,
    grid_count=config.grid_count
)
```

### Storage Format
File: `db/grid_anchor.json`
```json
{
  "btcusdt_main": {
    "anchor_price": 100000.0,
    "grid_step": 0.2,
    "grid_count": 50
  },
  "ethusdt_main": {
    "anchor_price": 3500.0,
    "grid_step": 0.2,
    "grid_count": 50
  }
}
```

### Key Notes
- `strat_id` identifies each strategy instance (supports multiple currencies/accounts)
- Grid is only rebuilt from anchor if both `grid_step` AND `grid_count` match
- If config changes, grid rebuilds fresh from market price (intentional)
- `GridEngine` now requires `strat_id` parameter

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

## Next Steps (Future Phases)

- Phase E: Live Bot Rewrite (multi-tenant orchestrator)
- Phase F: Backtest Rewrite (trade-through fill model)
- Phase G: Comparator (validation metrics)
- Phase H: Testing & Validation
