# Project Rules and Guidelines

## Project Overview

Grid trading bot system with pure strategy engine (gridcore), exchange adapter (bybit_adapter), database layer (grid_db), data capture (event_saver), live bot (gridbot), backtest engine, comparator, recorder, replay engine, and PnL checker.

Successfully extracted pure strategy logic from `bbu2-master` into `packages/gridcore/` with zero exchange dependencies.

**Documentation**: See `docs/features/0001_IMPLEMENTATION_SUMMARY.md` for complete implementation summary and usage examples.

### Legacy bbu2 paths intentionally not ported (gridcore scope)

1. **Legacy bbu2 paths intentionally not ported**

   These bbu2 code paths exist for products we do not target (Bybit
   inverse contracts: BTCUSD, ETHUSD, etc.). gridcore is intentionally
   scoped to Bybit linear USDT-perps. Future audits MUST recognize
   these as legacy carve-outs and not re-flag them as divergences:

   - **`"b..." amount mode`** — bbu2 `bybit_api_usdt.py:509-518`. The
     "b" prefix means "btc-equivalent" with two branches: `BTCUSD` →
     `btc_amount * price` (inverse), non-BTCUSD → `math.ceil(btc_amount
     / price)` (legacy linear non-USDT). Removed from `gridcore/qty.py`
     in Feature 0028. If a config tries to use `b...` it now raises
     `ValueError: invalid amount string`.
   - **`"x" mode currency derivation by symbol`** — bbu2
     `bybit_api_usdt.py:496-501`. bbu2 picks `USDT` if `'USDT' in
     symbol` else `symbol[:3]` (e.g., `BTC` for `BTCUSD`). This handles
     inverse contracts margined in coin. Our `gridcore/qty.py` always
     reads `wallet_balance` as USDT — correct for linear USDT-perps,
     would need a redesign (not a bbu2 port) if USDC or inverse support
     is ever added.
   - **`BTCUSD`-specific branches anywhere in bbu2** — inverse
     contract logic. Out of scope.

   If you ever consider reintroducing inverse / non-USDT support,
   start by re-reading the legacy paths in `bbu_reference/`, not by
   re-porting them blindly: bbu2's `'USDT' in symbol` heuristic does
   not handle USDC pairs (`BTCPERP`, `BTCUSDC`) correctly either.

## Constraints (do not)

Project-specific "what not to do" — pairs with the universal Constraints in `.claude/rules/code-style.md`.

- **Don't edit `bbu_reference/`** — vendored legacy BBU2 bot our code was ported from (its own `ruff.toml`, line-length 140; outside our lint/test scope). Read it to understand original behavior (see "Legacy bbu2 paths" above), but never modify it.
- **Don't hand-edit generated/data dirs** — `data/`, `output/`, `results/`, `db/` are gitignored recorder/replay/backtest artifacts and SQLite DBs, not source. `conf/` holds real tracked config (risk-limit tiers, instrument caches) — leave unless asked.
- **Backward-compat is deliberate, not speculative** — existing compat (`DirectionType`/`SideType` StrEnum aliases, replay `strict_cross` baseline, `extract_client_order_prefix` no-hyphen fallback) is intentional. Don't add new compat shims without a stated reason.
- **No dead config fields** — don't add YAML fields/flags "for later"; e.g. `max_margin` is declared but never read. (Feature 0079 added a real automatic position cap via the **C1 `safety_caps.max_notional_per_symbol`** notional limit — see "Production safety caps"; `max_margin` itself remains dead/unrevived.)
- **Don't point tooling at live state without explicit ask** — the account is Bybit **mainnet** (`mainnet_live`); never run against the live gridbot DB or live orders unless told.

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

