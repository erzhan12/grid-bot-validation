# 0022 Review — Detect grid drift every tick, walk grid by N steps

## Verdict

No blocking findings found. The current implementation matches the plan's core behavior: drift is measured from the current WAIT-band center, below-threshold ticks are true no-ops, stale fills do not cause repeated walks, pending fills are consumed on the first ticker, and a pending fill consumed during a fresh-build tick can now participate in same-tick recentering before order placement.

The prior P2 finding is resolved. `_handle_ticker_event()` now tracks `fill_consumed_this_tick` and permits `recenter_if_drifted()` when a fresh build also consumes a deferred fill.

Verification run:

```bash
uv run pytest packages/gridcore/tests/test_grid.py packages/gridcore/tests/test_engine.py -q
# 130 passed in 0.06s

uv run pytest packages/gridcore/tests -q
# 379 passed, 1 skipped in 0.28s

uv run ruff check packages/gridcore/src/gridcore/grid.py packages/gridcore/src/gridcore/engine.py
# All checks passed
```

Additional manual repro for the previous fresh-build edge also passes:

```text
ExecutionEvent before first ticker: fill at 98.01
First ticker: last_close=100.0
Pending fill consumed: True -> False
WAIT center after recenter: 100.0
Grid prices: [96.06, 97.03, 98.01, 99.0, 100.0, 101.0, 102.01, 103.03, 104.06, 105.1, 106.15]
```

The wider lint command including the touched tests still fails on four pre-existing unused locals in `packages/gridcore/tests/test_grid.py` (`min_price_before`, `imbalance_after`, `max_price_before`, `imbalance_after`). Those are outside this feature's changed test block and are not a 0022 blocker.

## Findings

No blocking findings found.

## Resolved Since Prior Review

- `recenter_if_drifted()` uses `wait_center = self._wait_center()`, matching plan Q1.
- Below-threshold ticks preserve both prices and sides, so cumulative sub-step drift is still detected against a stable WAIT band.
- `_fill_pending` is consumed on the first ticker for both restored-grid and empty-grid builds.
- Pending fills consumed during a fresh build now allow same-tick recentering before `_check_and_place()`.
- Stale `last_filled_price` no longer causes repeated grid walks on identical ticker events.
- `RecenterResult.__bool__` is falsy on no-op and truthy on walk/rebuild.
- Out-of-bounds restored grids emit the planned `Grid drift ... N=...` log line before rebuild.
- `restore_grid()` derives `_original_anchor_price` from `_wait_center()`.

## Plan Alignment

Implemented correctly:

- WAIT-center helper behavior for single WAIT, multi-WAIT, and zero-WAIT fallback.
- Trigger boundary: no action at `deviation_pct <= grid_step`; walk when greater.
- Drift reference is the current WAIT-band center, not the original build-time anchor.
- `n_steps = int(deviation_pct / grid_step)`.
- Multi-step walk up/down and safety-cap rebuild.
- Side reassignment only after actual walk/rebuild.
- `_original_anchor_price` refresh after incremental walks.
- Engine drift logging for walk and rebuild paths.
- Order placement remains downstream of the recentered grid state.

No snake_case/camelCase or `{data: ...}` nesting mismatch was found. `TickerEvent.last_price` remains a `Decimal` at the event boundary and is converted to `float` inside the engine. Persisted grid entries remain flat `{side, price}` dicts, and `GridSideType` remains a `StrEnum`, so JSON serialization stays string-compatible.

## Test Review

Coverage strengths:

- Focused grid tests cover WAIT-center calculation, threshold behavior, up/down walks, safety-cap rebuild, side reassignment, round-trip preservation outside the walked region, anchor refresh, notify behavior, and update-grid side assignment compatibility.
- Engine tests cover cold-start drift, mid-run fast moves, idempotency, below-threshold side stability, cumulative sub-step drift, pending fill consumption for restored and empty grids, current-WAIT-band reference after fill, same-tick recenter after fresh-build fill consumption, stale-fill idempotency, out-of-bounds drift logging, and full rebuild fallback.

Residual non-blocking gaps:

- Engine tests still inject `restored_grid` directly instead of exercising the full `GridStateStore` cold-restart path requested in the plan. This is acceptable for unit scope, but it is not a full persistence-path repro.
- There is no explicit even-sized multi-WAIT restore test. The implementation uses `_wait_center()`, so it should behave correctly, but a dedicated regression would lock it down.
- A few test comments around `TestAnchorPricePersistence` still describe the drift reference as an "original" or "persisted" anchor. The implementation now uses the current WAIT center; the comments should be cleaned up when convenient, but they do not affect behavior.

## Structure / Style

`RecenterResult`, `_wait_center()`, `_assign_sides()`, `_shift_grid()`, and `_fill_pending` are small, locally scoped additions that fit the existing engine/grid split. The feature does not introduce a large abstraction or move unrelated behavior. The only style nit is the stale wording in a few comments noted above.
