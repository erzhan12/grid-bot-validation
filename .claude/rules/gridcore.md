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