**`make test` note**: Runs pytest separately per package to avoid `conftest` ImportPathMismatchError. Coverage is appended; final run prints `term-missing`. `--cov-fail-under` not applied to merged total (~73%). Covers every `pyproject.toml` `testpaths` entry, including `apps/backtest/tests` (added for issue #178 — its prior omission was an oversight with no documented justification).

## Continuous Integration (`.github/workflows/ci.yml`)

The merge gate (feature 0073, issue #176). Two parallel jobs — `test` (`make test`) and `lint` (`make lint`) — run on every PR and on push to `main`; both fail the workflow on any non-zero exit. **CI green is the source of truth for repo health.**

Phase-0 rollout while the repo is red:
- **Step A (current)** — real failing jobs, no `continue-on-error`; the check is NOT required in branch protection, so it doesn't block the fix PRs for #177–#180.
- **Step A′** — advisory green via `continue-on-error: true` on `lint` ONLY (never `test`); opt-in scaffolding, avoid unless explicitly requested.
- **Step B** — after #177–#180 land and both jobs are green, drop any `continue-on-error` and mark the check required.

**Branch protection (making the check "required") is a GitHub repo setting, not a file** — it cannot be enforced from `ci.yml`; set it manually.

### Lint / Ruff (Feature 0078, issue #181)

`make lint` = `uv run ruff check .`. Root config lives in `pyproject.toml` `[tool.ruff]` (ruff defaults; only an additive `exclude` list). Excluded trees, each with a rationale comment in the config:
- `apps/backtest/debug_walkthrough.py` — interactive debug walkthrough; not app code.
- `bbu_reference` — vendored bbu2 reference; not maintained, has its own nested `ruff.toml` (line-length 140).
- `scripts` — one-off research/migration tooling; disposable, not imported by apps.

Maintained code is NOT excluded. When a maintained file has an intentional lint error, prefer a targeted `# noqa: <code>` over excluding it — e.g. `tests/integration/conftest.py:16` carries `# noqa: E402` on the `gridcore.config` import that must follow the `sys.path.insert` block.

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
- **Feature 0048 (bbu2 parity)**: no per-tick grid walk on ticker events. Drift is handled by `update_grid` post-fill (`last_filled_price` keys WAIT via `_assign_sides`) and bounds-guard `build_grid` on the ticker path (`engine.py` out-of-bounds check). `_assign_sides(last_close, *, fill_price)` requires `fill_price` — no `last_close`-based WAIT path. `anchor_price` tracks build/restore center only; use `wait_center()` for live WAIT-band center.

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
- **Unknown market price**: REST/WS position updates can arrive before the first ticker. Pass `last_close=None` (or a queued ticker price if available), never `0.0`; `StrategyRunner.on_position_update()` updates wallet/position sizes but skips risk multiplier recalculation until a real positive price exists.
- **`increase_same_position_on_low_margin` (feature 0040)**: YAML-wired in gridbot via `StrategyConfig` → `RiskConfig` in `apps/gridbot/src/gridbot/runner.py`. Gates `Position._adjust_position_for_low_margin` (open-interval `0.94 < position_ratio < 1.05` AND `total_margin < min_total_margin`): `True` → boost own side `×2`; `False` (default) → suppress opposite side `×0.5`. Continuous boost (not one-shot) while the guard condition holds. Since feature 0071 `apps/backtest` + `apps/replay` also wire the flag through to `RiskConfig` (`apps/backtest/src/backtest/runner.py` RiskConfig call; `apps/replay/src/replay/engine.py` pass-through). **Sole remaining divergence**: `apps/pnl_checker/src/pnl_checker/main.py` still constructs `RiskConfig` with the 4-arg pattern (no flag) — intentional, it is a PnL-attribution tool that never runs the position rule engine's low-margin branch. See `docs/features/0071_PLAN.md` "Out of scope".
- **Replay risk-mgmt tunables (feature 0071, issue #162)**: `ReplayStrategyConfig` exposes `min_liq_ratio`, `max_liq_ratio`, `min_total_margin`, `increase_same_position_on_low_margin`, `leverage`, passed through to `BacktestStrategyConfig` in `apps/replay/src/replay/engine.py`. Defaults match `BacktestStrategyConfig` (0.8 / 1.2 / 0.15 / false / 10) — NOT live values; populate ALL five in the replay YAML to mirror live risk-mgmt. Live values are operator-supplied (private gitignored config), not repo-derived — e.g. `min_total_margin` 3 (LTC) / 2.5 (SOL) vs default 0.15, a ~20x gap that otherwise silences the low-margin branch in replay.

### Pre-placement Validation (`_is_good_to_place`)

- **Reference**: `bbu_reference/bbu2-master/bybit_api_usdt.py:295-313`
- **Purpose**: Prevents placing reduce-only close orders when total reduce-only qty on the book would exceed position size. Without this, Bybit rejects with error 110017 ("orderQty will be truncated to zero") and the retry queue keeps retrying.
- **Logic**: Open orders always pass. For reduce-only orders: sum all placed reduce-only orders for that direction + new order qty, reject if `position_size <= total_reduce_qty` (strict `>`).
- **Location**: `StrategyRunner._is_good_to_place(intent, limits)` in `apps/gridbot/src/gridbot/runner.py`, called from `_execute_place_intent()` after qty resolution. Accepts an explicit `limits` dict (same format as `get_limit_orders()`) so the data source is injectable — live can pass exchange data, backtest can pass simulated data.
- **Position size source**: `Position.size` attribute set in `on_position_update()`. Defaults to `Decimal('0')` until first `on_position_update()` call, which safely rejects reduce-only orders during startup.
- **Decimal conversion safety**: Always use `Decimal(str(value))` — never bare `Decimal(value)` — when converting order dict fields (`price`, `qty`) or any variable that might be a float. `Decimal(0.5)` produces `0.500000000000000027...` which silently breaks equality checks. The `Decimal(str(...))` pattern is safe for strings, floats, and Decimals alike.
- **Zero-size rejection is intentional, not a bug**: When `position_size == Decimal('0')` the reduce-only order is silently rejected (debug log only). This is bbu2-faithful — bbu2 expresses the same behavior implicitly via `position_size > limits_qty` arithmetic. A race can occur when the engine emits a close intent in the sub-tick window after a fill but before the position update lands; it self-heals on the next tick because the engine re-emits the same reduce-only intent every tick from scratch. **Do NOT "allow through on staleness"** — that would place orders against known-stale state and make things worse. If the position feed itself dies, fix it in the position-update path (heartbeat, REST reconcile), not here. See `runner.py:748-753`.

### 110017 retry-storm self-heal + circuit-breaker (feature 0064, issue #149)

The guard above is logically correct but trusts a **stale mirror**: during a WS
outage `_long_position.size`/`_short_position.size` stay stale-high, so the strict
`>` check passes and Bybit clamps the oversized reduce-only to zero → ErrCode
**110017**. The engine re-emits that same close on (nearly) every `on_ticker`, so
one divergence produced **535 identical rejected placements in <90min** (no
circuit-breaker, retried via the queue each time). Two complementary mechanisms
fix this; **do not collapse them into one**:

1. **Dirty-mirror REST refresh BEFORE the guard (primary self-heal).** The first
   110017 on a direction sets `_position_dirty[direction]=True`. On the *next*
   reduce-only placement for that direction, `_refresh_position_size_from_rest`
   REST-reads the true size into the mirror **before** `_is_good_to_place`, so the
   *unchanged* guard rejects the oversized close locally (no submit, no second
   110017). The storm collapses to one 110017. **The guard body is NOT modified
   and there is NO qty-cap** — capping after a strict-`>` guard reading the same
   source is a no-op; refreshing the source is the fix. Hedge-mode
   reject-when-`position ≤ qty` convention is preserved (project memory).
2. **`TruncateBreaker` (backstop).** Scope key `(side, price)` — NOT orderLinkId
   (it carries a per-placement `-{millis}` suffix and never accumulates). After
   N (default 3) 110017s within the window it trips: `is_blocked` drops further
   intents for a cooldown, fires ONE forced reconcile, increments
   `truncate_breaker_reconcile_count`. Bounds the undetected-divergence window and
   residual races when the refresh can't heal (`dirty_refresh_enabled=False` /
   `rest_client=None`).

Pitfalls / invariants (all enforced + tested):
- **Pipeline order in `_execute_place_intent` is load-bearing** (`runner.py`): resolve
  qty → breaker `is_blocked` **first** (a tripped scope must not trigger a REST
  refresh) → dirty refresh (gated by `dirty_refresh_enabled` as the **first**
  term so it's a true kill-switch) → guard → submit → breaker bookkeeping.
- **110017 is excluded from the retry queue** and **drops wire-`order_link_id`
  reuse** (forces a fresh id next emission via `replace(order_link_id=None)`).
  Reusing the id could surface as 110072 — which the breaker doesn't count and the
  queue doesn't exclude — partially bypassing the backstop. Non-110017 failures
  keep feature-0032 reuse + queue enqueue. Classifier is module-level
  `executor.is_truncate_error()` (NOT a method — a `Mock(spec=IntentExecutor)`
  auto-creates a truthy method and would misclassify every failure).
- **Throttle uses a `None` sentinel** (`_last_dirty_rest_at`), not `0.0`: the first
  dirty refresh always fires regardless of clock value (an init of `0.0` only
  worked because real `time.monotonic()` is large; brittle under fake clocks).
  Clock is injectable (`clock=` ctor arg) for tests.
- **WS size write in `on_position_update` is gated while dirty** (only `.size`,
  not ratio/liq/multipliers): exact WS==last-REST match clears dirty; a non-match
  keeps the REST value authoritative (a stale WS frame must not reopen the storm);
  no REST baseline yet (`_last_rest_position_size is None`) → WS passes through
  normally (never restore a synthetic `0`, which would reject all closes when
  refresh is disabled).
- **Dirty clears only on a positive health signal**: a successful **reduce-only
  close** (NOT an open — an open never exercises the position-size guard, so
  clearing dirty on it would re-arm a 110017 on the next close), a forced
  reconcile (`force=True`), or a WS-size match — or process restart. The 10s
  throttle bounds REST while it stays dirty.
- **Episode-scoped state invariant (`_clear_dirty`)**: `_position_dirty[d]`,
  `_last_dirty_rest_at[d]`, and `_last_rest_position_size[d]` are reset *together*
  on every dirty-clear path. Throttle + baseline are meaningful ONLY while dirty
  is True, so a fresh episode always refreshes on its first placement and never
  consults a prior episode's stale baseline. The REST refresh **arms the throttle
  on every attempt (success OR failure)** — else a persistently failing
  `get_positions` / `rest_client=None` re-fires every tick (the `None` sentinel
  never advances).
- **Forced reconcile** (`Orchestrator._force_reconcile_strat`) = `reconcile_reconnect`
  (orders) **+** `_refresh_position_size_from_rest(force=True)` (position size —
  the piece `reconcile_reconnect` does NOT do); rate-limited per
  `truncate_breaker_cooldown_seconds` per strat. The two run in **independent
  `try/except` blocks** so an order-reconcile failure never skips the position
  resync — the position resync is the #149-critical healing step (closes the
  stale-mirror gap). Designed to be reused by the broader divergence detector
  (issue #151).
- **Observability (review v3)**: `dirty_rest_refresh_failure_count` (monotonic
  property; incremented when a dirty REST refresh's `get_positions` raises or
  returns an unparseable size) is surfaced by the health sweep alongside the
  breaker trip count — a persistent REST outage that blocks self-heal is visible
  without per-occurrence ERROR spam. `_dirty_ws_mismatch_streak[direction]`
  counts consecutive WS size mismatches while dirty (reset on match / episode
  clear) and emits a WARNING every `_DIRTY_WS_MISMATCH_ALERT_THRESHOLD` (10)
  mismatches — a WS feed stuck beyond the normal recovery window.
- Config (all on `StrategyConfig`, default-on): `dirty_refresh_enabled`,
  `dirty_rest_refresh_min_interval_seconds`, `truncate_breaker_{max_consecutive,
  window_seconds,cooldown_seconds,reconcile}`. Constant
  `bybit_adapter.error_codes.ORDER_QTY_TRUNCATED_TO_ZERO = 110017`.
- Files: new `apps/gridbot/src/gridbot/truncate_breaker.py`,
  `packages/bybit_adapter/src/bybit_adapter/error_codes.py`; touched
  `runner.py`, `orchestrator.py`, `executor.py`, `config.py`. Tests:
  `test_truncate_breaker.py`, `test_runner_truncate_storm.py`,
  `TestForcedReconcile` in `test_orchestrator.py`.

### Auto state-divergence detector (feature 0069, issue #151)

Closes the gap the 2026-05-30 incident exposed: after ~5h of WS instability the
local mirror diverged far enough that the bot could not self-recover until a
manual restart re-ran the cold-start reconciler. WS reconnects fired but did NOT
force a full position+order re-sync. This detector watches FOUR observable signals
and, on any fire, triggers a **forced full reconcile WITHOUT restarting** — the
same `Orchestrator._force_reconcile_strat` the 0064 breaker uses.

- **Reuses `_force_reconcile_strat`**, now `(strat_id, direction: str|None,
  emit_breaker_warning=True) -> bool`. `direction=None` (every detector signal)
  does ONE rate-limit check, ONE `reconcile_reconnect`, then refreshes BOTH LONG
  and SHORT mirrors — handled INTERNALLY so the per-strat rate-limit timestamp is
  set once (two back-to-back calls would rate-limit the second side and leave a
  hedge-mode mirror half stale). Returns True only when a reconcile actually ran
  (False = rate-limited / no runner). The breaker-trip caller still passes a
  specific direction + the default `emit_breaker_warning=True` → byte-for-byte
  unchanged.
- **Two SEPARATE throttles.** The detector throttle
  (`_divergence_last_fire_at`, `divergence_reconcile_min_interval_seconds`,
  default **300s**) is distinct from the breaker cooldown
  (`_force_reconcile_last_at`, `truncate_breaker_cooldown_seconds`, default 60s).
  The wrapper `_trigger_divergence_reconcile` checks its own throttle, then calls
  `_force_reconcile_strat(direction=None, emit_breaker_warning=False)` and branches
  on the bool: only on True does it emit the single
  `state-divergence detected (signal=…, evidence=…), forcing full reconcile`
  WARNING, clear the runner dedup cache (`clear_dedup_cache()`), and bump the
  detector throttle. On False (suppressed by the breaker cooldown) it emits a
  DEBUG line and does NONE of those — so the analyzer's `force_reconcile_fired`
  never overstates real reconciles and the dedup cache is never evicted without a
  resync. Passing `emit_breaker_warning=False` suppresses the breaker line (and
  its `'None'` direction text) on the detector path, so each reconcile matches
  EXACTLY ONE analyzer pattern (no double-count).
- **Master kill-switch `divergence_detector_enabled`** (default True) is enforced
  BOTH at the wrapper entry (catch-all) AND at each signal's upstream work, so the
  detector is fully inert (no signal, no extra REST) when off.
- **Signal 1 — placement-failure UNION.** `runner._record_placement_failure(error)`
  (called from BOTH `_execute_place_intent` failure exits) appends to a rolling
  `_placement_failure_window` (deque, stamped/evicted via the injectable
  `self._clock()`) when the error is in the UNION {110017, 110072, network};
  **110007 is EXCLUDED** (intentional low-balance drop). At
  `divergence_failure_mix_threshold` (10) within
  `divergence_failure_mix_window_seconds` (60) it CLEARS the window and fires the
  `on_divergence_failure_mix` callback — "a fire" = threshold-reached, regardless
  of whether the downstream reconcile is then suppressed (so a cooldown-suppressed
  fire does not leave the window full and re-trigger on every later failure). Two
  new classifiers in `executor.py`: `is_network_error` (narrow lowercased tokens:
  `timeout`/`connection`/`temporarily unavailable`/`readtimeout`) and
  `is_duplicate_link_error` (110072 / "OrderLinkedID is duplicate").
- **Signal 2 — retry-budget edge.** In `_health_check_once`, fire once per NEW
  edge when `truncate_breaker_reconcile_count >= divergence_retry_budget` (5) AND
  the count differs from `_divergence_budget_last_fired[strat]`. Backstop for when
  the breaker counts but does not auto-reconcile. **Pitfall:** only bump
  `_divergence_budget_last_fired` when `_trigger_divergence_reconcile` returns
  `True` (reconcile actually ran). Consuming the edge before a suppressed reconcile
  (breaker cooldown / detector throttle) leaves the bot stuck with a parked count
  and no further signal-2 retries until trips advances again.
- **Signal 3 — REST-vs-local size delta.** `_divergence_size_check_once` (gated by
  `_next_divergence_size_check`, primed half an interval ahead of `_next_order_sync`
  so the two REST sweeps don't co-fire) compares `runner.rest_position_size(dir)`
  (a NEW **pure** REST read — no mirror mutation, no throttle write, no failure-
  counter bump, unlike `_refresh_position_size_from_rest`) to the local mirror.
  Evaluates BOTH directions, fires ONCE with `direction=None` if EITHER exceeds
  `qty_step * divergence_size_delta_qty_step_multiplier` (5). A `None` REST read
  skips that direction (no fire). `divergence_size_check_interval_seconds` carries
  `gt=0` (cannot be disabled) so position size ALWAYS has a periodic backstop.
- **Signal 4 — post-WS-recovery.** A PRIVATE-channel gap/reset (heartbeat
  `_on_ws_disconnect kind=="private"`, or the private reconnect branches of
  `_health_check_once` / `_ws_health_check_once`) fans out account→strats via
  `_account_to_runners` into `_pending_post_recovery_reconcile` (a `set[str]`
  guarded by `_pending_post_recovery_lock` — `_on_ws_disconnect` runs on the WS
  heartbeat thread). Public-only gaps do NOT enqueue. The set is drained
  swap-and-clear ONCE per `_tick` at a PINNED point: after the per-tick WS event
  drains (so no concurrent reader of a half-cleared dedup cache) and IMMEDIATELY
  before the order-sync gate (sharing the tick's `now`). A throttle/cooldown-
  suppressed strat is DROPPED, not retried (no 10Hz busy-loop); when it would be
  throttle-suppressed AND `order_sync_interval > 0`, the drain peeks the throttle
  and sets `_next_order_sync = 0.0` so the order-sync gate runs THIS tick
  (fast-track), shrinking the order-level backstop from up to `order_sync_interval`
  to the same tick.
- Config (all on `StrategyConfig`, default-on): `divergence_detector_enabled`,
  `divergence_failure_mix_threshold` (ge=1), `divergence_failure_mix_window_seconds`
  (gt=0), `divergence_retry_budget` (ge=1), `divergence_size_check_interval_seconds`
  (gt=0), `divergence_size_delta_qty_step_multiplier` (gt=0),
  `divergence_reconcile_min_interval_seconds` (gt=0). Constant
  `bybit_adapter.error_codes.ORDER_LINK_ID_DUPLICATE = 110072`.
- **Analyzer**: `force_reconcile_fired` is a SINGLE merged `event_coverage` key
  covering BOTH origins (detector WARNING + 0064 breaker line) — do NOT split.
- Files touched: `runner.py`, `orchestrator.py`, `executor.py`, `config.py`,
  `error_codes.py`, `.claude/skills/gridbot-health/analyze.py`. Tests:
  `test_runner_divergence.py`, `test_orchestrator_divergence.py`,
  classifier tests in `test_executor.py`, config tests in `test_config.py`.

### 110007 low-balance preflight + chase-close (feature 0066, issue #159)

Defends against the **110007 "available balance not enough"** retry storm that
hit under a low-balance + long-heavy state (the risk-mgmt rule grew the losing
short with mult=2.0; the grown open exceeded free margin → 110007 every attempt
→ retry-queue amplification). Three layers + a bug-fix, all additive:

- **Margin observability (default-on, no behavior change).**
  `position_fetcher.WalletSnapshot` carries `available_balance` /
  `total_available_balance` / `total_maintenance_margin` (extracted from the
  same `get_wallet_balance()` REST response — **no extra API call**; the
  `_wallet_cache` now holds the snapshot, `get_wallet_balance` reads
  `.wallet_balance`). `PositionState` gains `initial_margin`/`maintenance_margin`
  (Bybit `positionIM`/`positionMM`, **dollar amounts** — kept SEPARATE from the
  `margin` ratio, see "Margin Ratio vs Bybit positionIM — Critical Distinction").
  The per-tick `Position
  update` INFO heartbeat is **extended** (not changed) with `avail=` /
  `total_avail=` / `total_mm=`; the gridbot-health analyzer parses those and
  tracks a `min_available_balance` all-time peak.
  - **UTA empty-string trap**: Bybit mainnet sends `""` for unused numeric
    fields (`availableToWithdraw` on cross-margin). Parse with
    `position_fetcher._float_or_zero` (None/`""` → 0.0); `.get(k, 0)` only
    handles *missing* keys.
  - **`available_balance` fallback chain** (`_snapshot_from_wallet_account_row`):
    prefer the UTA-v5 per-coin `availableToWithdraw`; when it is absent/empty,
    fall back to the **legacy `availableBalance`** coin field (UTA 1.0 / some
    cross-margin coins surface free margin only there — **must mirror
    `recorder.py:404-408`** so the two parsers can't drift); then fall back to the
    account-level `total_available_balance`. Missing this legacy field would let a
    funded account parse free margin as 0 → on the provider path a fresh 0 blocks
    ALL opens (halts trading), on the no-provider path it fail-opens.
- **Preflight balance check (default-on, the storm-stopper).** In
  `_is_good_to_place`, BEFORE the `if not intent.reduce_only: return True`
  early-return: for OPEN orders only, reject locally when
  `available_balance < (qty*price/leverage) * (1 + buffer)`. **Reduce-only
  always bypasses** (frees margin, can't 110007). **Fail-open** when
  `available_balance <= 0` (no data yet) — never block all opens on a transient
  gap. Leverage via `_effective_leverage(direction)`: live per-direction
  leverage (captured in `_build_position_state` into `self._leverage`, kept OUT
  of `PositionState.leverage` so the risk-multiplier upnl calc + backtest parity
  are untouched) else `assumed_leverage`. Bias leverage LOW — under-estimating
  only over-rejects affordable opens, never lets an unaffordable one through.
- **Retry-queue 110007 guard (default-on).** In `_execute_place_intent`, a
  110007 on an open order is **dropped, not enqueued** (mirrors the 0064 "do NOT
  enqueue 110017" decision). It is **stateless — no breaker, no cooldown**; the
  preflight re-gates on the next tick once balance recovers. `INSUFFICIENT_BALANCE
  = 110007` + `executor.is_insufficient_balance()` (sibling of
  `is_truncate_error`).
- **moderate_liq_risk bug-fix (3a, default-on).** Under low-balance the
  `moderate_liq_risk` arm SKIPS the 0.5 close-throttle (`position.py`, both long
  and short branches) — that throttle slowed the margin-freeing closes and
  deadlocked the bot. `low_balance` is threaded into `calculate_amount_multiplier`
  + the rule helpers (default False = byte-for-byte pre-0066). The kill-switch
  (`moderate_liq_low_balance_fix_enabled`) is applied in the RUNNER (it passes
  `low_balance=False` when off) because `position.py` is gridcore and cannot read
  gridbot config. This deviates from the bbu2-derived rule ONLY under the new
  low-balance condition.
- **Chase-close (3b, default-OFF).** When low-balance AND `position_ratio`
  extreme, cancel resting grow-side opens for the dominant side and place a
  reduce-only **post-only** close near the touch to trim it without slippage.
  Post-only support is new and additive: `PlaceLimitIntent.post_only`
  (`compare=False`, NOT in `_IDENTITY_PARAMS` → id/dedup unchanged), threaded
  via `executor.execute_place` → `rest_client.place_order(time_in_force=...)`
  (default `"GTC"` == today's implicit behavior).
  - **Dispatch — stash-and-drain**: `on_position_update` holds no `limits`
    snapshot and never dispatches, so the chase decision only mutates state +
    appends to `self._pending_chase_intents`; `_drain_pending_chase_intents()`
    at the top of `on_ticker`/`on_execution`/`on_order_update` feeds the buffer
    through the existing `_execute_intents(..., get_limit_orders())`.
  - State machine `IDLE→CHASING→IDLE`. Maker-safe pricing: **Sell above / Buy
    below** the touch (post-only never crosses — note this is the OPPOSITE of an
    aggressive taker peg). Chase qty is half the dominant size (rounded), kept
    strictly below position size so the reduce-only guard passes. Cancel-replace
    on drift acts on the chase order ONLY — explicitly exempt from the
    forward-only `amount_multiplier` invariant (it is not a grid order). Exit
    has hysteresis (`chase_close_hysteresis`) to avoid flapping.
- **Single source of truth**: `_is_low_balance(total_position_value)` (avail > 0
  AND avail < total_position_value * `low_balance_fraction`), recomputed each
  `on_position_update` into `self._low_balance`; 3a and 3b each apply their own
  kill-switch on top.
- **Real-time wallet feed (Phase 4, default-on).** The preflight's free-margin
  signal would otherwise lag up to `wallet_cache_interval` (300s, REST-cached),
  so the bot subscribes the private WS **`wallet`** topic (account-level free
  margin — `totalAvailableBalance` / per-coin `availableToWithdraw` — is NOT in
  the `position` topic, which carries only per-position `positionIM`/`positionMM`).
  `ws_client` already supports `on_wallet`; orchestrator just wires it.
  - `position_fetcher._wallet_ws_data[acct] = (snapshot, ts)` is written ONLY by
    `on_wallet_message` on the **WS thread** (single GIL-atomic assignment, no
    lock, mirrors `_position_ws_data`); it NEVER touches `_wallet_cache` (the
    main-thread-guarded REST cache). `on_wallet_message` **never raises on the WS
    thread** and validates **structurally** (needs `data[0]` + a USDT coin row) —
    a valid row is written **even when `available_balance==0`** (a funded account
    with no free margin is a REAL signal; do NOT use an all-zero skip heuristic).
  - **Shared parser** `_snapshot_from_wallet_account_row(row)` (returns `None` on
    no-USDT) is called by BOTH `_fetch_wallet_snapshot` (REST `list[0]`) and
    `on_wallet_message` (WS `data[0]`) — same V5 shape, one parser, no field drift.
  - **Hot path uses `peek_wallet_snapshot(acct) -> (snapshot, age) | None`**:
    non-blocking, NEVER fetches, returns the **newest of {WS slot, REST cache} by
    timestamp** (a stale WS slot must NOT shadow a fresher REST entry). The
    background `get_wallet_snapshot` keeps its WS-if-fresh-else-REST(-fetch) form;
    only `peek` feeds the preflight (injected as the runner `wallet_provider`).
  - **Preflight balance source = freshness, not positivity.** Provider wired: a
    FRESH peek (`age < wallet_ws_max_age_seconds`) is **authoritative even at 0**
    (fresh-zero blocks every open); a stale / `None` / **raising** peek →
    **fail-open** (never falls back to the equally-stale `_available_balance`
    latch; a raising provider is caught + throttled-WARNING and never aborts
    dispatch). No provider (kill-switch off / unit tests) → legacy
    `_available_balance` path with the `>0`-means-data rule. Logic lives in
    `runner._preflight_available_balance` / `_preflight_blocks_open`.
  - **`wallet_ws_enabled=False` is a FULL kill-switch**: skips BOTH the `on_wallet`
    wiring AND the `wallet_provider` injection → genuine pre-Phase-4 behavior
    (position-cadence balance, REST TTL, no age-bounding, no fail-open-on-stale).
    Not "WS off but REST still age-bounded".
  - Caveat: staleness is **bounded to `wallet_ws_max_age_seconds`, then
    fail-open** — NOT "eliminated". The INFO `avail=` heartbeat is written on the
    position-drain cadence and is NOT a reliable WS-freshness signal; validate via
    the **DEBUG WS-vs-REST source/age log** emitted at every `get_wallet_snapshot`
    return path (`served from WS (age=…)` / `served from REST …`).
  - **Predicate freshness (review F1):** the low-balance PREDICATE
    (`_is_low_balance`, used by 3a moderate_liq + 3b chase) reads the SAME
    `wallet_provider` peek as the preflight via `_predicate_available_balance`
    (fresh peek authoritative — a fresh 0 = real no-free-margin = low-balance;
    stale/None/raising → falls back to the position-cadence `_available_balance`
    latch, where a 0 still means "no data"). So chase and preflight now share one
    freshness — this resolves the plan rollout §3 chase-enable prerequisite.
    Additive: no provider (backtest / `wallet_ws_enabled=False`) keeps the exact
    pre-Phase-4 latch behavior.
- Config — **`GridbotConfig`** (Phase 4, not `StrategyConfig`):
  `wallet_ws_enabled` (True), `wallet_ws_max_age_seconds` (45.0, `gt=0`).
- Config (`StrategyConfig`, all default preserve behavior):
  `preflight_balance_check_enabled` (True), `preflight_balance_buffer` (0.05),
  `assumed_leverage` (1.0), `low_balance_fraction` (0.10),
  `moderate_liq_low_balance_fix_enabled` (True), `chase_close_enabled` (**False**),
  `chase_position_ratio_threshold` (5.0), `chase_offset_pct` (0.0007),
  `chase_replace_drift_pct` (0.0010), `chase_close_hysteresis` (0.1).
- Files touched: `position.py`, `intents.py`, `error_codes.py`,
  `rest_client.py`, `runner.py`, `position_fetcher.py`, `orchestrator.py`,
  `executor.py`, `config.py`, gridbot-health `analyze.py`. Tests:
  `test_runner_lowbalance_storm.py`, `test_position_low_balance.py`, plus 3b.0
  surface in `test_intents.py` / `test_executor.py` / `test_rest_client.py`; Phase-4
  WS-wallet/peek/provider surface in `test_position_fetcher.py` /
  `test_runner_lowbalance_storm.py` / `test_orchestrator.py`.
  Promote `chase_close_enabled` to on only after one live low-balance stress
  event confirms the dominant side decreases without market-order slippage.

#### LowBalanceSkip log-spam suppression (feature 0067, issue #164)

- The per-intent `LowBalanceSkip` DEBUG (one line per blocked open per tick →
  ~2.1M/day under a sustained storm) is **suppressed by default** and replaced
  by ENTER/EXIT INFO edges per `(direction, side)` + a 60s INFO summary. Both
  default-on and kill-switchable; with **both flags False** the preflight emits
  the per-intent DEBUG line exactly as before (byte-for-byte). The accept/reject
  DECISION is never changed — only what is logged.
- **Edges resolve at the SAMPLE boundary, never inline per intent.**
  `_preflight_blocks_open` (Part A) only records per-sample scratch
  (`_skip_tick_seen`, keyed by `(direction, side)`); `_reconcile_skip_edges`
  (Part B) emits ENTER/EXIT. Part B runs as the **first statement of
  `_drain_pending_chase_intents`, BEFORE its `if not self._pending_chase_intents:
  return` guard** (the guard returns on the common chase-disabled path, so a call
  after it would never fire during a storm). So the reconcile of dispatch N's
  scratch happens at the top of dispatch N+1 (one evaluated-sample latency).
  Pitfall: a test that calls `_preflight_blocks_open` alone sees NO edge — drive
  `preflight → _reconcile_skip_edges()` per sample, and ≥1 test must drive the
  real `on_ticker`/`on_order_update` handlers (cross-dispatch timing).
- **Scratch semantics (load-bearing):** absence of a key from `_skip_tick_seen`
  means "no fresh evidence this sample", NOT "affordable". A fail-open
  (`_preflight_available_balance` → None) is evidence-neutral: no scratch write,
  no window increment, no state mutation — so a stale-WS blip mid-storm produces
  no churn and doesn't discard a sibling key's genuine skip. EXIT requires the key
  to be present with `blocked=False` (evaluated-fresh-and-affordable). `blocked`
  is sticky-True within a sample → no intra-tick EXIT/ENTER flutter.
- **Scratch write is gated on `transition_logs_enabled`; the scratch clear (Part
  B step 3) is UNCONDITIONAL** — a `True→False→True` runtime flag flip must not
  strand stale evidence and replay it on re-enable. The `_skip_window` summary
  counter is incremented unconditionally on every genuine skip (independent of
  the transition flag) so the summary works even with edge logging off.
- **Idle-timeout sweep** (`low_balance_skip_exit_idle_seconds`, default 60s, 0
  disables): EXITs an `active` key that was removed from the grid mid-storm with
  no recovery EXIT. Sweeps ALL active keys, not just scratch keys; safe because a
  live storm re-blocks every ~100ms so `last_blocked_clock` refreshes and it never
  fires mid-storm — only after a sustained block-absence. A re-block then re-ENTERs
  fresh (count reset).
- Config (`StrategyConfig`, all default-on / preserve behavior):
  `low_balance_skip_transition_logs_enabled` (True),
  `low_balance_skip_exit_idle_seconds` (60, `ge=0`),
  `low_balance_skip_summary_enabled` (True),
  `low_balance_skip_summary_interval_sec` (60, `gt=0`).
- Files touched: `runner.py` (state in `__init__`, Part A in
  `_preflight_blocks_open`, `_reconcile_skip_edges` + `_emit_skip_summary`, drain
  hook), `config.py`. Tests: `test_runner_lowbalance_storm.py` (17 new, keyed
  `test_low_balance_skip_*`). Record floats (`avail_min/max`,
  `first_blocked_price`) are `float`; affordability comparison stays `Decimal`.

### Production safety caps (feature 0079, issue #182)

Hard, **last-resort** caps enforced OUTSIDE strategy logic so they cannot be
overridden by grid-engine decisions (the #159 storm motivated them). They are
**additive** to `min_liq_ratio` / `max_liq_ratio` / the low-balance preflight —
they do NOT replace them. All cap state + decision logic lives in ONE place,
`gridbot/safety_caps.py` (`SafetyCaps` + frozen `CapDecision`); the orchestrator
builds ONE instance per strat in `_init_strategy` and passes the SAME object
(and the same monotonic clock) to both the `StrategyRunner` (C1/C2/C3) and the
`IntentExecutor` (C4) so the C4 window and the loss latch are one source of truth.

- **Four caps** (`StrategyConfig.safety_caps`, a `SafetyCapsConfig`; every
  per-cap value defaults to `None` = that cap disabled, so an existing YAML with
  no `safety_caps:` block is byte-for-byte pre-0079 — no order is ever rejected
  until the operator opts in; `enabled` is the master kill-switch):
  - **C1 `max_notional_per_symbol`** (USDT) — halt new OPENs when live
    `long.position_value + short.position_value >= cap`. Reduce-only closes are
    EXEMPT.
  - **C2 `max_open_orders`** — pure count limit on tracked `placed` orders;
    rejects BOTH open and reduce-only at/above the cap.
  - **C3 `session_loss_limit`** (positive USDT magnitude) + flag
    `session_loss_auto_reset_utc_midnight`. Evaluated in `on_position_update`
    (where realized PnL lands) off the per-cycle `cur_realized_pnl` sum (the
    Bybit UI "Realized", NOT the ~80x lifetime `cumRealisedPnl`). On trip it is a
    **full circuit breaker**: cancel ALL working orders once (via
    `_execute_cancel_intent` → honors shadow mode + tracked-order state; uses the
    wire `orderId`, not `orderLinkId`) then suppress ALL new places (open AND
    reduce-only) via `loss_tripped()`. Recovery: latch clears on the first
    position update of the next UTC date (auto-reset True) or only on process
    restart (False). The UTC reset date is seeded lazily on the first
    `check_loss_breaker` (the constructor has only a monotonic clock; UTC enters
    via the `now_utc` arg).
  - **C4 `max_orders_per_minute`** — trailing-60s rate limit at
    `IntentExecutor.execute_place` (the single live-submit choke point, so
    retry-queue re-dispatch is rate-limited too). Returns the non-retryable
    `error="safety_cap_rate_limit"` sentinel; only real successes consume the
    window — **shadow placements do not**.
- **Precedence**: caps run in `_execute_place_intent` as **Step 2.5** — AFTER
  qty-resolve (Step 1) + the 110017 breaker (Step 2) and BEFORE the dirty refresh
  / `_is_good_to_place` guard — so a capped intent never reaches the strategy
  guard or the exchange. When a cap and a strategy guard both want to reject, the
  cap wins (it runs first / fail-closed). A capped or rate-limited intent is
  **dropped, NOT enqueued** to the retry queue (mirrors the 110007 / 110017
  drop): the runner short-circuits any `error.startswith("safety_cap")`.
- **Live-only → replay/backtest parity preserved by construction**: caps run in
  `StrategyRunner` / `IntentExecutor` only; `apps/backtest` / `apps/replay` /
  `apps/comparator` use `BacktestRunner` and import neither, so the cap code
  never runs there. `max_margin` remains dead/unused — C1 supersedes its original
  intent with a real notional cap (it does NOT revive `max_margin`).
- **Shadow mode**: the runner-level C1/C2/C3 run BEFORE the executor, so a
  tripped cap suppresses even the `[SHADOW] Would place …` log (faithful); C4 is
  after the executor's shadow early-return, so shadow never consumes its window.
- Rejection logging is throttled per reason (`_SAFETY_CAP_WARN_THROTTLE_SEC`,
  runner) and per executor (`_RATE_LIMIT_WARN_THROTTLE_SEC`); Telegram alerts
  throttle via `error_key=f"safety_cap_{reason}_{strat_id}"`.
- Files: `config.py` (`SafetyCapsConfig`), `safety_caps.py` (**new**),
  `runner.py` (Step 2.5, C3 in `on_position_update`, cached per-direction
  `position_value`, drop-not-enqueue), `executor.py` (C4 + `record_accepted_submission`),
  `orchestrator.py` (`_init_strategy` builds + shares one instance),
  `gridbot.yaml.example`. Tests: `test_safety_caps.py` (**new**, unit trip+recovery
  per cap), `test_runner.py` / `test_executor.py` (integration).

### SAME ORDER detection

- `StrategyRunner._check_same_orders_side()` compares tracked orders by `TrackedOrder.placed_ts` when both fills map to tracked orders; use fill `exchange_ts` only as a fallback for untracked/legacy events. Duplicate same-price orders can rest concurrently and fill more than 5s apart in thin markets, so fill-time-only windows silently miss the critical duplicate-placement bug.
- **Dedup + auto-recovery (feature 0031)**: `_same_order_dedup_cache` is the single dedup mechanism, keyed by `frozenset` of the two exchange order_ids in the SAME ORDER pair. Each entry carries `first_seen_ts`, `last_seen_ts`, and `verdict ∈ {WS_GLITCH_SUSPECTED, REAL_DUPLICATE, UNKNOWN}`. TTL is `_SAME_ORDER_DEDUP_TTL_SEC = 21600` (6 h), sized to comfortably exceed the 3 h 24 min retrigger gap from the 2026-05-09/10 incident. Don't reintroduce a separate rate-limit set — both REST-rechecking and dedup share this key.
- The dedup gate inside `_check_same_orders_side` is **verdict-aware**: `WS_GLITCH_SUSPECTED` retriggers are silently suppressed (DEBUG log only) **and must also set `_drop_phantom_event_for_current_call = True`** so `on_execution` drops the phantom replay end-to-end (no `mark_filled`, no engine, no place); `REAL_DUPLICATE` retriggers are silenced too but **must explicitly re-set `_same_order_error = True`** because `_check_same_orders` resets the flag at the top of every call — without this re-set the dedup gate would silently lift a legitimately-latched block. Do NOT set the phantom-drop flag for REAL_DUPLICATE: the event is real (REST saw the fill); `mark_filled` and `engine.on_event` must run normally on it. `UNKNOWN` falls through to the full first-trigger path. The first-trigger path inserts an `UNKNOWN` cache entry **before** running REST cross-check, so a same-event burst is suppressed by the gate on events 2..N.
- `verdict == "WS_GLITCH_SUSPECTED"` from `_diagnostic_rest_check_executions` auto-clears the soft-block via `reset_same_order_error(emit_recovery_info=False)` (clears flag + both per-side execution buffers + throttle state), but only after a paginated, time-bounded REST execution slice completes without truncation and sees exactly one of the two order_ids. A truncated/empty/incomplete REST slice is `UNKNOWN` and must leave the block latched; do not treat absence from a single recent page as proof of a phantom. The auto-clear path emits exactly **one** INFO log carrying both verdict context and, when at least one throttled `Same-order error active` WARNING was emitted during the latched period (feature 0046), a trailing `; suppressed N WARNINGs since` suffix — no `notifier.alert(...)` because `Notifier.alert()` always logs at ERROR level (`notifier.py:67`) and a successful recovery must not produce an ERROR-level line. The `emit_recovery_info=False` argument suppresses the generic recovery INFO from the reset helper so there is no double-log. `REAL_DUPLICATE`, `UNKNOWN`, and the REST-exception path leave the block latched. `reset_same_order_error()` does **not** clear the dedup cache, by design.
- **WARNING throttle (feature 0046, issue #94)**: while the soft-block is latched, `on_ticker` emits the `Same-order error active, skipping order placement` WARNING using a per-runner instance throttle (`_same_order_warn_last_ts`, `_same_order_warn_suppressed`) with `_SAME_ORDER_WARN_THROTTLE_SEC = 60.0`. Cadence: loud-first WARNING on entry → suppress within the window → at most one heartbeat re-emit per window with `(suppressed N since last)` suffix. On True→False transition, exactly one INFO line summarises the suppression window — emitted only when at least one WARNING fired during the latched period (silent latch + silent clear emits no INFO). Reset side-effects (`_same_order_error`, execution buffers, throttle counters) are owned by `reset_same_order_error()`; the REST WS-glitch auto-clear path passes `emit_recovery_info=False` to merge verdict + suppressed-count into a single INFO line. The clean-fill auto-clear path inside `_check_same_orders` does **not** route through the reset helper (it must preserve execution buffers); it snapshots `was_set` before the per-side checks and calls `_emit_clear_recovery_if_needed()` only on a confirmed True→False net transition. A 1-hour soft-block emits ≤ 61 WARNING lines (1 loud + ≤ 60 heartbeats).
- **Phantom event drop on the live execution path**: the auto-clear path also sets `_drop_phantom_event_for_current_call = True`. `on_execution` resets this flag at entry and, when set, returns BEFORE `self._engine.on_event(event)`, BEFORE `_execute_intents(...)`, **and BEFORE `tracked.mark_filled()`**. The order in `on_execution` is: lookup tracked → run `_check_same_orders` → drop-check → only then `mark_filled` + engine + place. Engine must NOT see the phantom fill (would corrupt grid/position state and contaminate every subsequent tick — e.g., mark the slot as walked, fail to re-place at that price, double-count when the real resting order eventually fills). The tracked order must NOT be flipped to `filled` either — `get_limit_orders()` filters `status not in ("placed",)` at line 324, so a phantom-marked-filled order silently disappears from the live in-memory book and the reconciler then operates on a stale view. The "update grid state regardless of error" convention applies only to the "we're not sure" case; once REST has produced an authoritative `WS_GLITCH_SUSPECTED` verdict, drop the event end-to-end. `on_ticker` and `on_order_update` do NOT consult this flag (it is scoped to the current execution event).
- `Notifier.alert(error_key=...)` only throttles Telegram delivery (`_DEFAULT_THROTTLE_SECONDS = 60` at `notifier.py:18`); `logger.error("ALERT: ...")` always fires. To suppress the log line itself, dedup must happen upstream of the notifier, not via `error_key`.

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
  - **Feature 0080 (issue #183) — strat_id namespacing**: `create(strat_id=...)` salts the hash by `strat_id` so two strategies on the same `(account, symbol)` get DISTINCT prefixes. `strat_id` is a SALT, NOT in `_IDENTITY_PARAMS`; the `None` default reproduces the pre-0080 hash byte-for-byte (back-compat for callers + historical rows — only the 3 production call sites thread it). Wire form `{hash16}-{millis}` and `extract_client_order_prefix` unchanged; Bybit `orderLinkId` ≤ 36 chars (`gridbot.order_link_id._BYBIT_ORDER_LINK_ID_MAX`; `make_order_link_id` raises if over). **Replay must salt with the live `strat_id`** or the comparator's `client_order_id` join breaks — the recording's strat_id is on NO DB row, so supply it via config; `apps/replay/src/replay/engine.py` resolves precedence `ReplayStrategyConfig.strat_id` → `seed.strat_id` → synthetic `replay_{symbol}`. For blank-start comparison set `strategy.strat_id` to the recording's live id. `validate_no_shared_symbol` still rejects co-location (positionIdx/cancel-on-mismatch sharing remains the blocker, not the prefix).

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

### Grid State DB snapshots — feature 0047

Live writes the same `grid.grid` payload to **two parallel sinks** from `_on_grid_change`:

1. **Legacy file** — `GridStateStore` (see section above). Timestamp-agnostic, latest-wins-per-strat coalesced into one `db/grid_anchor.json`. Owns live-restart parity.
2. **DB table** — `grid_state_snapshots` (column set: `run_id, account_id, strat_id, symbol, exchange_ts, local_ts, grid_json, grid_step, grid_count, raw_fingerprint`). Owns Phase 4 replay seeding. Written by `apps/gridbot/src/gridbot/writers/grid_state_writer.py:GridStateWriter` — sync API + `queue.Queue` + single worker thread; **NOT asyncio** (gridbot's main loop is sync; the event_saver writers live in a different process).

Both backends are independent guards in `runner._on_grid_change(grid, exchange_ts)` — file fires whenever `state_store` is configured; DB fires only when `grid_state_writer` is set AND `exchange_ts is not None`. The `on_change` callback signature is `(grid, exchange_ts)`; constructor-time `restore_grid` produces `exchange_ts=None` and DB drops the write (file is unaffected because it doesn't time-index).

**Replay loader priority** (`apps/replay/src/replay/engine.py:_load_seed`): account-scoped DB row at-or-before `seed.at_ts` (`load_grid_state_from_snapshots`) → active live/shadow gridbot DB row for the same `strat_id`/`symbol` when the account-scoped lookup misses (`load_grid_state_from_active_snapshots`; needed when `seed.account_id` does not match gridbot snapshot rows, e.g. legacy recorder placeholder vs unified `account_id_for` after 0053) → file path if `seed.grid_state_path is not None` → tail behaviour depends on `seed.enabled`: when `seed.enabled=false`, fall back to a fresh blank-build; when `seed.enabled=true` (0054), raise `SeedDataQualityError` because all loaders returned `None` (treat step/count mismatch as absence). `Grid.restore_grid` consumes both DB and file payloads identically (same `list[{side, price}]` shape).

**Cross-run lookup (feature 0052) — intentional asymmetry between `get_at_or_before` and `get_latest`.** Gridbot's `GridStateWriter` stamps rows under gridbot's **live** `run_id` (`run_type='live'`), while replay receives the **recording** `run_id`. The replay seed loader (`GridStateSnapshotRepository.get_at_or_before`) deliberately does **NOT** filter by `run_id` — it filters on `(account_id, strat_id, symbol, exchange_ts <= at_ts)`, joins `runs` to require the writer run was **active at `at_ts`** (`start_ts <= at_ts` and `end_ts` NULL or `>= at_ts`, `run_type` in `live`/`shadow`), and tie-breaks by `ORDER BY exchange_ts DESC, id DESC`. Without the run-active guard (feature 0062), a completed gridbot run's last snapshot can seed replay after restart before the new run's bootstrap write — wrong grid under `seed.enabled=True` while 0054 only raises when no row exists. The `symbol` predicate prevents cross-symbol bleed-through for accounts whose `strat_id` was retained across a rename (e.g. `strat_id='ltcusdt_test'`). On success the loader emits an INFO log of the form `<strat>: grid snapshot loaded from run_id=<actual writer run_id> exchange_ts=<ts>` — the logged `run_id` is the gridbot live run that wrote the row, **not** the recorder `run_id` the caller passed. **(0052 N3)** This success-path log fires only after step/count validation passes; the shared `_grid_seed_from_row` helper (used by both `get_at_or_before`'s loader and the account-agnostic `load_grid_state_from_active_snapshots`) logs `falling back` and returns `None` on a mismatch, and the caller suppresses the `loaded` line in that case. `GridStateSnapshotRepository.get_latest`, by contrast, IS per-`run_id` because the writer's in-memory dedupe gate (`get_last_fingerprint`) and the orchestrator bootstrap probe (`_bootstrap_grid_snapshots`, issue #108) both rely on per-run scoping — dropping `run_id` there would either suppress the first legit snapshot of a new run or fire the "investigate run_id reuse" alert on every restart. Do not unify these two methods. Pre-0052 the lookup also used `run_id` and silently fell through to the file path on every shared-DB replay — never reintroduce a `run_id` predicate to `get_at_or_before` without also fixing the recorder/gridbot run_id divergence. **(feature 0062 pitfall 1)** Tests that seed a DB grid snapshot must insert it under a `live`/`shadow` run **active at `at_ts`**, never a `recording` run — the `run_type` guard now excludes recording runs, so a grid snapshot under the recorder's `run_id` (a common fixture shortcut, e.g. `seeded_db`'s `"seed-run"`) returns `None` and the loader falls back to the file/0054 path. Also: a unit test querying a fixed-past `at_ts` must build its own `Run` with an explicit `start_ts <= at_ts` (the shared `sample_run` fixture uses `start_ts=datetime.now(UTC)`, which the new `start_ts <= at_ts` predicate excludes — yielding a spurious `None`). **(feature 0062 pitfall 2 — unclean shutdown)** The guard fixes the *graceful* stop path (`end_ts` stamped via `orchestrator._update_run_records_stopped`). A crash/kill can leave the old run `status="running"`, `end_ts=NULL`; `Orchestrator._create_run_records` now closes those orphaned rows via `RunRepository.close_stale_running_runs` before inserting the new run (issue #148), so replay no longer treats them as active at `at_ts` once `end_ts` is stamped at restart.

**Pitfalls**

- **`on_change` arity is silently swallowed** at `grid.py:78` — every callsite passing `on_change=` must use `(grid, exchange_ts)`. The grep gate `grep -rE 'on_change|on_grid_change' packages/ apps/ --include='*.py'` should show no single-arg lambdas.
- **`account_id` MUST match the `uuid5(NAMESPACE, "account:<name>")` formula** — single source of truth is `grid_db.identity.account_id_for()` (with sibling helpers `user_id_for`, `strategy_id_for` and the shared `UUID_NAMESPACE`). Gridbot's `_create_run_records` and recorder's `_seed_db_records` (shared-DB branch) both import from `grid_db.identity`; do not re-inline the namespace or the uuid5 formula anywhere else. Any deviation breaks the FK link to `runs.account_id` and replay returns no row. Feature 0053 removed `Orchestrator._account_id_for` and the per-process `_UUID_NAMESPACE` — those names are gone, not renamed. The replay `seed.account_id` (Phase 4 configs) must equal `account_id_for("<gridbot accounts[].name>")` — for `mainnet_live` that's `9bdb9748-f9e0-5c13-b144-0ad6a8dbcaba`; the pre-0053 placeholder `00000000-...-002` is no longer written for shared-DB recorder runs.
- **Partial unique index `uq_grid_state_snapshots_fingerprint_at_ts`** is scoped to `(run_id, account_id, strat_id, exchange_ts, raw_fingerprint) WHERE raw_fingerprint IS NOT NULL`. The repository's `insert()` must pass both `index_elements=[...]` AND `index_where=GridStateSnapshot.raw_fingerprint.is_not(None)` to ON CONFLICT DO NOTHING, otherwise the partial constraint won't bind.
- **`id DESC` tie-break depends on FIFO insertion** for same-`(run, account, strat, exchange_ts)` enqueues. The writer's single global queue preserves this; do NOT introduce per-strat queues or batch reordering without preserving per-(strat, ts) order, or `update_grid` out-of-bounds rebuilds will replay the intermediate (post-rebuild) state instead of the final (post-side-assignment) state.
- **Bootstrap window**: writer is constructed in `Orchestrator.__init__` but `_run_ids` is populated later in `start()` via `_create_run_records`. Writer's `run_id_provider` returns `None` during this window; writes are dropped with a one-time INFO. WS connect happens AFTER `_create_run_records`, and reconciliation does not mutate the grid, so the first real `_on_grid_change` always fires with `_run_ids` populated.
- **Startup bootstrap probe (issue #108) — best-effort, not blocking**: `Orchestrator.start()` calls `_bootstrap_grid_snapshots()` immediately after the writer's worker thread starts. For each runner, it probes `grid_state_snapshots` for the latest row per `(run_id, account_id, strat_id)` via `get_last_fingerprint(...)` (returns `(fingerprint, exchange_ts)` or `None`) and compares against `grid_fingerprint(current_grid, …)`. Four branches:
    - **Empty (no row)** → writes the current in-memory grid with `exchange_ts=Run.start_ts` (just-created run row's `start_ts`, also tracked in `_run_start_ts`) as the design-intended lower-bound anchor.
    - **Match (`last_fp == current_fp`)** → primes `_last_fingerprint` only; no row written.
    - **Stale, `last_exchange_ts <= Run.start_ts`** (realistic case: residual row from before — or exactly at — this run's start) → writes with `exchange_ts = Run.start_ts` (literally; no `+1ms`). The new row anchors at run start AND supersedes the stale row: same-`exchange_ts` ties are broken by `id DESC` in repository ordering (`ORDER BY exchange_ts DESC, id DESC`), and the autoincrement `id` is higher for the newer insert. Earlier drafts of this plan used `max(Run.start_ts, last_exchange_ts + 1ms)`; that broke the equality edge (`last_exchange_ts == Run.start_ts`) because it pushed the correction to `Run.start_ts + 1ms`, leaving a seed at exactly `Run.start_ts` reading the stale row. The partial unique index includes `raw_fingerprint`, and stale/new rows have different fingerprints (the very reason this branch fires), so same `exchange_ts` cannot conflict.
    - **Stale, `last_exchange_ts > Run.start_ts`** (anomalous: a fresh `run_id` should not have rows in its own future) → **alert-only, no write**. WARNING log + `notifier.alert(..., error_key="bootstrap_anomalous_{strat_id}")` + bump `writer._total_bootstrap_failures`. Replay seeds in `[Run.start_ts, last_exchange_ts]` cannot honestly be repaired by a single bootstrap write (writing at `Run.start_ts` loses to the stale row; writing at `last + 1ms` leaves the historical window broken) — dual-write with dedupe bypass was considered and rejected as over-engineering for a scenario that shouldn't occur in production. Operator must investigate.

  The method then calls `flush(timeout=5.0) -> bool`; on `False` (timeout) AND on per-runner probe/write exceptions, the bot keeps starting but emits `notifier.alert(...)` + WARNING + bumps `writer._total_bootstrap_failures`. Probe errors are NEVER collapsed to "DB empty" — that would risk duplicate inserts. **Clock-domain caveat**: `Run.start_ts` is wall-clock-derived (`utc_now` default on the column), while live `exchange_ts` is the Bybit exchange clock. `Run.start_ts` is the *design-intended* lower bound, not a strict ordering guarantee — sub-second exchange-vs-wall-clock skew at run start can leave a tiny window in which a seed.at_ts (exchange domain) just below `Run.start_ts` (wall domain) misses the bootstrap row via `at_or_before`. Practically impossible in normal use; flagged so future debugging of "missing-by-milliseconds" replay misses lands on this path, and so future implementers do not build invariants assuming `Run.start_ts <= every live exchange_ts` holds absolutely. Operator-visible degradation: watch `writer.get_stats()["total_bootstrap_failures"]` and the `bootstrap_grid_state_*` notifier channel.

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

## Private WS disconnect handling (event_saver / recorder)

**Feature 0035 — private WS message-gap watchdog disabled on recorder side**:
- **Parity with gridbot feature 0026**: pybit ping/pong frames bypass business-event handler, so the 30s message-gap watchdog produces false-positive disconnects on a healthy quiet private WS. Recorder now passes `message_gap_watchdog_enabled=False` to `PrivateWebSocketClient`, matching `gridbot.orchestrator._init_account`.
- **Feature 0037 follow-up**: Recorder keeps a private TCP-level health probe in `PrivateCollector` while the message-gap watchdog stays disabled. On a dead private socket it resets the client and invokes the existing private gap callback so REST execution reconciliation runs for the outage window.
- **Invariant**: Do not remove both private disconnect detectors. Message silence is not a private-stream failure signal, but the recorder still needs TCP-level liveness checks so real private WS outages do not silently skip execution backfill.
- File: `apps/event_saver/src/event_saver/collectors/private_collector.py`

**Feature 0039 — bound private WS reset/disconnect with daemon thread + wait_for**:
- **Why not `asyncio.to_thread` for pybit reset/disconnect**: the default `ThreadPoolExecutor` is joined by `concurrent.futures.thread._python_exit` at interpreter shutdown. A parked pybit call would block interpreter exit, moving the hang from `stop()` to `atexit` where it is also non-responsive to SIGTERM.
- **Pattern**: wrap any potentially-hanging blocking call from a recorder collector path in `_run_in_daemon_thread(fn)` (a daemon `threading.Thread` bridged to the loop via `loop.create_future()` + `call_soon_threadsafe`) and bound it with `asyncio.wait_for(...)`. The daemon flag is load-bearing — daemon threads are not joined at interpreter exit.
- **Cancellation safety**: the completer must guard on `fut.done()` before `set_result` / `set_exception` (so a late-returning abandoned worker does not raise `InvalidStateError`) and swallow `RuntimeError` from `call_soon_threadsafe` (so a worker that returns after the loop closed exits cleanly).
- **Shutdown invariant**: if a prior `reset()` timed out, the worker is still holding `PrivateWebSocketClient._lock`; `stop()` must **skip** `disconnect()` (it would deadlock on the same lock) and clear the client reference — the daemon thread leaks until the process exits. This is the explicit "abandon" trade-off documented in `docs/features/0039_PLAN.md`.
- **Don't touch an abandoned client from the event loop**: `PrivateWebSocketClient.is_socket_alive()` (`ws_client.py:504`) acquires the same `_lock` the parked reset worker holds. After `_ws_reset_abandoned` is set, `_ws_health_check_once()` must return early before any lock-taking method on the client runs — otherwise the next health tick blocks the event loop and reintroduces the SIGTERM hang.
- **Pybit daemon verification**: pybit's `WebSocket` worker thread is started with `self.wst.daemon = True` (`.venv/lib/python3.12/site-packages/pybit/_websocket_stream.py:168-169`). Verified once for the abandon strategy — the OS reclaims the leaked thread at process exit. If the pybit version changes, re-check this line.
- **Tests**: `try/finally` release of `threading.Event` gates is mandatory so parked worker threads do not leak between tests.
- Files: `apps/event_saver/src/event_saver/collectors/private_collector.py:_run_in_daemon_thread`, `_ws_health_check_once`, `stop`.

## Testing

### Cross-Package Integration Tests (`tests/integration/`)

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

**Shadow-Mode Validation Pipeline** (`test_shadow_validation.py`): feeds identical price data through two independently constructed paths — **Path A** is `BacktestEngine` (orchestrated, high-level), **Path B** is manual `GridEngine + BacktestOrderManager` (low-level, mimics shadow mode) — and validates trade count match, deterministic client_order_ids, 100% comparator match rate, zero price/qty deltas, identical PnL totals. Uses `generate_price_series()` for reproducible sine-wave price oscillation.

### Test Pitfalls

1. **Mocking `async def` functions in cli() tests**: When `cli()` calls `asyncio.run(main(...))`, patching `main` with `return_value=0` auto-creates an `AsyncMock` that still returns a coroutine. Use `_close_dangling_coro(mock_run)` helper (in `test_main.py`) to close the unawaited coroutine after assertions, silencing warnings.
2. **`asyncio.get_event_loop()` deprecation in tests**: Use `asyncio.new_event_loop()` instead of `asyncio.get_event_loop()` when setting up event loops in non-async test methods (e.g., `saver._event_loop = asyncio.new_event_loop()`).
3. **Import ordering in test files**: Never place class/dataclass definitions between import blocks. All imports must be grouped at the top of the file before any class or function definitions (e.g., `test_eventsaver_db.py` had `SeededDb` splitting import blocks).
4. **`integration_helpers.py` import path**: `tests/integration/conftest.py` adds `tests/integration/` to `sys.path` explicitly so `import integration_helpers` works even when pytest is invoked without the root `pyproject.toml` `pythonpath` setting (e.g., per-app test runs).
5. **`_fetch_wallet_balance` fallback**: Returns `0.0` when no USDT balance is found in the wallet API response, but now logs `logger.warning` first so unexpected API structures are visible in logs.
6. **generate_price_series**: Uses sine-wave oscillation; period = `num_ticks / 4` (4 complete oscillations). Increase `amplitude` for more fills.
7. **Shadow-Mode Qty Calculator**: Must replicate `BacktestEngine._create_qty_calculator()` exactly, including `InstrumentInfo.round_qty()` ceil rounding.

## PnL Calculation Functions (`packages/gridcore/src/gridcore/pnl.py`) — Added 2026-02-24

Pure PnL calculation functions extracted into gridcore as the single source of truth.

**Functions exported from gridcore:**
- `calc_unrealised_pnl(direction, entry_price, current_price, size)` — Absolute PnL
- `calc_unrealised_pnl_pct(direction, entry_price, current_price, leverage)` — Standard Bybit ROE %
- `calc_position_value(size, entry_price)` — Entry-based notional (size * entry_price); feeds this project's local margin/IM/MM helpers. NOT Bybit's reported positionValue (mark-based: |size| * mark_price). Bybit UTA IM uses mark + hedge (RULES.md:2184); local formulas stay entry-based. Snapshot/parity code computes mark at emit time separately (feature 0060).
- `calc_initial_margin(position_value, leverage)` — Initial margin
- `calc_liq_ratio(liq_price, current_price)` — Liquidation ratio
- `calc_maintenance_margin(position_value, symbol, tiers=None)` — Tier-based MM (supports dynamic tiers)
- `calc_imr_pct(total_im, margin_balance)` — Account IMR %
- `calc_mmr_pct(total_mm, margin_balance)` — Account MMR %
- `calc_margin_ratio(position_value, wallet_balance)` — Per-position margin ratio (positionValue / walletBalance)
- `parse_risk_limit_tiers(api_tiers)` — Bybit API response → `MMTiers`

All take Decimal inputs; `position.py` keeps float copy for risk mgmt performance.

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

### Reconciliation & order-adoption invariants (Phase E)

   - **Inject is NOT durable adoption (bbu2-faithful)**: Injected orders live for exactly one ticker event. On the first `on_ticker` after startup, `GridEngine._place_grid_orders` (`packages/gridcore/src/gridcore/engine.py:319-325`) cancels any injected order whose price is not in the current `grid_price_set` (`'outside_grid'` reason), and `engine.py:305-312` cancels any at a grid price with the wrong side (`'side_mismatch'` reason). Over-limit cases (`engine.py:237-243`) trigger a full rebuild that cancels everything. Direct port of bbu2 `strat.py:154-160`, `:145-149`, `:103-104`. This means: (a) a "silent adoption of manual orders" security review is a false alarm — the bot does not keep manual orders around, it destroys them on the next tick; (b) the **real** operational concern is the opposite — the bot will **cancel** any limit order on the symbol that doesn't match the grid; (c) do NOT add a refuse-to-start check in `reconcile_startup` — it would re-break normal crash-restart (the bot's own prior orders look identical to manual ones) and was already removed in commit `138737a` for that reason.
   - **(account, symbol) uniqueness is enforced unconditionally at config load**: Even though `orderLinkId` IS sent to Bybit (since feature 0029, with a `-{millis}` suffix added in HOTFIX 2026-05-08), it cannot disambiguate strategies at runtime. The deterministic prefix is a SHA of `(symbol, side, price, direction)`, so two strategies on the same `(account, symbol)` would compute the SAME prefix for the same logical order — the wire-form suffix only differs across re-placements, not across strategies. Two strategies on the same `(account, symbol)` pair would therefore cancel each other's orders every tick via the cancel-on-mismatch pass described above. `GridbotConfig.validate_no_shared_symbol` (`apps/gridbot/src/gridbot/config.py`) rejects any such configuration at load time with **no escape hatch** — there is no flag to disable it. bbu2 enforces the same invariant structurally: its `amounts[].strat` field is a scalar pointing at a single `pair_timeframes[]` entry, and each `pair_timeframe` has a single `symbol`, so the bad configuration is physically unrepresentable in bbu2's config schema. grid-bot's schema is more flexible (independent `accounts` and `strategies` lists, FK goes `strategy.account → account.name`), so the constraint must be reconstructed as a pydantic validator — but it is enforced just as strictly. If you need a second strategy on the same symbol, use a different account.
   - **Operational consequence (manual orders get cancelled)**: Any limit order on the symbol that is not at a current grid price, or is at a grid price with the wrong side, will be cancelled by the engine on the next ticker event after it is seen (see the "Inject is NOT durable adoption" bullet above for the exact mechanism). This applies to manual orders placed via the Bybit UI while the bot is running, orders from other tools/scripts on the same account, and stale orders left over from a prior run with different grid parameters. **Manual orders and the grid cannot coexist on the same symbol** — the bot treats "not in my grid" as "cancel it." To manually intervene, stop the bot, make your changes, restart, and accept that anything not matching the grid on restart will be cancelled on the first tick.
   - **Before first start**: Closing existing orders for the symbol before the first start is recommended for operator clarity (otherwise the bot will cancel them within ~1 second of startup, which is surprising but not incorrect). There is no config flag to disable either the cancel-on-mismatch behavior or the `(account, symbol)` uniqueness check — both are unconditional.

### orderLinkId wire format & matching

**orderLinkId Wire Format & Matching (HOTFIX 2026-05-08)**: How the deterministic `client_order_id` survives Bybit's id-cache
   - **Why the suffix exists**: Bybit caches `orderLinkId` for ~1-2h post-cancel/fill. Our `PlaceLimitIntent.client_order_id` is a deterministic 16-char SHA256 hex digest of `(symbol, side, price, direction)`, so re-placing the same logical intent collides with the cached id and triggers ErrCode 110072 "OrderLinkedID is duplicate" in a tight loop. Live-verified: ~12k duplicate-rejected attempts / 0 successful orders across a 2h window before the fix.
   - **Wire format**: `{16-hex prefix}-{int(datetime.now(UTC).timestamp() * 1000)}`. The prefix is guaranteed not to contain `-` (`hashlib.sha256().hexdigest()[:16]` returns only `0-9a-f`), so splitting at the first `-` always recovers the deterministic prefix.
   - **Wire-vs-key invariant**: The full suffixed value goes on the wire and is persisted verbatim in `private_executions.order_link_id` and `orders.order_link_id` (forensics). Internal dict keys (`Runner._tracked_orders`, comparator join key, replay seed `client_id`) use only the deterministic prefix.
   - **Retry idempotency invariant (feature 0032)**: The wire suffix is minted once per `PlaceLimitIntent` placement lifecycle in `StrategyRunner` and stored on `PlaceLimitIntent.order_link_id`; runner reattempts, retry-queue retries, and fresh engine re-emissions after a failed placement reuse that same wire id. Executor-side generation is only a fallback for direct callers that bypass runner assignment.
   - **Reconcile-upgrade path**: If REST order sync later reports an open order whose normalized prefix matches a pending/failed tracked placement, `Runner.inject_open_orders` upgrades that tracked order to `placed`, patches the tracked intent with the exchange-reported wire `orderLinkId`, and cancels queued retries for that prefix. This closes the ambiguous-failure window where Bybit accepted the first request but the bot only observed a timeout/error.
   - **Helper**: `gridcore.intents.extract_client_order_prefix(order_link_id) -> Optional[str]` splits at the first `-` and returns the prefix. `None` or empty-string input → `None` (so callers using `prefix or fallback_id` cleanly fall back). No-hyphen input → unchanged (pre-hotfix backward compat).
   - **Three call sites normalize on read**: (a) `gridbot.runner._find_tracked_order` and `inject_open_orders` — strip suffix before lookup/inject; (b) `comparator.loader.LiveTradeLoader.load` — strip before grouping live executions; (c) `replay.snapshot_loader.load_active_orders` — strip before seeding active orders for replay. Tests in `packages/gridcore/tests/test_intents.py`, `apps/gridbot/tests/test_runner.py`, `apps/comparator/tests/test_loader.py`, `apps/replay/tests/test_snapshot_loader.py`.
   - **Files**: `packages/gridcore/src/gridcore/intents.py` (helper + `PlaceLimitIntent.order_link_id`), `apps/gridbot/src/gridbot/order_link_id.py`, `apps/gridbot/src/gridbot/executor.py` (wire-id fallback), `apps/gridbot/src/gridbot/runner.py` (wire-id assignment/reuse + read-side normalization), `apps/comparator/src/comparator/loader.py`, `apps/replay/src/replay/snapshot_loader.py`.

### Active WS reconnect with TCP-level probe (feature 0024)

**Active WS reconnect with TCP-level probe (2026-05-03, feature 0024)**: bbu2 `_ensure_*_connection` pattern
   - **Problem**: Wrapper's `is_connected()` is state-flag based — flips False only on explicit `disconnect()`. A dead TCP socket pybit hadn't noticed left it stuck True. Mainnet observed 6–15 min reconnect gaps.
   - **Two health signals (both call `client.reset()`)**:
     - **Primary** (TCP-level, every 10s): `Orchestrator._ws_health_check_once()` calls new `client.is_socket_alive()` → pybit's `ws.sock.connected`. Mirrors bbu2 `ENSURE_SOCKET_INTERVAL = 10`.
     - **Secondary** (message-gap, on heartbeat fire): existing 30s gap detector → `on_disconnect` callback → `Orchestrator._on_ws_disconnect()` → `client.reset()`. Catches "socket alive but server silent" failure mode that TCP check misses.
   - **`reset()`**: Stop heartbeat → `_disconnect_internal()` → `connect()` (re-subscribes all streams). Idempotent — back-to-back resets are a no-op + a single re-establishment.
   - **Heartbeat thread sharp edge**: `on_disconnect` callback runs on the heartbeat thread. **Wrapper-level guard**: `_stop_heartbeat_watchdog` skips `Thread.join()` when `threading.current_thread() is self._heartbeat_thread`, so calling `reset()` inline from a callback is safe (no `RuntimeError`). The orchestrator still dispatches reset to a one-shot daemon worker (`WSReset-{account}-{kind}`) to avoid blocking the heartbeat thread on the full TCP teardown / handshake / subscription replay.
   - **Zombie heartbeat protection**: `_start_heartbeat_watchdog` replaces `self._stop_heartbeat` with a fresh `threading.Event` each start; the old loop holds a reference to the old (still-set) event and exits cleanly. `_heartbeat_loop(stop_event)` takes the event as parameter.
   - **`retries=0`**: Both `connect()` methods pass `retries=0` to `pybit.unified_trading.WebSocket(...)` → pybit's `infinitely_reconnect=True`. Removes the 10-attempt cliff at which pybit raises `WebSocketTimeoutException` and gives up.
   - **Orchestrator wiring**: `_init_account` constructs WS clients with `on_disconnect=lambda ts, a=name: self._on_ws_disconnect(a, "public"|"private", ts)`. Periodic gate `_next_ws_health_check` in `_tick()` between `_next_health_check` and `_next_order_sync`.
   - Files: `packages/bybit_adapter/src/bybit_adapter/ws_client.py`, `apps/gridbot/src/gridbot/orchestrator.py`

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
- **Quantity**: same amount format as gridbot (`"100"`, `"x0.001"`); rounding uses `math.ceil`. Legacy `"b..."` removed in 0028.
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

### Robust spike-vs-drift stats (feature 0070, issue #156)

Each per-snapshot abs-delta family in `position_metrics.py` AND the trade-level `pnl_*` family carry six robust stats alongside the existing `mean`/`max`, so an operator rule can tell a **sharp spike** (real divergence — few snapshots towering over baseline) from **sustained drift** (benign accumulation — many snapshots persistently above baseline). Today's flat `max > $0.30` gate conflates them (C108: `cur_realised_usdt_max_abs_delta = $0.307` tripped purely from drift).

- **Six fields per family**: `<f>_median_abs_delta`, `<f>_p95_abs_delta`, `<f>_std_abs_delta`, `<f>_spike_intensity` (Decimal) + `<f>_spike_count_30c`, `<f>_spike_count_relative_3` (int). Helper: `comparator.metrics._spike_stats(abs_deltas) -> RobustStats` (single shared impl; `position_metrics._fold_family` and `metrics.calculate_metrics` both call it — do NOT duplicate). Position families folded in `fold_metrics_into._fold_family` over the same `matched` lists as mean/max (so they inherit the 0044 state-diverged exclusion). `pnl_*` folded in `calculate_metrics` over per-trade `pnl_delta`.
- **Families instrumented (8 position + 1 trade)**: `cur_realised_usdt`, `pos_value_usdt`, `cum_realised_usdt`, `upnl_usdt`, `unrealised_pnl`, `liq_price`, `position_im`/`position_mm` (optional — issue #155 noise, folded for symmetry), and `pnl`. Price/qty keep mean/median/max only (out of scope).
- **Definitions**: `median = deltas[len//2]` (**upper-mid** index — NOT the averaging `_decimal_median`; the spike rules are calibrated to this exact index, keep the two helpers distinct); `p95 = deltas[int(len*0.95)]` clamped to `len-1` (defensive — the truncating index is already always ≤ len-1, so it never actually fires; prevents IndexError reasoning on tiny lists); `std = statistics.pstdev` (Decimal in → Decimal out, `0` for one element); `spike_intensity = max - median`; `spike_count_30c = #(|delta| > ABS_THRESHOLD)`; `spike_count_relative_3 = #(|delta| > REL_K*median)` **only when `median > 0`** (the `median==0` guard is mandatory — the relative test is otherwise trivially true for every positive delta → returns 0).
- **Comparator constants** (declarative, in `metrics.py`): `ABS_THRESHOLD = Decimal("0.30")`, `REL_K = Decimal("3")`. **Operator Layer 1–4 thresholds live HERE / in the external monitoring prompt, NOT in code** (comparator only emits metrics; operator applies the rule):
  | Layer | Rule (substitute the family prefix) | Meaning |
  |---|---|---|
  | 1. Spike (real) | `spike_intensity > $0.20` AND `spike_count_30c ≤ 3` | sharp peak, few snapshots |
  | 2. Drift (known) | `median > $0.10` AND `spike_count_30c > 10` | persistent gap — SHOW, don't flag if `bt_only > 0` |
  | 3. Heavy tail | `p95 > $0.50` AND `median < $0.10` | quiet baseline, hot tail — investigate |
  | 4. Volume floor | `position_pairs_compared < 20` | skip Layers 1–3 — too few samples for percentiles |

  For `cur_realised_usdt` Layer-1 replaces the old `max > $0.30` rule; for `pos_value_usdt` use `spike_intensity > $0.30` (cleaner than the old `> $0.50`).
- **Layer-shorthand → emitted-key**: the helper's internal `spike_count_abs`/`spike_count_rel` are exported as `<family>_spike_count_30c` / `<family>_spike_count_relative_3`; `median`→`<family>_median_abs_delta`, `spike_intensity`→`<family>_spike_intensity`. Never grep for a bare `spike_count_abs` key — it does not exist in `validation_metrics.csv`. Layer-2's `bt_only > 0` suppression qualifier maps to **`position_pairs_unmatched_bt`** (per-snapshot unmatched-backtest rows — the queue-priority accumulation the C108 example cites), NOT the trade-level `backtest_only_count`.
- **CSV/console**: `export_metrics` appends the six rows contiguous per family (after each `_mean/_max` pair; `pnl_*` after `cumulative_pnl_delta`, before `pnl_correlation` — no pnl mean/max anchor) via `_robust_stat_rows`. `print_summary` adds a grouped `POSITION ROBUST STATS` block (median/p95/max side-by-side) + a `PnL robust:` line. Additive only — no existing row removed/reordered; per-snapshot `position_comparison.csv` unchanged.

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
- **Phase 4 shared DB + surgical wipe + identity bootstrap (features 0049 + 0053)**: Phase 4 default is a **shared SQLite DB** used by gridbot, recorder, replay, and comparator. All four processes must resolve `database_url` to the same physical file; the documented form is `sqlite:////<abs-path>/data/recorder_ltcusdt_phase4.db` (four slashes = absolute), avoiding gridbot CWD-dependent relative resolution. `scripts/phase4/start_recorder.sh` no longer parses YAML in shell — it invokes `scripts/phase4/prepare_recorder_session.py` (thin wrapper around `recorder.prepare_session.main`), which uses `recorder.config.load_config` so `database_url` env-var expansion (`${VAR}`) is honored. Prepare does three things in one pass: (1) **§5.1 + §5.2 surgical wipe** inside a single `BEGIN IMMEDIATE` transaction with `PRAGMA foreign_keys=ON` — §5.1 deletes any rows still stamped with the legacy placeholder `account_id='00000000-...-002'` (one-time 0053 migration; idempotent thereafter); §5.2 broad-deletes `private_executions`, `orders`, `wallet_snapshots`, `position_snapshots WHERE source='live'`, then `runs WHERE run_type='recording'`. The final `runs` delete cascades through `position_snapshots.run_id` and removes `source='backtest'` rows tied to the recording runs (intentional — old replay artifacts). `ticker_snapshots` is **not** wiped — public ticker data has no `account_id` column and is reusable across recorder restarts. (2) **Identity bootstrap (when `account:` set)**: insert-if-missing `User`/`BybitAccount`/`Strategy` rows derived from `--gridbot-config` so the recorder's verify-only `_seed_db_records` succeeds on a clean DB without requiring gridbot to start first. No `runs` row is inserted by prepare — gridbot still creates `run_type='live'`, recorder still creates `run_type='recording'`. (3) **Preflight verify**: runs the same `verify_shared_db_parents` (`apps/recorder/src/recorder/shared_db_parents.py`) the recorder uses — 3 existence + 5 metadata checks. Stale rows with mismatched `environment` / `strategy_type` / `symbol` / ownership fail here (`start_recorder.sh` aborts), not after the recorder has been launched into a guaranteed `_seed_db_records` failure. `BybitAccount.environment` is bootstrapped from gridbot `AccountConfig.testnet` (not recorder top-level `testnet`); the recorder/gridbot testnet config-parity check at prepare time guards against further drift. The wipe does **not** delete the DB file or the `-wal` / `-shm` sidecars. Preserved on each recorder start: `grid_state_snapshots` (gridbot-owned, feature 0047 seed data), `runs WHERE run_type='live'`, `bybit_accounts`, `strategies`, `users`, `ticker_snapshots`. If the DB file does not exist yet, the wipe is a no-op; prepare calls `db.create_tables()` before parent inserts. Helper scripts and runbook snippets that need the recorder identifiers must select the **latest `runs.run_type='recording'`** row — never unfiltered `ORDER BY start_ts DESC LIMIT 1` in a shared DB, since the newest row can belong to live gridbot. `ACCOUNT_ID` must come from that same recording-run row, not from `bybit_accounts LIMIT 1` (which can pick the wrong account when shared setup data contains multiple); after 0053 this `ACCOUNT_ID` is the uuid5 `account_id_for(name)` value, not the legacy placeholder. `public_trades` is **not** part of the wipe — Phase 4 LTCUSDT runs with `capture_public_trades: false`. Recorder credentials in Phase 4 configs are `${BYBIT_READONLY_API_KEY}` / `${BYBIT_READONLY_API_SECRET}` (read-only), distinct from gridbot's live `${BYBIT_API_KEY}` / `${BYBIT_API_SECRET}` (trade-permission). Resolved by feature 0052: replay's `grid_state_snapshots` lookup is cross-run by `(account_id, strat_id, symbol)`, so the recorder/gridbot `run_id` divergence no longer drops the DB grid-state seed (see the Grid State DB snapshots section for the contract).

- **Phase 4 recorder startup — snapshot sentinels are the shell contract (feature 0055)**: `start_recorder.sh` must classify the initial REST snapshot result via terminal sentinels emitted by `recorder.py:_write_initial_rest_snapshot`, not by grepping the human-readable `Initial REST snapshot:` INFO line. The recorder emits the INFO line *before* the WARNING on the zero-count failure path (lines 308–326), so a wait loop that breaks on the INFO line races the failure and can declare success on an incomplete snapshot. Sentinels: `RECORDER_SNAPSHOT_OK` (wallet_count > 0 AND position_count > 0) and `RECORDER_SNAPSHOT_INCOMPLETE` (auth-client construction failure OR zero wallet/position rows). Two shell libs back this contract — both side-effect-free at top level so pytest can source them: `scripts/phase4/lib/recorder_snapshot_check.sh` (`_classify_recorder_snapshot` returns 0/1/2 for OK/INCOMPLETE/timeout) and `scripts/phase4/lib/recorder_stop.sh` (`_stop_recorder_pattern PATTERN [WAIT_SECONDS]` SIGINTs by `pkill -f`, polls `pgrep -f`, returns 0 on clean shutdown / 1 with a `ps aux` diagnostic on stderr if still alive). On INCOMPLETE or 15s timeout the launcher calls `_stop_recorder_pattern "recorder --config $CONFIG"` (pattern, not `$RECORDER_PID` — the `uv` wrapper PID may shadow the Python child), waits up to 10s, and exits non-zero with no `Recorder PID:` tail. Same helper is used for the stop-prior-recorder block at script start, so kill+verify lives in exactly one place. Adding new exit paths in `_write_initial_rest_snapshot` requires emitting one of the two sentinels — otherwise the wait loop hangs to timeout. The launcher process-management branches (kill on incomplete, kill on timeout, manual-intervention on stuck shutdown) are covered by `apps/recorder/tests/test_start_recorder_check.py:TestStopRecorderPattern` via bash-function stubs (`pkill`/`pgrep`/`sleep`/`ps` defined before sourcing the lib — function lookup wins over PATH). `start_recorder.sh` itself is still not sourced from pytest (top-level side effects); `TestStartRecorderLauncherIntegration` instead asserts the launcher sources the lib and calls the helper.

### Recorder-specific test pitfalls

1. **TickerEvent fields**: Does NOT have `index_price` or `next_funding_time` — check `gridcore.events.TickerEvent` dataclass definition before constructing test fixtures.
2. **Mock collectors need `stop = AsyncMock()`**: When mocking `PublicCollector`/`PrivateCollector`, must set `stop` as `AsyncMock()` since `Recorder.stop()` awaits them.
3. **`_close_dangling_coro()` pattern**: When testing `cli()` that calls `asyncio.run(main(...))`, the mock creates an unawaited coroutine. Use the helper to close it after assertions (same pattern as gridbot `test_main.py`).
4. **Testnet default differs**: Recorder defaults to `testnet=False` (mainnet), unlike gridbot which defaults to `testnet=True`.
5. **Position/wallet test data format**: `PositionWriter` and `WalletWriter` expect Bybit-formatted dicts with `"data"` keys (e.g., `{"data": [{"symbol": "BTCUSDT", ...}]}`). Flat dicts silently produce zero snapshots.
6. **Test fixture deduplication**: Shared `db` fixture lives in `conftest.py` — do not duplicate in individual test files. Same for `basic_config` and `config_with_account`.
7. **Mock config completeness**: When using `MagicMock()` for config in tests, set all attributes that `main()` accesses before the code path under test. E.g., `mock_config.database_url = "sqlite:///test.db"` — bare MagicMock attributes break `urlparse()`.

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
- **Position telemetry parity (feature 0034)**: backtest emits `position_snapshots` rows with `source='backtest'` on every fill (including wind-down close-outs). Comparator pairs them per-side with `source='live'` rows (monotonic two-pointer, 5s tolerance, one-to-one consume invariant) and recomputes unrealized PnL from `live.mark_price` so the delta is apples-to-apples. Twelve metrics added to `ValidationMetrics`; `position_comparison.csv` emitted when at least one pair exists. Un-migrated DBs raise loudly at load time — do NOT silently mask as zero-pair.
- **UTA wallet balance semantics (feature 0042)**: `wallet_snapshots` stores account-level UTA fields alongside per-coin rows: `total_equity`, `total_available_balance`, `total_margin_balance`, `account_im_rate`, `account_mm_rate`. Account rates are raw Bybit decimal ratios, not percentages. Replay seeds `BacktestSession.initial_balance/current_balance` from `WalletSeed.total_available_balance` when present, not per-coin `wallet_balance`. That `current_balance` flows through more than liquidation: order margin gating, wallet-fraction qty sizing, margin-ratio logging, and risk multiplier state all see the UTA account-level available-balance baseline. Legacy rows with `total_available_balance IS NULL` fall back to config `initial_balance`.
- **Position-value parity (feature 0059/0060)**: `position_snapshots.position_value` is the fourth USDT field the 0058 log line emits. **Live**: writers store Bybit `positionValue` verbatim (= `|size| × mark_price`; `not in (None, "")` guard unchanged). **Backtest snapshot** (0059 parity / `position_snapshots.position_value`): mark-based `abs(size) * mark_price` computed inline in `BacktestRunner._emit_position_snapshot` (feature 0060) — NOT `tracker.state.position_value`. **Backtest local margin path**: `tracker.state.position_value` stays entry-based (`calc_position_value` via `_update_margin`) for local IM/MM and the risk margin ratio. Bybit UTA IM uses mark + hedge (`RULES.md:2184`); our `calc_initial_margin` uses entry-based notional — known mismatch, out of scope. Flat backtest snapshots use explicit `Decimal("0")` (not a stale read). Comparator adds per-snapshot `upnl_usdt_delta` (stored unrealised 1:1, distinct from the mark-recomputed `unrealised_pnl_delta`) and `pos_value_delta` (≈0 after 0060; was ≈ per-side unrealized PnL). Nine `ValidationMetrics` aggregates (cur/cum `_usdt_*` reuse the existing per-pair deltas via `_agg`). NULL `position_value` is NULL-safe and does NOT trip `has_missing_telemetry` (mirrors the 0056 `cur_realised_pnl` exclusion). Migration: `scripts/migrate_0059_position_value.py --database-url ...` (idempotent); fresh DBs get the column via `create_all()`. Pre-0060 backtest rows keep entry semantics; re-run backtests for parity (no backfill).

### Position telemetry repository contract (feature 0034)

- `PositionSnapshotRepository` read methods accept a `source: str | None = 'live'` parameter. **When adding a new read method, default to `'live'`** so legacy callers never silently mix in backtest rows. Pass `'backtest'` for backtest rows or `None` for the union (the comparator is the only legitimate `None` caller).
- `position_snapshots` has a CHECK constraint `source IN ('live', 'backtest')` (Postgres) plus a B-tree index `(run_id, account_id, symbol, side, source, exchange_ts)`. Equality predicates precede the `exchange_ts` range — do not reorder.
- One-off SQL migration for existing DBs: `scripts/migrate_0034_position_telemetry.py --database-url ...`. Fresh DBs get the columns via `Base.metadata.create_all()`.

### Fill simulator modes

3. **Replay Fill Simulator Modes**
   - `strict_cross`: conservative trade-tape model. BUY fills only below limit; SELL fills only above limit. Used as `BacktestEngine` default and as opt-in for replay backward-compat baseline.
   - `trade_through_at_limit`: last-price model that includes exact limit touches (`<=` / `>=`).
   - `book_touch`: parity mode using recorded L1 (`ask1 <= limit` for BUY, `bid1 >= limit` for SELL), falling back to `trade_through_at_limit` for legacy bare-price callers. Was the replay default through features 0033–0050; kept as opt-in for legacy L1-touch parity.
   - `last_cross` (**replay default since feature 0051**): transition-based aggressor detection. BUY fires when `prev_last > limit_price` AND `curr_last <= limit_price`; SELL fires when `prev_last < limit_price` AND `curr_last >= limit_price`. Strict inequality on `prev_last` — `prev_last == limit_price` does **not** count as a cross. Sticky `last_price` (`prev == curr`) never fires. First-ever observation of a symbol returns `False` (no prior tick). Legacy bare-`Decimal` input on `check_fill` returns `False` for `LAST_CROSS` and does not mutate any state slot (no symbol/exchange_ts available to key the per-tick advance). v7 A/B re-validation cut fill-timing `|delta|` from 19.0s (`book_touch`) to 5.1s at match_rate=100%, closing issue #117's +12.6s lag.
   - `event_follower` (feature 0072, issue #168): fills sourced from recorded live `private_executions` instead of the per-order simulator — recorded `exec_price`/`exec_qty`/`exec_fee`/`closed_pnl` are applied **as-is** (never recomputed; Bybit's `closed_pnl` is authoritative for the wallet, the tracker keeps only size/entry). Dispatched as a pre-tick injection in `BacktestRunner.process_fills` (`self._event_follower is not None` branch) — `TradeThroughFillSimulator.check_fill` raises if ever consulted under this mode. Executions are consumed in `(exchange_ts, exec_id)` order (sorted by `PrivateExecutionRepository.get_by_run_range` — single sort site) via a forward-only monotonic cursor (`EventFollower.drain`); the within-tick drain is iterative to a fixpoint so a reactive close placed mid-window (via a synthetic ticker at the fill's `exchange_ts`) matches the close execution from the same window. Matching is key-faithful on `extract_client_order_prefix(order_link_id) == client_order_id`, with `order_id` then side/closest-price fallbacks for pre-hotfix rows. Partial fills aggregate per `(matcher_key, recorded_order_id)` — one `BacktestTrade` per order lifecycle, mirroring `LiveTradeLoader._aggregate_fills`; in-flight partials live in `session.set_pending_wallet` until a flush trigger fires (1 full-fill intra-loop; 2 last-in-stream post-fixpoint; 3 cancel in `_dispatch_intents` before `execute_cancel`; 4 end-of-replay `finalize_event_follower()` called by `engine.py` before wind-down/finalize). Consequences: `backtest_only` is structurally `0`; `live_only` = intent-set divergence from live (not simulator misses); the mode answers "how would strategy A vs B have dispatched live's exact fills" — it cannot model fills the strategy never placed an order for, new market regimes, or latency. Recorded qty above replay's placed qty is capped (`qty_excess_divergence` counter; fee/pnl pro-rated). Engine materializes ORM rows to `RecordedExecution` dataclasses inside the DB session (DetachedInstanceError, cf. 0038) and skips other-symbol rows (`get_by_run_range` does not filter symbol).
   - `advance_market(market: TickerEvent)` contract (feature 0051): `BacktestOrderManager.check_fills` calls `self.fill_simulator.advance_market(market)` as the first statement inside the `isinstance(market, TickerEvent)` branch, before the per-order loop. Runs unconditionally on every `TickerEvent` (including orderless ticks) so the `T -> T+1` transition signal is preserved when no order is active for the symbol. The legacy bare-`Decimal` `else`-branch never calls `advance_market`. The simulator owns three per-symbol state dicts: `_prev_last_price` (committed prior-tick value), `_tick_prev_last` (read slot for the in-flight tick), and `_tick_token` (idempotency guard keyed on `(symbol, exchange_ts, local_ts)`). Repeated calls within a tick are no-ops. `_should_fill_last_cross` reads only `_tick_prev_last` and never writes.
   - **Test fixture timestamp discipline (pitfall, feature 0051)**: every snapshot in a multi-tick `LAST_CROSS` test MUST carry monotonically distinct `(exchange_ts, local_ts)` values. Reusing one timestamp across two snapshots makes the second `advance_market` call token-match the first and silently no-op — the stash never runs, `_tick_prev_last[symbol]` stays `None`, and the test asserts `False` on every cross. The `_ticker` / `_ticker_for` helpers in `apps/backtest/tests/test_fill_simulator.py` accept a `tick_index: int` parameter that offsets both timestamps by `timedelta(milliseconds=tick_index)`. Pass `tick_index=0` for prev and `tick_index=1` for curr. Production replay is protected from this collision by the DB unique constraint on `(symbol, exchange_ts)` at `shared/db/src/grid_db/models.py:265-267`; tests do not go through that path.
   - Default split: `apps/replay` defaults to `last_cross` (timing-accurate transition detection; v7 A/B vs `book_touch` showed 19.0s → 5.1s fill-timing improvement at 100% match-rate); `BacktestEngine` keeps `strict_cross` because forward backtest data sources may lack the L1/last-price-history required by the parity-oriented modes.
   - `BacktestOrderManager.check_fills(TickerEvent(...))` is always scoped to the ticker's own symbol; the legacy bare-Decimal path preserves all-symbol scanning when no `symbol` filter is supplied.
   - Rationale: production backtests keep conservative semantics; replay parity smoke benefits from the richer bid/ask already stored in `ticker_snapshots`.

### Replay-specific test pitfalls

1. **InMemoryDataProvider for tests**: Use `data_provider=` parameter in `engine.run()` to bypass DB reads — avoids needing real TickerSnapshot rows in test DB.
2. **InstrumentInfoProvider must be mocked**: Tests use `@patch("replay.engine.InstrumentInfoProvider")` — the provider tries to fetch real instrument info otherwise.
3. **Run resolution needs full FK chain**: When seeding test DB for run resolution tests, must create User → BybitAccount → Strategy → Run (foreign key constraints).
4. **`datetime.fromisoformat()` requires Python 3.11+** for full timezone offset support. Earlier versions don't handle `+00:00`.
5. **Test for `ValidationError` not `Exception`**: Pydantic config validation tests should use `from pydantic import ValidationError` for specific assertions.

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
- **Division guard constants**: `MIN_POSITION_IM` and `MIN_LEVERAGE` in `calculator.py` prevent division by near-zero values. Warnings are logged when these guards activate.
- **Symbol validation**: `_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{4,20}$")` in `config.py`. Bybit symbols are uppercase alphanumeric only.
- **`get_transaction_log_all()` return type**: Returns `tuple[list[dict], bool]` — the bool indicates whether data was truncated at `max_pages`. Callers must handle the truncation flag.
- **Config redaction**: `_redact_config()` in `reporter.py` replaces API credentials with `[REDACTED]` before writing to JSON output. Never serialize raw `AccountConfig` to files.
- **Tolerance scaling for percentages**: PnL % values are 100x USDT values. Use `PERCENTAGE_TOLERANCE_MULTIPLIER = 100` in `comparator.py` to scale tolerance for ROE comparisons.
- **Test coverage**: Currently at 92%. Run: `uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -v`
- **Workspace dependency**: `pnl-checker` must be in root `pyproject.toml` dev deps AND `tool.uv.sources` for test discovery to work.

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
20. **`Decimal("")` raises `decimal.InvalidOperation`**: Bybit may send empty strings for unused/dust numeric fields on mainnet UTA (e.g. `walletBalance`, `availableToWithdraw`). `d.get(key, "0")` only handles *missing* keys, not present-but-empty. For non-nullable Decimal columns use a `_decimal_or_zero(value)` helper (predicate `value in (None, "")`, fallback `Decimal("0")`); for nullable columns use the 0034 recorder pattern `Decimal(str(v)) if v not in (None, "") else None`. See `apps/event_saver/src/event_saver/writers/wallet_writer.py:17-34` and `apps/recorder/src/recorder/recorder.py:450-453`. Truthiness checks (`if v:`) are wrong here because they conflate legitimate `"0"` with empty.
21. **Bybit V5 wallet WS frame puts the exchange timestamp at `msg["creationTime"]` (ms, frame top), not inside `data[i]["updateTime"]`**. Pre-V5 / V3 fixtures still set the inner `updateTime`. `wallet_writer._resolve_exchange_ts` tries `updateTime` first, then frame `creationTime`, then `local_ts` as a guard — never falls to epoch. Don't read `wallet_data["updateTime"]` directly on real V5 traffic; it is missing and `int(None or 0) = 0` silently produces 1970-01-01, which then sorts below the single REST snapshot in `WalletSnapshotRepository.get_latest_before` and freezes 0042 seed lookups on the recorder-start balance.
22. **Hedge-mode liquidation (feature 0043) is computed pair-shaped, not per-leg.** **TL;DR:** call `BacktestRunner._estimate_pair_liq_prices(long_state, short_state, total_equity) -> (liq_long, liq_short)`. Pool input is `BacktestSession.total_equity` (NOT `current_balance`). MM is `calc_maintenance_margin(L_pv + S_pv, symbol, tiers)` (full tier-MMR on combined notional, NOT the sum of per-leg `positionMM`). The over-hedged smaller leg is `0` by construction. Three non-obvious choices (derived from 13 mainnet validation snapshots, see `docs/features/0043_PLAN.md` Phase 2): (a) pool input is `BacktestSession.total_equity` (UTA `totalEquity`), **not** `current_balance` / `totalAvailableBalance` — `total_available_balance` undershoots live by 30-45 USDT in net configurations; (b) MM term is `calc_maintenance_margin(L_pv + S_pv, symbol, tiers)` — **full** tier-MMR on the combined notional, not the sum of per-leg `position_mm` from Bybit's WS payload (Bybit publishes the smaller leg's MM with a hedge discount but reverts to full MMR internally for liq calc); (c) `_process_fill` calls `session.refresh_balances(post_fill_unrealized)` so the emitted parity snapshot, risk multipliers, and log all see synchronous post-fill equity instead of the previous tick's value. Don't reintroduce a per-leg `_estimate_liquidation_price` — the pair function is the only liq formula in the codebase.
23. **PositionComparator state-consistency filter (feature 0044).** Pairs matched by exchange_ts but where backtest state has drifted from live (size delta > `state_size_tolerance` default 0.001, or relative entry drift > `state_entry_rel_tolerance` default 0.001 = 0.1%) are flagged `state_diverged=True` on the `PositionComparisonPair`, counted in `ValidationMetrics.position_pairs_state_diverged`, and **excluded** from `liq_price_*` / `position_im_*` / `position_mm_*` / `unrealised_pnl_*` aggregates. They still appear in `position_comparison.csv` (with the `state_diverged` column = `1`) for diagnostic inspection. Why: operator manual fills, missed grid orders, and other state-divergence artefacts otherwise pollute the headline `liq_price_max_abs_delta` metric. Re-validating 0043 on the original noisy DB dropped the metric from 17.77 USDT (dominated by 2 manual-intervention outliers) to 0 USDT. Tolerances are constructor kwargs — relax via `PositionComparator(state_size_tolerance=Decimal("2.0"), ...)` for runs where you want to compare states that drifted by more than a step. See `docs/features/0044_PLAN.md`.

24. **Hedge-aware `positionIM` / `positionMM` on `PositionSnapshot` (feature 0045) — inline pair helper, no per-leg primitives on the emission path.** **TL;DR:** in `BacktestRunner._emit_position_snapshot`, call `self._estimate_pair_im_mm(long_state, short_state, mark_price) -> (im_long, mm_long, im_short, mm_short)` inline and pick the leg matching the snapshot direction. Do **not** reintroduce `calc_initial_margin(L_pv, ...)` / `calc_maintenance_margin(L_pv, ...)` on the snapshot path — those primitives omit Bybit's fee-to-close component (a ~0.23 USDT single-leg gap) AND the hedge cross-credit (a ~1 USDT paired-hedge gap). They stay in `gridcore.pnl` for non-snapshot callers (`pnl_checker` and other pure single-leg sites). Three non-obvious choices, derived from 10 paired LTCUSDT mainnet snapshots + Bybit help-center docs (see `docs/features/0045_PLAN.md` Phase 1 and `docs/features/0045_REVIEW.md`): (a) `positionIM` / `positionMM` returned by Bybit's `/v5/position/list` **include the estimated fee-to-close** — `fee_long = L_size × L_entry × (1 − 1/leverage) × taker_rate`, `fee_short = L_size × L_entry × (1 + 1/leverage) × taker_rate`; (b) the dominant leg's MM uses **only the unhedged portion** at the leg's full-pv tier (`max((L − S) × mark × MMR_tier − deduction_tier, 0)`) — the hedged portion contributes zero to the dominant leg's published MM because Bybit cross-credits it to the smaller leg; the `deduction_tier` term carries through to keep the per-tier MM formula continuous at tier boundaries (matters once any leg crosses tier 1, e.g. LTCUSDT pv ≥ 200k); (c) the smaller (fully hedged) leg has **no `pv × MMR` baseline term** — it publishes `fee_to_close_smaller + MMR_tier × hedged_size × |L_entry − S_entry| × C`, where `C ≈ 5.657` is an empirical Bybit hedge buffer factor whose closed form is not yet documented and needs per-symbol calibration. Config inputs live on `BacktestStrategyConfig.taker_fee_rate` (default 0.00075) and `BacktestStrategyConfig.hedge_smaller_buffer_factor` (default 5.657, LTCUSDT @ 10x). The pre-0045 `calc_initial_margin` / `calc_maintenance_margin` on the snapshot path was wrong even for single-leg cases (missed fee-to-close); the new helper closes the gap for both single-leg and paired-hedge configurations. Single consumer per emit — call inline, no precompute / kwargs threading (contrast with 0043 liq, where two consumers in `_process_fill` justify a precomputed pair).

25. **Non-USDT collateral re-marking on backtest `total_equity` (feature 0065).** When the trader holds non-USDT spot (e.g. SOL) as UTA collateral, live `totalEquity` floats with that coin's mark while a pre-0065 backtest stayed anchored to the seed snapshot — surfacing as `liq_price_*`/`position_im_*`/`position_mm_*` parity drift (those metrics read `BacktestSession.total_equity`, see entry 22). 0065 adds a collateral re-mark **delta** to `total_equity` ONLY. Non-obvious points:
    - **Bybit `totalEquity` excludes the collateral value ratio** — the re-mark term is the FULL asset USD value `Σ balance × mark` (anchored: `+ (collateral_now − Σ balance × seed_mark)`), with **no** ratio/haircut. The ratio applies to *margin balance*, not `totalEquity`. `SeedConfig.collateral_value_ratios` is stored for a FUTURE margin-parity feature and is **not** applied here.
    - **Term moves `total_equity` only.** `current_balance`, `equity_curve`, and `finalize().final_balance` stay available-based (`initial_balance + pnl_delta`) so executor sizing/qty/PnL/fee parity is byte-identical. Applied in BOTH `BacktestSession.update_equity` AND `refresh_balances` (the latter is what `process_fills` calls before reading `total_equity` for the emitted pair-liq snapshot — omitting it there lags the metric one collateral step on fill rows).
    - **Mark feed runs at the TOP of the engine tick loop**, before `process_fills` (`engine.py` `CollateralMarkFeed.mark_at(coin, tick.exchange_ts)` → `session.update_collateral_mark`). The traded-symbol provider does not carry collateral ticks; the feed streams each coin's `*USDT` `ticker_snapshots` with carry-forward at-or-before semantics and a monotonic per-coin cursor (assumes non-decreasing tick ts).
    - **Seed-mark valuation basis (load-bearing):** `load_collateral_seed` uses `usdValue / wallet_balance` ONLY when the per-coin wallet row is FRESH vs `at_ts` (`_strip_tz(at_ts) − _strip_tz(row.exchange_ts) ≤ collateral_wallet_max_staleness`, default 60s); else it falls back to `TickerSnapshotRepository.get_mark_at_or_before(symbol, at_ts)`. Reason: Bybit's UTA wallet WS pushes only CHANGED coins, so a quiet collateral coin carries a stale `usdValue` while `initial_equity` (USDT row) is current — using the stale ratio would inject a false jump on the first tick. Use the module-level `_strip_tz` in `snapshot_loader.py` (engine's is nested in `_seed_pre_check`).
    - **Inclusion gate is the operator list + `wallet_balance > 0`, never the booleans.** `collateralSwitch` / `marginCollateral` in `raw_json` are **booleans** ("usable as collateral"), recorded in `collateral_switch_off_coins` + WARN when off but the coin is STILL re-marked (totalEquity ignores the switch). No row / **non-positive** balance (zero OR negative — a negative/borrowed balance is not spot collateral and would invert the drift) → `collateral_excluded_coins`; balance row but no usable mark → `collateral_missing_mark_coins` (dropped from all model dicts). Returned `coin_balances`/`seed_marks` contain ONLY fully-modelled coins.
    - **`CollateralMarkFeed` anchors each coin's carry-forward to the latest mark in `[seed_at_ts, start_ts]`** in `__init__` (`_anchor_mark`), so the first replay tick uses a correct carry-forward mark when `seed.at_ts < start_ts` or the symbol has no row exactly at `start_ts` (sparse stream). **Floored at `seed_at_ts`** — a mark from BEFORE the seed anchor is NOT applied (it would be false backward drift vs the `at_ts` seed mark); no mark in the window → keep the seed mark. The forward generator then streams rows `>= start_ts`; `mark_at` is forward-only and **raises** on non-monotonic ts.
    - **`seed.collateral_coins` non-empty HARD-REQUIRES a wallet seed** — `engine._load_seed` raises `SeedDataQualityError` (not the soft-fallback to `initial_balance`) when `load_wallet_seed_full` returns None, because the re-mark is anchored to live `total_equity` at `at_ts`. Merge is via `dataclasses.replace` onto the (frozen) `WalletSeed` inside the same DB session.
    - **Recorder:** `RecorderConfig.collateral_symbols` adds the coin's `*USDT` perp to the PUBLIC ticker subscription only (de-duped with `symbols`, merged in `_init_collectors` via `dict.fromkeys`); private/executions/positions stay scoped to `symbols`. Only affects NEW recordings.
    - **#3a integration tests MUST use `wind_down_mode: leave_open`** — `_wind_down` (close_all) does not call `refresh_balances`/`update_equity` and `finalize()` does not rewrite `total_equity`, so read `session.total_equity` after the tick loop, NOT post-`finalize()`. Static-balance limitation: `coin_balances` are frozen at seed; live deposits/withdrawals/spot-trades of the coin during the window are un-modelled (#3a WARN). Empty `collateral_coins` → term is identically zero (USDT-only no-op, acceptance #4).
    - Real-data attribution: `scripts/verify_0065_collateral.py` shows `balance × (mark_end − mark_seed)` against live `totalEquity` drift on a recorder DB. See `docs/features/0065_PLAN.md`.

26. **`claude-code-action` workflow file must match default branch** — The `.github/workflows/claude-code-review.yml` on a PR branch must be identical to the version on `main`. Modify it on `main` first, then all future PRs pick it up. If you change it on a feature branch, the OIDC token validation fails with "Workflow validation failed."
27. **`_margin_ratio` in calculator.py** — Distinguishes `pos is None` (no position, returns 0 silently) from `wallet_balance <= 0` (logs warning then returns 0). This aids debugging when wallet data is missing.
28. **`grid.py __center_grid` rebalancing** — `lowest_buy_price` must be tracked in the loop (not just initialized from `grid[0]`). After `update_grid` changes sides, grid[0] may be WAIT, not BUY. Fixed 2026-04-11.
29. **f-string division in `runner.py _process_fill`** — Decimal division by `session.current_balance` inside f-strings crashes even when debug logging is disabled. Always guard balance divisions with `> 0` check outside the f-string. Fixed 2026-04-11.
30. **`reconciler.py` public trade reconciliation** — Bybit's `/v5/market/recent-trade` only returns the most recent trades; it does NOT support time-range queries. The reconciler logs a warning when fetched data doesn't cover the gap. Execution reconciliation (`get_executions_all`) correctly passes `start_time`/`end_time`. Fixed 2026-04-11.
31. **`runner.py _execute_intents` stale limits snapshot** — `_execute_intents()` must refresh the `limits` snapshot after each successful `_execute_place_intent()` call. Without this, multiple reduce-only intents in the same batch all check against the same stale snapshot, over-covering the position and causing Bybit 110017 reduce-only rejections. Path: `apps/gridbot/src/gridbot/runner.py`. Fixed 2026-04-11.
32. **Backtest `_should_place_close` must resolve intent qty** — Engine emits `qty=0`; the gate must resolve it via `executor.qty_calculator` before checking `pos_size > (pending + intent_qty)`. Without this, the backtest gate is weaker than live `_is_good_to_place()` and allows over-closing positions. Path: `apps/backtest/src/backtest/runner.py`. Fixed 2026-04-11.
33. **Backtest `_apply_risk_to_qty` must re-round after multiplier** — Base qty is rounded to `qty_step`, but multiplying by the risk multiplier can produce sub-step values (e.g., 0.001 * 0.5 = 0.0005). Must call `instrument_info.round_qty()` after multiplying, matching live `_resolve_qty`. Path: `apps/backtest/src/backtest/runner.py`. Fixed 2026-04-11.

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
16. **In-process lock registry location**: `_IN_PROCESS_LOCKS` and `acquire_in_process_lock` / `release_in_process_lock` live in `cache_lock.py`, not `risk_limit_info.py`. Integration tests that assert ref-counts must import `backtest.cache_lock`; oversized-cache warnings log `CacheSizeExceededError` text (`"Cache file size"` … `"exceeds"`), not the legacy `"Cache file exceeds"` substring.

## Reference Code

- Location: `bbu_reference/bbu2-master/`
- Keep for comparison tests; never modify
- **WARNING**: Contains bugs (e.g., short position liq risk logic)

## Docs

Feature documentation lives in `docs/features/` — see `0001_IMPLEMENTATION_SUMMARY.md`, `ORDER_IDENTITY_DESIGN.md`, `0003_FIXES.md`, `0008_PLAN.md`, `0009_PLAN.md`, etc.

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
