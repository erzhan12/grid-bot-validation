# 0010 Review: PnL Checker (Post-Fix Pass)

## Findings (ordered by severity)

1. **[P2] "Cum Funding (from tx log)" does not show an "ours" funding value**
   - `apps/pnl_checker/src/pnl_checker/comparator.py:255` creates the cumulative funding row.
   - `apps/pnl_checker/src/pnl_checker/comparator.py:258` sets `our_value` to `"{transaction_count} records"` instead of a funding calculation.
   - This means the `[Bybit | Ours | Delta | Status]` table is not actually comparing funding values for that row, and it diverges from the plan’s funding comparison intent.
   - **Recommendation:** Put a numeric "ours" funding value on this row (or rename/split fields clearly so this is not interpreted as a value comparison).

2. **[P2] Truncated funding history can still produce overall PASS**
   - `apps/pnl_checker/src/pnl_checker/comparator.py:270` adds `"Funding Data Warning"` with `passed=None`.
   - `apps/pnl_checker/src/pnl_checker/comparator.py:73` defines `all_passed` only as `total_fail == 0`.
   - Result: a run can report `ALL CHECKS PASSED` while funding data is explicitly marked incomplete.
   - **Recommendation:** Treat truncation as a failure (`passed=False`) or fail earlier when truncation is detected.

3. **[P3] Missing comparator test coverage for truncated-funding behavior**
   - `apps/pnl_checker/tests/test_fetcher.py:116` verifies `truncated=True` is captured by fetcher.
   - `apps/pnl_checker/tests/test_comparator.py` has no test asserting comparator/verdict behavior when `funding.truncated=True`.
   - **Recommendation:** Add a comparator test that verifies expected verdict semantics for truncated funding (warning-only vs fail).

## Plan Compliance Summary

- Previously reported core issues are fixed:
  - mark-price snapshot mismatch fixed (calculator now uses position mark price),
  - funding double-counting per hedge sides fixed (attached once per symbol),
  - missing calculation now fails,
  - funding pagination truncation is surfaced,
  - JSON now includes redacted config,
  - missing test modules were added,
  - lint issues were resolved.
- Remaining plan/behavior gap is funding comparison semantics for the cumulative funding row.

## Validation Performed

- `uv run pytest apps/pnl_checker/tests -q` -> **60 passed**
- `uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -q` -> **91% total coverage**
- `uv run ruff check apps/pnl_checker/src apps/pnl_checker/tests` -> **all checks passed**
- `uv run pytest packages/bybit_adapter/tests/test_rest_client.py -q` -> **56 passed**
