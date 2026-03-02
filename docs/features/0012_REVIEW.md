# 0012 Review — Tiered MMR from Risk Limits Cache (Re-review)

Plan: `docs/features/0012_PLAN.md`  
Rubric: `commands/code_review.md`

## Findings (ordered by severity)

### [P2] Cache tier loader still crashes on some malformed value types (e.g. `null`) instead of falling back

`BacktestRunner._load_mm_tiers()` converts tier fields with `Decimal(...)` (`apps/backtest/src/backtest/runner.py:210-213`) and now catches `json.JSONDecodeError`, `KeyError`, `ValueError`, and `ArithmeticError` (`apps/backtest/src/backtest/runner.py:225`).

This fixed the `"not_a_number"` path, but malformed cache values like `null` still raise uncaught `TypeError` (`Decimal(None)`), which aborts startup instead of falling back to hardcoded tiers.

Repro used during review:
- `.venv/bin/python -c "... BacktestRunner._load_mm_tiers('BTCUSDT', <cache_with_mmr_rate_null>) ..."`  
- Result: `TypeError: conversion from NoneType to Decimal is not supported`.

### [P3] `parse_risk_limit_tiers()` raises `AttributeError` for non-dict items despite documented ValueError contract

`parse_risk_limit_tiers()` validates that input is a non-empty list (`packages/gridcore/src/gridcore/pnl.py:159-162`), but does not validate element types before `_TierValidator.sort()` calls `.get(...)` on each element (`packages/gridcore/src/gridcore/pnl.py:183-189`).

So malformed input like `[123]` raises `AttributeError` instead of a predictable validation error (`ValueError`), and this path is not tested in `packages/gridcore/tests/test_pnl.py`.

Repro used during review:
- `PYTHONPATH=packages/gridcore/src .venv/bin/python -c "from gridcore.pnl import parse_risk_limit_tiers; parse_risk_limit_tiers([123])"`  
- Result: `AttributeError: 'int' object has no attribute 'get'`.

## Plan Implementation Coverage

Implemented and verified:
- Tier tables, tier lookup, maintenance-margin calculation, and risk-tier parsing are present in `gridcore.pnl`.
- Backtest runner loads tiers from cache (or hardcoded fallback), passes `position_value` to liquidation estimation, and uses effective MMR (`mm_amount / position_value`) when tiered data is available.
- Default `risk_limits_cache_path=None` behavior now auto-discovers `conf/risk_limits_cache.json`.
- Tests cover: effective tiered liquidation, hardcoded fallback, cache load success, missing-symbol fallback, malformed-JSON fallback, invalid-numeric-string fallback.
- Lint is clean on all changed files.

Remaining gaps:
- No test currently covers `null` / wrong-type tier fields in cache loader (P2).
- No test currently covers non-dict elements for `parse_risk_limit_tiers` (P3).

## Test/Lint Evidence

- `uv run pytest -q apps/backtest/tests/test_runner.py packages/gridcore/tests/test_pnl.py` -> `69 passed`
- `uv run ruff check apps/backtest/src/backtest/config.py apps/backtest/src/backtest/runner.py apps/backtest/tests/test_runner.py packages/gridcore/src/gridcore/__init__.py packages/gridcore/src/gridcore/pnl.py packages/gridcore/tests/test_pnl.py` -> `All checks passed!`
