# 0070 Code Review

## Findings

### [P1] Restore unrelated `RULES.md` content removed by this change

- File: `RULES.md`
- Evidence: `git diff --numstat RULES.md` reports `133` additions and `1516` deletions; the file shrank from `2633` lines in `HEAD` to `1250` lines in the worktree.
- Impact: The 0070 plan only asked for a short note documenting the new metric set and classification convention. Instead, the change removes a large amount of unrelated project guidance, including the old `Key Implementation Notes` section near the top and many later phase notes. This is a documentation regression and can cause future implementation/review work to lose established project invariants.
- Recommendation: Revert the unrelated deletions in `RULES.md` and keep only the additive 0070 section. The new 0070 note itself is useful; it should be appended/preserved without rewriting unrelated sections.

## Notes

- The comparator implementation otherwise matches the plan: one shared `_spike_stats` helper, upper-mid median, clamped p95, `pstdev`, median-zero relative-count guard, all required position families, optional IM/MM, and trade-level `pnl_*` symmetry.
- CSV rows are grouped as requested, including `pnl_*` after `cumulative_pnl_delta` and before `pnl_correlation`.
- Tests cover helper edge cases, trade-level PnL robust stats, key per-snapshot families, spike-vs-drift classification examples, state-diverged exclusion, idempotency, and reporter rows/summary output.

## Verification

- `uv run pytest -q apps/comparator/tests/test_metrics.py apps/comparator/tests/test_position_metrics.py apps/comparator/tests/test_reporter.py`
  - `105 passed in 0.09s`
- `uv run ruff check apps/comparator/src/comparator/metrics.py apps/comparator/src/comparator/position_metrics.py apps/comparator/src/comparator/reporter.py apps/comparator/tests/test_metrics.py apps/comparator/tests/test_position_metrics.py apps/comparator/tests/test_reporter.py`
  - `All checks passed`
