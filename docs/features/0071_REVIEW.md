# 0071 Code Review

## Summary

Approved. Feature 0071 is correctly implemented in `2a083cb`. The five replay
risk-management fields flow from `ReplayStrategyConfig` into
`BacktestStrategyConfig`; the four `RiskConfig` fields then reach both linked
positions, and `leverage` remains consumed by `BacktestRunner` as planned.
Defaults preserve behavior for existing replay YAMLs.

## Findings

No blocking or actionable issues found.

`apps/replay/src/replay/engine.py:L332`: build site includes all five 0071
fields; real `ReplayEngine.run()` test covers this path.

`apps/backtest/src/backtest/runner.py:L163`: `RiskConfig` receives
`increase_same_position_on_low_margin`; both linked-position tests cover
`True` and default `False`.

`apps/replay/src/replay/config.py:L59`: YAML names are flat snake_case and
match the Pydantic fields directly; no camelCase or nested `{data: ...}` style
alignment issue found.

## Plan Compliance

All requested files and phases are covered:

- `BacktestStrategyConfig` now has `increase_same_position_on_low_margin` with
  default `False`.
- `BacktestRunner` passes the flag into `RiskConfig`.
- `ReplayStrategyConfig` exposes the five fields with backtest-matching
  defaults.
- `ReplayEngine.run()` passes all five fields through to
  `BacktestStrategyConfig`.
- `backtest.yaml.example`, `replay.yaml.example`, and `RULES.md` document the
  defaults, live-value caveats, and feature-0040/0071 relationship.
- Tests cover defaults, leverage validation, YAML round-trip, real engine
  build-site wiring, seed-pipeline mirror wiring, and linked-position risk flag
  wiring.

## Test Review

Tests follow existing patterns and stay fast. External dependencies in
`test_engine.py` are mocked with `InstrumentInfoProvider`, and `_init_runner`
is patched only where the test is specifically asserting the constructed
`BacktestStrategyConfig`.

Acceptable residual gap: no new test separately asserts `min_liq_ratio` and
`max_liq_ratio` reach `RiskConfig` through `BacktestRunner`; that wiring
already existed before 0071. The new field introduced by 0071 is covered.

## Verification

- `uv run pytest -q apps/backtest/tests/test_config.py apps/backtest/tests/test_runner.py apps/replay/tests/test_config.py apps/replay/tests/test_engine.py apps/replay/tests/test_engine_seed.py`
  - `173 passed in 0.46s`
- `uv run ruff check apps/backtest/src/backtest/config.py apps/backtest/src/backtest/runner.py apps/replay/src/replay/config.py apps/replay/src/replay/engine.py apps/backtest/tests/test_runner.py apps/replay/tests/test_config.py apps/replay/tests/test_engine.py apps/replay/tests/test_engine_seed.py`
  - `All checks passed!`
