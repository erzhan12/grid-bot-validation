---
paths:
  - "packages/gridcore/**"
---

## gridcore — Pure Strategy Engine

**Path**: `packages/gridcore/` | **Dependencies**: ZERO external

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
- **InstrumentInfo** lives in `gridcore/instrument_info.py` (shared by backtest, replay, gridbot). **InstrumentInfoProvider** (fetcher) lives in `packages/bybit_adapter/src/bybit_adapter/instrument_info.py` (moved from backtest in 0090); apps import it from there.
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
    (where realized PnL lands) off the per-cycle `curRealisedPnl` sum read
    directly from the raw long/short WS payloads (`runner._cur_realized_pnl_from_raw`),
    NOT from `_build_position_state` (which returns `None` at `size == 0` and
    would miss the closing-fill loss). Uses the Bybit UI "Realized", NOT the ~80x
    lifetime `cumRealisedPnl`. On trip it is a
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
- **Retry-queue cap enforcement (critical)**: place retries are dispatched via
  `StrategyRunner.retry_dispatch_place` (orchestrator wires this in
  `_init_strategy`), which re-applies the 110017 truncate breaker (Step 2) and
  C1/C2/C3 before calling the executor — the executor alone only enforces C4. On
  C3 trip the retry queue is **cleared** (mirrors auth-cooldown).
  `RetryQueue.is_paused` also gates on `loss_tripped()`. Cap-blocked retries
  return `error.startswith("safety_cap")` and breaker-blocked retries return
  `error="truncate_breaker_blocked"`; both are **dropped** from the queue (not
  re-backed-off).
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

