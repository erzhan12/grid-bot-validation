# 0009 Review — Replay Engine (Shadow-Mode Validation) (Re-review 2)

Plan: `docs/features/0009_PLAN.md`  
Review rubric: `commands/code_review.md`

## Findings (ordered by severity)

No new code findings identified in this pass.

## Resolved Since Previous Review
- `parse_datetime()` now handles ISO timestamps with `T`, timezone offsets (e.g. `+00:00`), and `Z` suffix.
- CLI datetime parse errors are handled in the main error path and return exit code `1`.
- `RunRepository.get_latest_by_type()` now filters to `("completed", "running")` by default, with tests for status behavior.
- Repository test-file lint issues (unused imports) were removed.

## Plan Implementation Coverage
- Replay orchestration is implemented with the intended backtest/comparator component reuse.
- The previous run-resolution gaps are fixed:
  - explicit `run_id` can resolve missing timestamps from the `Run` row;
  - active runs (`end_ts is None`) are supported via `now()` fallback.
- The plan document now explicitly reflects the implemented root-level replay parameters (`initial_balance`, `enable_funding`, `wind_down_mode`).

## Over-Engineering / Structure Notes
- No over-engineering concerns identified in this pass.
- File sizes and structure remain reasonable for current scope.

## Residual Risk / Test Gap
- Replay tests are primarily unit-level with in-memory providers/mocked instrument info.
- There is still no end-to-end integration test that replays a real recorder DB snapshot and validates generated comparator report artifacts.

## Test / Lint Evidence
- `uv run pytest apps/replay/tests shared/db/tests/test_repositories.py -q`  
  Result: `84 passed`
- `uv run ruff check apps/replay shared/db/tests/test_repositories.py`  
  Result: `All checks passed!`
