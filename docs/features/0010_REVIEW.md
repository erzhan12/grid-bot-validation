# 0010 Review: PnL Checker (Post-Fix Re-Review)

## Findings (ordered by severity)

1. **[P3] Cumulative funding is still informational, not a bybit-vs-ours comparison**
   - The plan says each comparison field should include `bybit_value`, `our_value`, `delta`, and pass/fail, and explicitly maps cumulative funding as:
     - Bybit: transaction log sum
     - Ours: `size * mark * rate` snapshot
     (`docs/features/0010_PLAN.md:142`, `docs/features/0010_PLAN.md:158`)
   - Current implementation keeps funding as info-only fields:
     - `Cum Funding (from tx log)` with only Bybit value (`apps/pnl_checker/src/pnl_checker/comparator.py:255`)
     - `Funding Record Count` metadata (`apps/pnl_checker/src/pnl_checker/comparator.py:259`)
     - Snapshot remains separate as another info field (`Funding Snapshot (cur rate)` in position comparison)
   - Net effect: no numeric funding delta/pass check is produced.
   - **Recommendation:** Either:
     - implement a numeric funding comparison row (bybit vs snapshot with delta), or
     - update the plan/docs to explicitly define funding as informational-only to match actual behavior.

## Resolved Since Last Review

- Prior issues were fixed:
  - truncated funding now fails comparison,
  - comparator test was added for truncated-funding failure,
  - funding row labeling is clearer and no longer implies record count is "ours funding value."

## Validation Performed

- `uv run pytest apps/pnl_checker/tests -q` -> **61 passed**
- `uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -q` -> **91% total coverage**
- `uv run ruff check apps/pnl_checker/src apps/pnl_checker/tests` -> **all checks passed**
- `uv run pytest packages/bybit_adapter/tests/test_rest_client.py -q` -> **56 passed**
