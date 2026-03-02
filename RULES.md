# Project Rules and Guidelines

## Project Overview

Grid trading bot system with pure strategy engine (gridcore), exchange adapter (bybit_adapter), database layer (grid_db), data capture (event_saver), live bot (gridbot), backtest engine, comparator, recorder, replay engine, and PnL checker.

## Package Management (uv)

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

### Position Risk Module (`position.py`)

- **TWO-POSITION ARCHITECTURE**: Each pair has TWO Position objects (long + short), linked via `set_opposite()`
- **RECOMMENDED**: `Position.create_linked_pair(risk_config)` — or manual link with `set_opposite()` both ways
- `calculate_amount_multiplier()` validates opposite is linked, raises `ValueError` if not
- **Priority order**: Liquidation risk FIRST, then position sizing. Liquidation = 100% loss > missed trade = 0% loss
  - Long: High liq → Moderate liq (modifies opposite) → Low margin → Position ratios
  - Short: High liq → Position ratios/margin → Moderate liq (modifies opposite)
- **SHORT position bug**: Reference code had incorrect liq risk logic (`<` instead of `>`). Higher ratio = closer to liquidation for shorts.

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

Exported from gridcore as single source of truth:
- `calc_unrealised_pnl(direction, entry_price, current_price, size)` — Absolute PnL
- `calc_unrealised_pnl_pct(direction, entry_price, current_price, leverage)` — bbu2 ROE %
- `calc_position_value(size, entry_price)` — Notional value (entry-based)
- `calc_initial_margin(position_value, leverage)` — Initial margin
- `calc_liq_ratio(liq_price, current_price)` — Liquidation ratio

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

## Reference Code

- Location: `bbu_reference/bbu2-master/`
- Keep for comparison tests; never modify
- **WARNING**: Contains bugs (e.g., short position liq risk logic)

## Docs

Feature documentation lives in `docs/features/` — see `0001_IMPLEMENTATION_SUMMARY.md`, `ORDER_IDENTITY_DESIGN.md`, `0003_FIXES.md`, `0008_PLAN.md`, `0009_PLAN.md`, etc.
