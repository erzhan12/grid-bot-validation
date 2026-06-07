# 0069 Code Review — Auto state-divergence detector + forced reconcile

Plan reviewed: `docs/features/0069_PLAN.md`

## Findings

No open implementation findings after follow-up fixes.

## Notes

- The analyzer changes live under `.claude/skills/gridbot-health/`, which is gitignored by repository convention (`.gitignore:42`; see `docs/features/0063_PLAN.md` and `docs/features/0068_PLAN.md`). They are intentionally local/operator-skill changes, not normal tracked source changes. If this feature is later published as a branch artifact that must include the skill tree, it will need an explicit force-add or a tracked skill-source location.
- Follow-up lint cleanup was applied in the new divergence tests: semicolon-separated mock setup was split onto separate lines, and the unused `replace` import was removed.

## Positive Review Notes

- The two-throttle contract is implemented correctly: `_trigger_divergence_reconcile` checks the detector throttle first, calls `_force_reconcile_strat(..., direction=None, emit_breaker_warning=False)` exactly once, and only emits the detector WARNING / clears dedup / bumps `_divergence_last_fire_at` when `_force_reconcile_strat` returns `True`.
- `_force_reconcile_strat(direction=None)` handles both LONG and SHORT internally under one cooldown timestamp and suppresses the breaker WARNING on detector calls, preventing the analyzer double-count described in the plan.
- Kill-switch coverage is present at the wrapper and upstream signal work: signal 1 recording, signal 2 health check edge, signal 3 REST read, and signal 4 enqueue all skip when `divergence_detector_enabled=False`.
- Signal 1 records the intended union `{110017, 110072, network}` from both failure exits and excludes 110007.
- Signal 3 uses a read-only `rest_position_size` helper and fires one full reconcile with `direction=None` when either side exceeds the configured qty-step threshold.
- Signal 4 correctly limits enqueueing to private WS paths, drains via a locked pending set in `_tick`, drops suppressed pending entries, and fast-tracks order sync when detector-throttle suppression would otherwise leave order state waiting for the normal interval.
- Analyzer logic locally includes the merged `force_reconcile_fired` key, label, detector/breaker scan, no-double-count tests, and additive merge test.

## Verification

- `uv run pytest -q apps/gridbot/tests/test_orchestrator_divergence.py apps/gridbot/tests/test_runner_divergence.py apps/gridbot/tests/test_executor.py apps/gridbot/tests/test_config.py`  
  Result: `143 passed in 0.25s`
- `uv run pytest -q .claude/skills/gridbot-health/test_analyze.py`  
  Result: `55 passed in 0.03s`
- `uv run ruff check`  
  Result: failed on pre-existing full-repo lint issues outside this feature's tracked changes.
- Scoped ruff on feature files fails only on pre-existing analyzer/runner lint baseline (`.claude/skills/gridbot-health/analyze.py` ambiguous `l` variables and `runner.py` import-order baseline).

## Test Coverage Assessment

Coverage is strong for the new behavior: happy paths, throttle/cooldown suppression, kill-switch inertness, both-direction reconcile, signal edge behavior, REST read failure paths, dedup clearing, analyzer registration/counting/merge, and production WS trigger paths are all covered with fast isolated mocks.

Remaining risk is mostly operational rather than logic-level: the analyzer update lives in ignored local skill files by repository convention, and full-repo lint remains red because of the existing lint baseline.
