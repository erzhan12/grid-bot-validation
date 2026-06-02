# Feature 0064 Code Review (redo)

## Summary

Re-reviewed after the P2 fix and follow-up tests. The implementation matches `docs/features/0064_PLAN.md` for issue #149: dirty-mirror REST refresh before the unchanged `_is_good_to_place` guard, `TruncateBreaker` backstop, shared `ORDER_QTY_TRUNCATED_TO_ZERO`, orchestrator forced reconcile, and WS size gating while dirty. No qty-cap; reduce-only strict-`>` semantics preserved.

**Verdict:** No blocking or actionable issues. Ready to merge.

---

## Plan compliance

| Plan item | Status |
|-----------|--------|
| `StrategyConfig` fields with safe defaults | Done |
| `ORDER_QTY_TRUNCATED_TO_ZERO` in `bybit_adapter` | Done |
| `is_truncate_error()` (module-level, `_ERR_CODE_RE`) | Done |
| Pipeline: breaker → dirty refresh → guard → submit | Done |
| `_position_dirty` on first 110017; exclude from retry queue | Done |
| Fresh wire `order_link_id` after 110017 | Done |
| REST refresh by `positionIdx` (camelCase API fields) | Done |
| `_last_dirty_rest_at is None` first-refresh sentinel | Done |
| WS gate + no-baseline pass-through | Done |
| Successful reduce-only clears dirty; open does not (F1) | Done |
| `_force_reconcile_strat`: orders + position refresh, rate-limited | Done |
| Health sweep logs `truncate_breaker_reconcile_count` | Done |
| `_is_good_to_place` body unchanged | Confirmed |
| `RULES.md` updated | Done |

**Scope key:** Plan text uses `(strat_id, side, price)`; code uses `(side, price)` on a per-runner `TruncateBreaker` — correct (one runner per `strat_id`).

---

## Findings

No blocking or actionable issues.

### Previously P2 — resolved

`_force_reconcile_strat` now uses **independent** `try/except` blocks: order reconcile failure is alerted (`force_reconcile_orders_{strat_id}`) but no longer skips `_refresh_position_size_from_rest(..., force=True)`. Position refresh failures get a separate alert (`force_reconcile_pos_{strat_id}`).

```1218:1235:apps/gridbot/src/gridbot/orchestrator.py
        if reconciler is not None:
            try:
                reconciler.reconcile_reconnect(runner)
            except Exception as e:
                self._notifier.alert_exception(
                    f"forced reconcile orders {strat_id}", e,
                    error_key=f"force_reconcile_orders_{strat_id}",
                )
        # Position-size resync is the #149-critical healing step ...
        try:
            runner._refresh_position_size_from_rest(direction, force=True)
        except Exception as e:
            self._notifier.alert_exception(
                f"forced reconcile position {strat_id}", e,
                error_key=f"force_reconcile_pos_{strat_id}",
            )
```

`test_force_reconcile_exception_is_caught_and_alerted` asserts the position resync still runs when `reconcile_reconnect` raises.

### Previously P3 — resolved

- **`rest_client=None` backstop:** `test_breaker_bounds_storm_when_rest_client_none` — dirty refresh attempted but cannot heal; breaker trips at N=3.
- **Short-side REST refresh:** `test_refresh_position_size_short_side` — `positionIdx==2` parity with long.

---

## Data alignment

- `get_positions` returns camelCase Bybit dicts (`positionIdx`, `size`) — consistent with WS / `_build_position_state`.
- Sizes treated as non-negative magnitudes; malformed `positionIdx` entries skipped without crashing the hot path.
- `is_truncate_error` matches `[110017]` from `rest_client._check_response` and pybit `(ErrCode: …)` via `_ERR_CODE_RE`.

---

## Code quality

- Clear module split (`truncate_breaker.py`, `_clear_dirty`, `_apply_dirty_ws_size_gate`, `_refresh_position_size_from_rest`).
- No over-engineering; throttle-on-attempt (F2) and episode-scoped `_clear_dirty` (F3) are intentional and tested.
- Style matches `RetryQueue` (injected `now`, poll-free breaker).

---

## Tests

| Area | Coverage |
|------|----------|
| Storm collapse (primary) | `test_110017_storm_self_heals_after_one_failure`, clock-zero refresh |
| Backstop (disabled refresh) | `test_breaker_bounds_storm_when_refresh_cannot_heal` |
| Backstop (`rest_client=None`) | `test_breaker_bounds_storm_when_rest_client_none` |
| Phase 2 refresh/guard | Including `test_refresh_position_size_short_side` |
| Breaker unit | `test_truncate_breaker.py` (9 tests) |
| WS gate / stale WS | Match, non-match, no-baseline, stale-WS regression |
| Wire-id / retry guardrails | 110017 fresh id; non-110017 reuse + enqueue |
| Orchestrator reconcile | `TestForcedReconcile` incl. P2 order-fail + position refresh |
| Config / classifier | Defaults, overrides, validation, `TestIsTruncateError` |

Tests are isolated (mocks, virtual clock), fast, and name intent clearly.

---

## Verification

```text
uv run pytest apps/gridbot/tests/test_truncate_breaker.py \
  apps/gridbot/tests/test_runner_truncate_storm.py \
  apps/gridbot/tests/test_config.py::TestStrategyConfig::test_truncate_breaker_defaults \
  apps/gridbot/tests/test_config.py::TestStrategyConfig::test_truncate_breaker_overrides \
  apps/gridbot/tests/test_config.py::TestStrategyConfig::test_truncate_breaker_invalid_values_rejected \
  apps/gridbot/tests/test_executor.py::TestIsTruncateError \
  apps/gridbot/tests/test_orchestrator.py::TestForcedReconcile -q
→ 49 passed
```

---

## Residual risk (informational only)

- **Dormant dirty + throttled REST heartbeat** while WS is down is intentional; throttle bounds cost.
- **Ratio/liq/multipliers** still read fresh WS state during a dirty window (pre-existing, out of scope for #149).
- `_force_reconcile_strat` is per-strat rate-limited — sufficient for 0064; issue #151 may extend reuse.
