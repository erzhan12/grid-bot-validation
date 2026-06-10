# 0071 Code Review

## Summary

Feature 0071 is **correctly implemented** for its core goal: the five risk-mgmt fields (`min_liq_ratio`, `max_liq_ratio`, `min_total_margin`, `increase_same_position_on_low_margin`, `leverage`) are exposed on `ReplayStrategyConfig`, passed through `ReplayEngine.run()` into `BacktestStrategyConfig`, and `increase_same_position_on_low_margin` is wired into `RiskConfig` on the backtest/replay path. Defaults preserve backwards compatibility. Tests cover the plan's required cases including the real engine build site.

One **blocking documentation regression** in `RULES.md` should be fixed before merge.

## Findings

### [P1] Restore unrelated `RULES.md` content removed by this change

- File: `RULES.md`
- Evidence: `git diff --numstat RULES.md` reports `135` additions and `1517` deletions; the file shrank from `2637` lines in `HEAD` to `1255` lines in the worktree.
- Impact: Phase 5 only asked for an update to the feature-0040 divergence note (~line 127) and a one-line entry for the five replay tunables. Instead, the change removes a large amount of unrelated project guidance, including the old `Key Implementation Notes` section near the top and many later phase notes. This is the same class of regression flagged in `docs/features/0070_REVIEW.md` and can cause future work to lose established invariants.
- Recommendation: Revert the unrelated deletions in `RULES.md` and keep only the additive 0071 edits (the new bullets at lines 127–128 are correct and should be preserved).

### [P3] Unused imports in `test_engine_seed.py`

- File: `apps/replay/tests/test_engine_seed.py:28-29`
- Evidence: `uv run ruff check` reports `F401` for `TickerSnapshot` and `TickerSnapshotRepository`.
- Impact: Noise in lint output; may fail CI if ruff is enforced on this path.
- Recommendation: Remove the unused imports (or use them if a follow-up test needs them).

### [P3] Four duplicate `BacktestStrategyConfig` build sites (maintenance hazard)

- Files: `apps/replay/src/replay/engine.py:332-351`, `apps/replay/tests/test_config.py:188-206`, `apps/replay/tests/test_engine_seed.py:271-291` and `:354-372`
- Impact: Future field additions to the engine build require updating multiple mirrors. The plan anticipated this; `test_run_passes_risk_fields_to_backtest_strategy_config` in `test_engine.py` closes the critical gap (engine omission). Remaining mirrors in seed/config tests can still drift for fields not asserted.
- Recommendation: Acceptable for this feature. Consider a shared `replay_strategy_to_backtest_config(replay_config) -> BacktestStrategyConfig` helper in a follow-up if more pass-through fields are added.

## Plan compliance

| Phase | Status | Notes |
|-------|--------|-------|
| 1A — Existing `BacktestStrategyConfig` ratio/leverage fields | OK | No change needed |
| 1B — Add `increase_same_position_on_low_margin` to `BacktestStrategyConfig` | OK | Default `False`, description matches gridbot |
| 1B — `backtest.yaml.example` commented entry | OK | Wording matches `gridbot.yaml.example:23` |
| 1C — `runner.py` passes flag into `RiskConfig` | OK | Lines 163-170 |
| 2A — Five fields on `ReplayStrategyConfig` | OK | Defaults match backtest; block comment + `min_total_margin` description document partial-set caveat |
| 2B — Engine pass-through | OK | All five kwargs at `engine.py:344-350` |
| 3 — `replay.yaml.example` documentation | OK | Commented live-mirror examples with operator-supplied caveats; uncommented baseline not live values |
| 4A — Replay config tests | OK | Defaults, round-trip, leverage bounds |
| 4B — Backtest runner wiring tests | OK | True/false flag on both linked positions |
| 4B optional — `test_config.py` default assertion | OK | `increase_same_position_on_low_margin is False` |
| 4C — Seed pipeline mirror sync | OK | Five kwargs in main pipeline test |
| 4C recommended — Risk-config assertion | OK | `test_seed_pipeline_wires_risk_config_to_positions` |
| 4D — Engine build-site test | OK | `test_run_passes_risk_fields_to_backtest_strategy_config` patches `_init_runner`, asserts all five fields |
| 5 — `RULES.md` targeted edits | **Partial** | Correct 0071 content added, but massive unrelated deletion (see P1) |

## Implementation notes (no action required)

- **Data alignment**: Field names are consistent snake_case end-to-end (`ReplayStrategyConfig` → `BacktestStrategyConfig` → `RiskConfig`). No nested-object or camelCase mismatches observed. YAML round-trip uses flat `strategy:` keys as expected.
- **`leverage`**: Correctly passed to `BacktestStrategyConfig` and consumed by `BacktestRunner` at `runner.py:150`; not wired into `RiskConfig` (per plan / `RiskConfig` schema).
- **Backwards compatibility**: Omitting the five fields in replay YAML keeps prior behaviour (`0.8 / 1.2 / 0.15 / false / 10`).
- **Scope**: `pnl_checker` left on 4-arg `RiskConfig` — documented in `RULES.md` and matches plan out-of-scope.
- **Style**: Changes follow existing pydantic `Field` patterns and test naming (`test_*` with feature comments). No over-engineering; diff is focused.

## Test review

- **Happy path**: Non-default risk values round-trip yaml → config → `BacktestStrategyConfig`; engine `run()` captures correct `BacktestStrategyConfig`; seed pipeline wires flag and `min_total_margin` into `Position.risk_config`.
- **Edge cases**: Leverage `0` and `126` rejected; default flag `False` on both backtest and replay configs; explicit `True` wired to both linked positions.
- **Isolation**: Engine test mocks `InstrumentInfoProvider` and patches `_init_runner` — minimal, fast, no DB side effects for the capture assertion.
- **Gap (acceptable)**: No test asserts `min_liq_ratio` / `max_liq_ratio` / `min_total_margin` reach `RiskConfig` on `BacktestRunner` (only the new flag). Those three were already wired pre-0071; plan did not require new coverage for them.

## Verification

- `uv run pytest apps/backtest/tests/test_config.py apps/backtest/tests/test_runner.py apps/replay/tests/test_config.py apps/replay/tests/test_engine.py apps/replay/tests/test_engine_seed.py -q`
  - `173 passed in 0.47s`
- `uv run ruff check` on changed source files: **2 unused-import errors** in `test_engine_seed.py` (see P3)
