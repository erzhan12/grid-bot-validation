# 0048 Code Review: Remove per-tick drift detector, restore bbu2 grid semantics

**Reviewer:** automated review (2026-05-20)  
**Plan:** `docs/features/0048_PLAN.md`  
**Scope:** `grid.py`, `engine.py`, `test_grid.py`, `test_engine.py`, `RULES.md`

---

## Findings

No blocking or non-blocking code review findings remain.

The prior test-quality finding in `TestUpdateGridSidewayOscillation` is resolved. `test_update_grid_remarks_wait_on_repeat_fill_at_same_price` now re-queries the live grid by `fill_price` after the second `update_grid`, and its name/docstring describe repeat-fill behavior accurately.

---

## Plan Implementation

The implementation matches the feature plan:

| Plan item | Status | Notes |
|-----------|--------|-------|
| Delete `RecenterResult`, `recenter_if_drifted`, `_shift_grid` | Done | Removed from `packages/gridcore/src/gridcore/grid.py` |
| `_assign_sides(..., *, fill_price: float)` required | Done | No default; sole source caller is `update_grid` |
| Remove tick-path recenter block | Done | `grid_just_built`, `fill_consumed_this_tick`, and `_log_drift` removed from `engine.py` |
| Keep bounds-guard rebuild | Done | Ticker path still rebuilds when restored/stale grid is out of range |
| Simplify out-of-bounds telemetry | Done | Only `"Restored grid out of range"` remains; `"Grid drift"` source telemetry removed |
| Migrate/delete recenter tests | Done | `TestRecenterIfDrifted` and `TestRecenterIntegration` removed/replaced |
| Add no-recenter regression coverage | Done | `TestEngineNoRecenter.test_ticker_does_not_overwrite_post_fill_wait` covers the production failure mode |
| Update `RULES.md` | Done | Grid module note documents 0048 semantics |

Root cause coverage is sound: the tick path no longer calls `_assign_sides`, and `_assign_sides` can no longer use `last_close` as its WAIT reference. A post-fill WAIT level cannot be overwritten by in-band price drift unless another fill-driven `update_grid` occurs.

---

## Other Risks And Notes

- `update_grid` still uses exclusive bounds (`__min_grid < last_close < __max_grid`, grid.py:250) while ticker rebuild uses inclusive bounds (`min_p <= last_close <= max_p`, engine.py:171). At an exact boundary price, `update_grid` rebuilds but the tick path does not. Pre-existing, not introduced by 0048; worth a follow-up to unify.
- `_assign_sides` leaves a level unchanged when `last_close == level['price']` and the level is not close to `fill_price`. This is pre-existing bbu2-style behavior.
- Some names remain legacy wording (`test_drift_guard_*`, `test_bounds_rebuild_handles_zero_wait_center`), but the assertions now match bounds-only behavior.

### Second-pass parallel review (2026-05-20)

A five-category parallel pass (code quality, security, performance, testing, documentation) found one additional minor item:

- **Suggestion** — `apps/gridbot/tests/test_runner.py:4459` carries a stale inline comment `"Second ticker at the same price — bounds OK, no recenter, but"`. The test logic still passes under 0048 (it asserts ticker-timestamp propagation), but the "no recenter" phrasing predates the feature; reword to `"bounds OK, no grid mutation, but"` next time the file is touched.

No new Critical or Warning items. Security, performance, and test coverage are confirmed clean: `_assign_sides` is keyword-only `fill_price` with one caller (`update_grid`), the tick path has no `_assign_sides` invocation, and `test_ticker_does_not_overwrite_post_fill_wait` (test_engine.py:1506) actually exercises the in-bounds, deviation-greater-than-`grid_step` regime that triggered the production REAL_DUPLICATE under feature 0022.

---

## Verification

Commands run:

```bash
uv run pytest -q packages/gridcore/tests/test_grid.py packages/gridcore/tests/test_engine.py
# 119 passed in 0.05s

uv run pytest -q packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80
# 391 passed, 1 skipped; total coverage 93.70%

uv run pytest -q apps/replay/tests/
# 92 passed in 0.56s

uv run pytest -q apps/backtest/tests/
# 426 passed, 3 failed
```

Backtest failures appear unrelated to 0048:

- `apps/backtest/tests/test_risk_limit_info.py::TestRiskLimitProvider::test_load_from_cache_oversized_logs_size_error` expects `"Cache file exceeds"` but implementation logs `"Cache file size 10000001 exceeds 10000000 byte limit"`.
- `apps/backtest/tests/test_risk_limit_info.py::TestConcurrentCacheAccess::test_lock_registry_released_when_instances_deleted` fails because `backtest.risk_limit_info` has no `_IN_PROCESS_LOCKS`.
- `apps/backtest/tests/test_risk_limit_info.py::TestConcurrentCacheAccess::test_close_then_new_provider_keeps_lock_registry_consistent` fails for the same missing `_IN_PROCESS_LOCKS` symbol.

Removed-symbol search:

```bash
rg -n "recenter_if_drifted|RecenterResult|_shift_grid|fill_consumed_this_tick|grid_just_built|_log_drift" packages apps
# no matches
```

---

## Verdict

Approve from code review. The implementation and updated tests are consistent with the 0048 plan; remaining Phase 4-6 work is operational deploy/observation, not a code-review blocker.
