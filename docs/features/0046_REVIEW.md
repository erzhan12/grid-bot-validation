# Feature 0046 Code Review

## Findings

No remaining findings.

The previous review findings were addressed:

- `RULES.md` now documents feature 0046 under SAME ORDER detection, including the 60 s per-runner throttle, loud-first / heartbeat / recovery-summary cadence, REST WS-glitch single-INFO behavior, issue #94, and the 1-hour warning bound.
- The clean-execution regression test now re-latches through the real `_check_same_orders` duplicate-pair path instead of directly setting `_same_order_error`.
- The REST WS-glitch regression test now starts from an actual latched UNKNOWN state, pumps `on_ticker` to populate throttle state through the placement gate, then re-adjudicates the same pair to `WS_GLITCH_SUSPECTED`.

## Notes

- The main throttle implementation in `apps/gridbot/src/gridbot/runner.py` matches the plan: first warning is unchanged, subsequent ticks inside the window are suppressed, and heartbeat re-emits include `(suppressed N since last)` before resetting the counter.
- Clear handling matches the three required contexts: external reset uses `reset_same_order_error()`, clean-fill auto-clear preserves buffers and emits through `_emit_clear_recovery_if_needed()` only on a True→False net transition, and REST WS-glitch auto-clear emits one combined verdict + suppressed-count INFO via `reset_same_order_error(emit_recovery_info=False)`.
- No data-shape alignment issues, style mismatches, or over-engineering concerns were found in the updated change.
- The new tests cover the requested happy paths and edge cases and follow the existing direct-runner testing style.

## Verification

- `uv run pytest apps/gridbot/tests/test_runner.py -q` passed: `159 passed`.
- `uv run pytest -q` still fails in unrelated backtest risk-limit tests:
  - `apps/backtest/tests/test_risk_limit_info.py::TestRiskLimitProvider::test_load_from_cache_oversized_logs_size_error`
  - `apps/backtest/tests/test_risk_limit_info.py::TestConcurrentCacheAccess::test_lock_registry_released_when_instances_deleted`
  - `apps/backtest/tests/test_risk_limit_info.py::TestConcurrentCacheAccess::test_close_then_new_provider_keeps_lock_registry_consistent`
