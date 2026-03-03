# 0012 Review — Tiered MMR from Risk Limits Cache (Re-review)

Plan: `docs/features/0012_PLAN.md`  
Rubric: `commands/code_review.md`

## Findings (ordered by severity)

### [P2] ~~Cache tier loader still crashes on some malformed value types (e.g. `null`) instead of falling back~~ RESOLVED

`BacktestRunner._load_mm_tiers()` now catches `TypeError` in addition to other exceptions (`runner.py:225`), so `Decimal(None)` falls back to hardcoded tiers. Test added: `test_load_mm_tiers_null_values_falls_back`.

### [P3] ~~`parse_risk_limit_tiers()` raises `AttributeError` for non-dict items despite documented ValueError contract~~ RESOLVED

`parse_risk_limit_tiers()` now validates element types with `isinstance(t, dict)` check (`pnl.py:554`) and raises `ValueError("api_tiers must contain dict objects")`. Test added: `test_non_dict_elements_raises` covering `int`, `None`, and `str` elements.

## Plan Implementation Coverage

Implemented and verified:
- Tier tables, tier lookup, maintenance-margin calculation, and risk-tier parsing are present in `gridcore.pnl`.
- Backtest runner loads tiers from cache (or hardcoded fallback), passes `position_value` to liquidation estimation, and uses effective MMR (`mm_amount / position_value`) when tiered data is available.
- Default `risk_limits_cache_path=None` behavior now auto-discovers `conf/risk_limits_cache.json`.
- Tests cover: effective tiered liquidation, hardcoded fallback, cache load success, missing-symbol fallback, malformed-JSON fallback, invalid-numeric-string fallback.
- Lint is clean on all changed files.

Remaining gaps:
- ~~No test currently covers `null` / wrong-type tier fields in cache loader (P2).~~ Done.
- ~~No test currently covers non-dict elements for `parse_risk_limit_tiers` (P3).~~ Done.

## Test/Lint Evidence

- `uv run pytest -q apps/backtest/tests/test_runner.py packages/gridcore/tests/test_pnl.py` -> `115 passed`
- `uv run ruff check apps/backtest/src/backtest/config.py apps/backtest/src/backtest/runner.py apps/backtest/tests/test_runner.py packages/gridcore/src/gridcore/__init__.py packages/gridcore/src/gridcore/pnl.py packages/gridcore/tests/test_pnl.py` -> `All checks passed!`
