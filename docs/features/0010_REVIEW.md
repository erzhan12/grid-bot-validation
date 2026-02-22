# 0010 Review: PnL Checker

## Findings (ordered by severity)

1. **[P1] Mark-price validation compares values computed from different data snapshots**
   - `apps/pnl_checker/src/pnl_checker/calculator.py:197` computes mark-based PnL using `ticker.mark_price` (from market ticker endpoint).
   - `apps/pnl_checker/src/pnl_checker/comparator.py:123` compares that against `pos_data.unrealised_pnl` (from positions endpoint).
   - These are fetched from different endpoints/timestamps, so drift can create false failures even when formulas are correct.
   - **Recommendation:** For mark-based validation, use `PositionData.mark_price` (same payload family as `unrealisedPnl`) or otherwise compare only data derived from the same snapshot.

2. **[P1] Cumulative funding is duplicated across long/short rows**
   - `apps/pnl_checker/src/pnl_checker/comparator.py:271` reads symbol-level funding once, then `apps/pnl_checker/src/pnl_checker/comparator.py:284` appends it to every position under that symbol.
   - In hedge mode (both long + short open), each row shows the full symbol cumulative funding, effectively double-counting if users aggregate per-position output.
   - **Recommendation:** Report cumulative funding at symbol level once, or split funding per side if transaction data can be attributed per position side.

3. **[P1] Funding API failures are silently converted to zero funding**
   - `apps/pnl_checker/src/pnl_checker/fetcher.py:180` catches all exceptions from funding fetch and `apps/pnl_checker/src/pnl_checker/fetcher.py:187` returns zero funding.
   - This allows a run to continue and possibly PASS while key validation data is missing.
   - **Recommendation:** Fail the run (or mark field as explicit error state) when funding retrieval fails.

4. **[P2] Missing calculated positions do not fail the run**
   - `apps/pnl_checker/src/pnl_checker/comparator.py:277` logs and skips positions with no matching calculated entry.
   - `apps/pnl_checker/src/pnl_checker/comparator.py:73` defines `all_passed` only as `total_fail == 0`, so skipped rows can still yield global PASS.
   - **Recommendation:** Treat missing calculation rows as failures (or at minimum produce explicit failed comparison entries).

5. **[P2] Transaction log pagination is capped and may truncate cumulative funding**
   - `packages/bybit_adapter/src/bybit_adapter/rest_client.py:580` defaults `max_pages` to 20.
   - `packages/bybit_adapter/src/bybit_adapter/rest_client.py:598` stops when the page limit is reached, which can undercount funding for long-lived symbols/accounts.
   - **Recommendation:** Make the limit configurable for this tool and flag truncation in output.

6. **[P2] Plan requirement not fully implemented in JSON output**
   - Plan requires JSON to include “config used” (`docs/features/0010_PLAN.md:190`).
   - `apps/pnl_checker/src/pnl_checker/reporter.py:162` builds JSON without any config payload.
   - **Recommendation:** Include resolved config (or a safe redacted subset) in report JSON for reproducibility.

7. **[P2] Test coverage is incomplete for newly introduced functionality**
   - Plan lists `apps/pnl_checker/tests/test_reporter.py` (`docs/features/0010_PLAN.md:53`), but test directory currently contains only `test_calculator.py` and `test_comparator.py`.
   - No tests were added for `config.py`, `fetcher.py`, `main.py`, `reporter.py`, or new `BybitRestClient` methods (`get_tickers`, `get_transaction_log`, `get_transaction_log_all`).
   - Current targeted coverage run reports 50% total for `pnl_checker`, with `config.py`, `main.py`, and `reporter.py` at 0%.
   - **Recommendation:** Add isolated unit tests with mocking for API calls and filesystem writes, and add reporter/config/main tests per plan.

8. **[P3] Style/lint drift from existing codebase checks**
   - `uv run ruff check apps/pnl_checker/src apps/pnl_checker/tests` reports unused imports in:
     - `apps/pnl_checker/src/pnl_checker/calculator.py:12`
     - `apps/pnl_checker/tests/test_calculator.py:5`
     - `apps/pnl_checker/tests/test_calculator.py:12`
     - `apps/pnl_checker/tests/test_comparator.py:5`
     - `apps/pnl_checker/tests/test_comparator.py:11`
   - **Recommendation:** Clean these imports to keep the new module aligned with repo lint standards.

## Plan Compliance Summary

- Implemented: new app scaffold, config model, calculator/comparator/reporter modules, CLI, root `pyproject.toml` and `Makefile` integration, and `BybitRestClient` method additions.
- Partial/missing vs plan:
  - Missing `apps/pnl_checker/tests/test_reporter.py`.
  - JSON output missing “config used”.
  - Funding comparison behavior differs from planned bybit-vs-ours per-field comparison semantics.

## Validation Performed

- `uv run pytest apps/pnl_checker/tests -q` -> **26 passed**
- `uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -q` -> **50% total coverage**
- `uv run ruff check apps/pnl_checker/src apps/pnl_checker/tests` -> **5 lint errors (all unused imports)**
