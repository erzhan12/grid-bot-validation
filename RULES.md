# Project Rules and Guidelines

## Phase B: Core Library Extraction (gridcore)

### Completed: 2025-12-30

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
# Run all project tests (recommended)
make test

# Run all gridcore tests with coverage
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v

# Run specific test file
uv run pytest packages/gridcore/tests/test_grid.py -v
```

**make test**: Runs pytest separately for gridcore, bybit_adapter, grid_db, event_saver, gridbot (avoids `conftest` ImportPathMismatchError when multiple `tests/conftest.py` exist). Coverage is appended; the final run prints `term-missing` for the combined report. `--cov-fail-under` is not applied to the merged total (~73%); to enforce 80% on one package: `uv run pytest <testpath> --cov=<pkg> --cov-fail-under=80`.

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
10. **Duplicate Orders**: `PlaceLimitIntent.create()` uses deterministic `client_order_id` (SHA256 hash of identity params) so execution layer can detect/skip duplicates
    - **Dynamic Identity Hash (2026-01-23)**: Uses `_IDENTITY_PARAMS` class constant to define which parameters affect order identity
    - **UPDATED (2026-02-06)**: Removed `grid_level` from identity hash - Current identity params: `['symbol', 'side', 'price', 'direction']`
    - **Rationale**: Orders survive grid rebalancing (`center_grid()`) when grid_level changes but price stays same
    - Excluded from hash: `qty` (execution layer determines), `reduce_only` (order flag), `grid_level` (tracking only)
    - **grid_level field preserved**: Still part of dataclass for tracking/reporting/analytics, just not in hash
    - **Safety check added**: `build_grid()` validates no duplicate prices (would violate uniqueness without grid_level in hash)
    - **Maintenance**: When adding new parameters, decide if they affect identity. If yes, add to `_IDENTITY_PARAMS`. If no (like `qty`, `grid_level`), don't add.
    - **Benefit**: No manual f-string construction; adding/removing identity params is a one-line change to the list
    - **Documentation**: See `docs/features/ORDER_IDENTITY_DESIGN.md` for comprehensive design rationale and implementation details
11. **Position Risk Management**: For SHORT positions, higher liquidation ratio means closer to liquidation (use `>` not `<` in conditions)
12. **Two-Position Architecture (CRITICAL)**: Always create BOTH Position objects and link with `set_opposite()`
    - Each trading pair requires two Position objects: one for long, one for short
    - **RECOMMENDED**: Use `Position.create_linked_pair(risk_config)` helper to create properly linked positions
    - **Manual linking**: Call `long.set_opposite(short)` and `short.set_opposite(long)` before using
    - **Validation (2026-01-15)**: `calculate_amount_multiplier()` now validates that opposite is linked and raises `ValueError` if not
    - **DirectionType Enum (2026-01-25)**: Use `DirectionType.LONG`, `DirectionType.SHORT` instead of hardcoded strings `'long'`/`'short'`
      - `DirectionType` is a `StrEnum` (like `GridSideType`) - values are strings, so backward-compatible
      - Imported from `gridcore` or `gridcore.position`
      - `Position.DIRECTION_LONG` and `Position.DIRECTION_SHORT` are aliases for backward compatibility
    - **SideType Enum (2026-02-05)**: Use `SideType.BUY`, `SideType.SELL` instead of hardcoded strings `'Buy'`/`'Sell'`
      - `SideType` is a `StrEnum` - values are strings, so backward-compatible
      - Imported from `gridcore` or `gridcore.position`
      - `Position.SIDE_BUY` and `Position.SIDE_SELL` are now aliases for `SideType.BUY`/`SideType.SELL`
      - **Comparator (2026-02-13)**: `NormalizedTrade` uses `SideType` and `DirectionType` for `side`/`direction`; loaders convert string inputs via `SideType(s)` / `DirectionType(s)`
    - **RunType Enum (2026-02-13)**: Use `RunType.LIVE`, `RunType.BACKTEST`, `RunType.SHADOW` instead of strings
      - `RunType` is a `StrEnum` defined in `grid_db.enums` and exported from `grid_db`
      - Values: `'live'`, `'backtest'`, `'shadow'`
    - Moderate liquidation risk triggers cross-position adjustments (modifying opposite's multipliers)
    - Without linking, the method will fail with a clear error message instead of silently doing nothing
    - Example:
      ```python
      from gridcore import Position, DirectionType

      # RECOMMENDED approach
      long_mgr, short_mgr = Position.create_linked_pair(risk_config)

      # Manual approach (if different configs needed)
      long_mgr = Position(DirectionType.LONG, long_config)
      short_mgr = Position(DirectionType.SHORT, short_config)
      long_mgr.set_opposite(short_mgr)
      short_mgr.set_opposite(long_mgr)
      ```
13. **Risk Rule Priority (CRITICAL)**: ALWAYS check liquidation risk BEFORE position sizing strategies
    - Liquidation = total loss (100%), missed trade opportunity = no loss (0%)
    - Long: High liq → Moderate liq (modifies opposite) → Low margin → Position ratios
    - Short: High liq → Position ratios/margin → Moderate liq (modifies opposite)
    - Test scenarios must have SAFE liquidation ratios when testing position sizing logic
14. **CancelIntent Creation (2026-01-23)**: Use helper methods instead of creating CancelIntent directly
    - **DO**: `intents.append(self._cancel_limit(limit, 'reason'))` for single cancellation
    - **DO**: `intents.extend(self._cancel_all_limits(limits, 'reason'))` for bulk cancellation
    - **DON'T**: Create CancelIntent objects directly in loops (duplicates field extraction logic)
    - File: `packages/gridcore/src/gridcore/engine.py` (see `_cancel_limit`, `_cancel_all_limits`)
15. **Bybit Order Status 'Active' is LEGACY (2026-01-23)**: V5 API does NOT have 'Active' status
    - **Bybit V3 API (deprecated Aug 31, 2024)**: Had 'Active' status for triggered conditional orders
    - **Bybit V5 API (current)**: Valid statuses are 'New', 'PartiallyFilled', 'Filled', 'Cancelled', 'Rejected', 'Untriggered', 'Triggered', 'Deactivated'
    - **bbu2-master issue**: Code checks `['Active', 'New', 'PartiallyFilled']` but uses V5 API (`category='linear'`)
    - **Result**: 'Active' never matches (harmless but confusing migration artifact from V3→V5 upgrade)
    - **gridcore fix**: Only checks actual V5 statuses: `['New', 'PartiallyFilled']` for pending, `['Filled', 'Cancelled', 'Rejected']` for terminal
    - **Reference**: [Bybit V5 Order Status Enums](https://bybit-exchange.github.io/docs/v5/enum), [V3→V5 Migration](https://announcements.bybit.com/article/important-api-update-transition-from-open-api-v3-to-open-api-v5-blt07c25e4e6f734fee/)
    - File: `bbu_reference/bbu2-master/bybit_api_usdt.py:41`, `packages/gridcore/src/gridcore/engine.py:157-160`
16. **PlaceLimitIntent Identity Parameters (2026-01-23)**: When adding new parameters to `PlaceLimitIntent.create()`, update `_IDENTITY_PARAMS`
    - **Rule**: If new parameter affects order uniqueness (like `symbol`, `price`, `grid_level`), add it to `_IDENTITY_PARAMS` class constant
    - **Exception**: Parameters that DON'T affect identity (like `qty`, `reduce_only`) should NOT be added
    - **Why**: `client_order_id` is SHA256 hash of identity params for deduplication; wrong params = broken deduplication
    - **Pattern**: `_IDENTITY_PARAMS = ['symbol', 'side', 'price', 'grid_level', 'direction']` drives dynamic hash generation
    - **Test**: `test_qty_does_not_affect_id` verifies that `qty` is correctly excluded from identity hash
    - File: `packages/gridcore/src/gridcore/intents.py:21`
17. **Testing Grid State (2026-01-23)**: When testing anchor_price or grid state, verify against actual grid structure, not just input values
    - **DON'T**: `assert engine.get_anchor_price() == 100000.0` (only checks input value)
    - **DO**: Extract grid structure, verify anchor matches actual WAIT zone AND grid levels are preserved
    - **Pattern for anchor**: `wait_prices = [g['price'] for g in engine.grid.grid if g['side'] == GridSideType.WAIT]; assert anchor in wait_prices`
    - **Pattern for grid preservation**: `original_grid = [(g['price'], g['side']) for g in engine.grid.grid]; assert restarted_grid == original_grid`
    - **Why**: Grid may round prices, have multiple WAIT zones, or transform input in unexpected ways. Test names claiming to test "grid levels" must verify actual grid structure.
    - **Examples**:
      - `test_get_anchor_price_returns_wait_zone_price` verifies anchor matches actual grid WAIT zone
      - `test_anchor_price_preserves_grid_levels_on_restart` verifies grid structure is identical after restart
    - File: `packages/gridcore/tests/test_engine.py:703-812`
18. **GridSideType Naming (2026-01-23)**: Use `GridSideType` enum, not raw strings
    - **DO**: `g['side'] == GridSideType.WAIT` (type-safe, autocomplete, refactorable)
    - **DON'T**: `g['side'] == 'Wait'` (typo-prone, no IDE support)
    - **Values**: `GridSideType.BUY` ('Buy'), `GridSideType.SELL` ('Sell'), `GridSideType.WAIT` ('Wait')
    - **History**: Renamed from `GridSide` to `GridSideType` for clarity (these are type constants, not directional sides)
    - File: `packages/gridcore/src/gridcore/grid.py:20-24`

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
   - Deterministic `client_order_id` (16-char hex from SHA256) enables deduplication
   - `runner.inject_open_orders()` for startup reconciliation

2. **Position Risk Management**
   - `StrategyRunner` owns linked `Position` pair (long/short)
   - `on_position_update()` calculates `position_ratio` and `amount_multiplier`
   - Periodic position check (default 63s) via `orchestrator._position_check_loop()`

3. **Event Routing**
   - `_symbol_to_runners`: routes ticker events by symbol
   - `_account_to_runners`: routes position/order/execution events by account
   - WebSocket callbacks use `asyncio.run_coroutine_threadsafe()` for thread safety

4. **Shadow Mode**
   - `shadow_mode=True` in strategy config → intents logged but not executed
   - `IntentExecutor` returns shadow order IDs: `shadow_{client_order_id}`
   - Useful for validating strategy behavior before live trading

5. **Reconciliation**
   - **Startup**: fetch open orders, identify "our" orders (16-char hex orderLinkId), inject into runner
   - **Reconnect**: compare exchange state with in-memory, update tracked orders
   - **Orphan detection**: orders on exchange not matching our pattern
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
- `parse_risk_limit_tiers(api_tiers)` — Bybit API response → `MMTiers`

**Consumers:**
- `apps/pnl_checker/src/pnl_checker/calculator.py` — Imports from `gridcore.pnl`
- `apps/backtest/src/backtest/position_tracker.py` — Imports from `gridcore.pnl`
- `packages/gridcore/src/gridcore/position.py` — Has float copy for risk mgmt (cross-referenced)

**Key decisions:**
- `position.py` keeps its float-arithmetic copy (lines 199-202) for risk management performance
- `funding_snapshot` stays in pnl_checker (one-liner, single consumer)
- All functions take Decimal inputs, except `calc_liq_ratio` which returns float
- `calc_initial_margin` is only used for display/comparison (not risk engine)

## Margin Ratio vs Bybit positionIM — Critical Distinction

**`PositionState.margin` = `positionValue / walletBalance`** (a ratio, e.g., 0.26)

This is the bbu2 pattern (bbu2-master/position.py:105). It represents what fraction of wallet this position uses. All risk config thresholds (`max_margin=8`, `min_total_margin=0.15`) are ratios.

**Bybit's `positionIM`** is a dollar amount (e.g., $0.52) — completely different. In UTA hedge mode, Bybit applies margin optimization: the winning side carries full IM, the losing side is reduced to just closing fees. Do NOT use `positionIM` as `margin` in the risk engine.

**Who uses what:**
| Consumer | margin calculation | Status |
|----------|-------------------|--------|
| gridbot (live) | `positionValue / walletBalance` | Correct (runner.py:478) |
| pnl_checker | `positionValue / walletBalance` | Fixed 2026-02-24 (was using positionIM) |
| backtest | Not implemented yet | Does NOT use gridcore Position risk mgmt |

**Backtest gap**: The backtest engine does not integrate gridcore's `Position` risk manager. It uses `BacktestPositionTracker` (PnL only) and `GridEngine` (grid logic only). Risk-based order size multipliers are not applied in backtests.

## Dynamic Risk Limit Tiers (2026-02-26)

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

## Phase K: PnL Checker (`apps/pnl_checker/`)

### Overview
Read-only tool that fetches live Bybit data and compares our PnL/margin calculations against exchange-reported values.

**Config layout (matches other apps)**: Example config lives at `apps/pnl_checker/conf/pnl_checker.yaml.example`. Copy to `apps/pnl_checker/conf/pnl_checker.yaml` for runtime; that path is in `.gitignore`.

**Pipeline**: `fetcher.py` → `calculator.py` → `comparator.py` → `reporter.py`, orchestrated by `main.py`.

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
- `apps/backtest/src/backtest/risk_limit_info.py` — `RiskLimitProvider` class (fetch, cache, fallback)
- `packages/bybit_adapter/src/bybit_adapter/rest_client.py` — `get_risk_limit()` API call
- `apps/pnl_checker/src/pnl_checker/calculator.py` — Uses tiers for IM/MM calculation
- `apps/pnl_checker/src/pnl_checker/fetcher.py` — Fetches risk limits per symbol

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

## Next Steps (Future Phases)

- Phase I: Deployment & Monitoring