- **Mechanics**: duplicate orders at the same price level soft-block ALL new placements (liquidation guard). Separate per-direction buffers (deque maxlen=2, matches bbu2); only fully filled orders (`leaves_qty == 0`) enter; closing trades detected via `closed_size != 0` (not `closed_pnl`). The engine always runs — only `_execute_intents()` is gated by `_same_order_error`; BOTH sides are checked on every execution event; auto-recovers on a clean fill at a different price.
- `StrategyRunner._check_same_orders_side()` compares tracked orders by `TrackedOrder.placed_ts` when both fills map to tracked orders; use fill `exchange_ts` only as a fallback for untracked/legacy events. Duplicate same-price orders can rest concurrently and fill more than 5s apart in thin markets, so fill-time-only windows silently miss the critical duplicate-placement bug.
- **Dedup + auto-recovery (feature 0031)**: `_same_order_dedup_cache` is the single dedup mechanism, keyed by `frozenset` of the two exchange order_ids in the SAME ORDER pair. Each entry carries `first_seen_ts`, `last_seen_ts`, and `verdict ∈ {WS_GLITCH_SUSPECTED, REAL_DUPLICATE, UNKNOWN}`. TTL is `_SAME_ORDER_DEDUP_TTL_SEC = 21600` (6 h), sized to comfortably exceed the 3 h 24 min retrigger gap from the 2026-05-09/10 incident. Don't reintroduce a separate rate-limit set — both REST-rechecking and dedup share this key.
- The dedup gate inside `_check_same_orders_side` is **verdict-aware**: `WS_GLITCH_SUSPECTED` retriggers are silently suppressed (DEBUG log only) **and must also set `_drop_phantom_event_for_current_call = True`** so `on_execution` drops the phantom replay end-to-end (no `mark_filled`, no engine, no place); `REAL_DUPLICATE` retriggers are silenced too but **must explicitly re-set `_same_order_error = True`** because `_check_same_orders` resets the flag at the top of every call — without this re-set the dedup gate would silently lift a legitimately-latched block. Do NOT set the phantom-drop flag for REAL_DUPLICATE: the event is real (REST saw the fill); `mark_filled` and `engine.on_event` must run normally on it. `UNKNOWN` falls through to the full first-trigger path. The first-trigger path inserts an `UNKNOWN` cache entry **before** running REST cross-check, so a same-event burst is suppressed by the gate on events 2..N.
- `verdict == "WS_GLITCH_SUSPECTED"` from `_diagnostic_rest_check_executions` auto-clears the soft-block via `reset_same_order_error(emit_recovery_info=False)` (clears flag + both per-side execution buffers + throttle state), but only after a paginated, time-bounded REST execution slice completes without truncation and sees exactly one of the two order_ids. A truncated/empty/incomplete REST slice is `UNKNOWN` and must leave the block latched; do not treat absence from a single recent page as proof of a phantom. The auto-clear path emits exactly **one** INFO log carrying both verdict context and, when at least one throttled `Same-order error active` WARNING was emitted during the latched period (feature 0046), a trailing `; suppressed N WARNINGs since` suffix — no `notifier.alert(...)` because `Notifier.alert()` always logs at ERROR level (`notifier.py:67`) and a successful recovery must not produce an ERROR-level line. The `emit_recovery_info=False` argument suppresses the generic recovery INFO from the reset helper so there is no double-log. `REAL_DUPLICATE`, `UNKNOWN`, and the REST-exception path leave the block latched. `reset_same_order_error()` does **not** clear the dedup cache, by design.
- **WARNING throttle (feature 0046, issue #94)**: while the soft-block is latched, `on_ticker` emits the `Same-order error active, skipping order placement` WARNING using a per-runner instance throttle (`_same_order_warn_last_ts`, `_same_order_warn_suppressed`) with `_SAME_ORDER_WARN_THROTTLE_SEC = 60.0`. Cadence: loud-first WARNING on entry → suppress within the window → at most one heartbeat re-emit per window with `(suppressed N since last)` suffix. On True→False transition, exactly one INFO line summarises the suppression window — emitted only when at least one WARNING fired during the latched period (silent latch + silent clear emits no INFO). Reset side-effects (`_same_order_error`, execution buffers, throttle counters) are owned by `reset_same_order_error()`; the REST WS-glitch auto-clear path passes `emit_recovery_info=False` to merge verdict + suppressed-count into a single INFO line. The clean-fill auto-clear path inside `_check_same_orders` does **not** route through the reset helper (it must preserve execution buffers); it snapshots `was_set` before the per-side checks and calls `_emit_clear_recovery_if_needed()` only on a confirmed True→False net transition. A 1-hour soft-block emits ≤ 61 WARNING lines (1 loud + ≤ 60 heartbeats).
- **Phantom event drop on the live execution path**: the auto-clear path also sets `_drop_phantom_event_for_current_call = True`. `on_execution` resets this flag at entry and, when set, returns BEFORE `self._engine.on_event(event)`, BEFORE `_execute_intents(...)`, **and BEFORE `tracked.mark_filled()`**. The order in `on_execution` is: exec_id dedup guard (feature 0083) → lookup tracked → run `_check_same_orders` → drop-check → only then `mark_filled` + engine + place. Engine must NOT see the phantom fill (would corrupt grid/position state and contaminate every subsequent tick — e.g., mark the slot as walked, fail to re-place at that price, double-count when the real resting order eventually fills). The tracked order must NOT be flipped to `filled` either — `get_limit_orders()` filters `status not in ("placed",)` at line 324, so a phantom-marked-filled order silently disappears from the live in-memory book and the reconciler then operates on a stale view. The "update grid state regardless of error" convention applies only to the "we're not sure" case; once REST has produced an authoritative `WS_GLITCH_SUSPECTED` verdict, drop the event end-to-end. `on_ticker` and `on_order_update` do NOT consult this flag (it is scoped to the current execution event).
- `Notifier.alert(error_key=...)` only throttles Telegram delivery (`_DEFAULT_THROTTLE_SECONDS = 60` at `notifier.py:18`); `logger.error("ALERT: ...")` always fires. To suppress the log line itself, dedup must happen upstream of the notifier, not via `error_key`.
- **Exec-id redelivery guard (feature 0083, issue #202)**: single-`exec_id` WS resync redelivery bursts (same execution replayed N times; SAME ORDER never fires because there is no different-oid pair) are deduped by `_seen_exec_id` / `_processed_exec_ids` — a bounded FIFO `OrderedDict` capped at `_EXEC_DEDUP_MAX_ENTRIES = 4096`, **no TTL/clock**: Bybit `exec_id`s are globally unique, so time-expiry would only re-admit late replays. The guard sits at the **top** of `on_execution`, *before* `_check_same_orders` — load-bearing, not stylistic: `_check_same_orders` appends every full fill into the maxlen=2 buffers and resets `_same_order_error` before re-evaluating, so a redelivery burst reaching it would evict a genuine different-oid pair and spuriously clear a latched SAME ORDER error. Empty `exec_id` is never deduped. Division of labor: 0031 handles different-oid REST-adjudicated phantom pairs, 0083 handles same-`exec_id` replays — they don't overlap, and a phantom's first-sighting `exec_id` being recorded by the guard is harmless (its redelivery should be dropped anyway; a later genuine fill carries a fresh `exec_id`). The recorder DB path needs no change — `PrivateExecutionRepository` is independently `exec_id`-idempotent via `on_conflict_do_nothing`.

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

## PnL Calculation Functions (`packages/gridcore/src/gridcore/pnl.py`) — Added 2026-02-24

Pure PnL calculation functions extracted into gridcore as the single source of truth.

**Functions exported from gridcore:**
- `calc_unrealised_pnl(direction, entry_price, current_price, size)` — Absolute PnL
- `calc_unrealised_pnl_pct(direction, entry_price, current_price, leverage)` — Standard Bybit ROE %
- `calc_position_value(size, entry_price)` — Entry-based notional (size * entry_price); feeds this project's local margin/IM/MM helpers. NOT Bybit's reported positionValue (mark-based: |size| * mark_price). Bybit UTA IM uses mark + hedge (see "Margin Ratio vs Bybit positionIM" section); local formulas stay entry-based. Snapshot/parity code computes mark at emit time separately (feature 0060).
- `calc_initial_margin(position_value, leverage)` — Initial margin
- `calc_liq_ratio(liq_price, current_price)` — Liquidation ratio
- `calc_maintenance_margin(position_value, symbol, tiers=None)` — Tier-based MM (supports dynamic tiers)
- `calc_imr_pct(total_im, margin_balance)` — Account IMR %
- `calc_mmr_pct(total_mm, margin_balance)` — Account MMR %
- `calc_margin_ratio(position_value, wallet_balance)` — Per-position margin ratio (positionValue / walletBalance)
- `parse_risk_limit_tiers(api_tiers)` — Bybit API response → `MMTiers`

All take Decimal inputs; `position.py` keeps float copy for risk mgmt performance.

---

