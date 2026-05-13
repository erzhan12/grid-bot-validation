# Feature 0038 Review - Fix `DetachedInstanceError` in position pairing

## Summary

No blocking issues found.

The implementation matches the plan:

- `ReplayEngine.run` now performs `PositionComparator.pair_and_compare(...)`
  and `fold_metrics_into(...)` inside the same database session that loaded the
  `PositionSnapshot` ORM rows.
- `comparator.main.run` applies the same session-scope fix.
- Both call sites call `expunge_all()` before the context manager exits, so the
  rows are detached before SQLAlchemy's exit-time `commit()` can expire loaded
  attributes.
- The paired snapshot rows remain available to downstream CSV export through
  `ComparatorReporter`.

## Findings

None.

## Test Review

The added regression coverage is appropriate for the bug:

- `apps/replay/tests/test_engine.py::TestPositionPairsSurviveSessionClose::test_position_pairs_accessible_after_run`
  seeds matching live/backtest `PositionSnapshot` rows, runs the replay engine,
  and verifies returned pair attributes are readable after `ReplayEngine.run`
  returns.
- `apps/comparator/tests/test_main.py::TestRun::test_run_position_telemetry_survives_session_close`
  seeds matching snapshots, runs the comparator path, and verifies
  `position_comparison.csv` is written with data rows.

The tests follow existing in-memory SQLite patterns and are isolated from
external services. They cover the failure mode from the plan: ORM column
attribute access after the loader session has closed.

## Verification

Commands run:

```bash
uv run pytest -q apps/replay/tests/test_engine.py::TestPositionPairsSurviveSessionClose::test_position_pairs_accessible_after_run
uv run pytest -q apps/comparator/tests/test_main.py::TestRun::test_run_position_telemetry_survives_session_close
uv run pytest -q apps/comparator/tests/test_main.py apps/comparator/tests/test_reporter.py apps/comparator/tests/test_position_metrics.py apps/comparator/tests/test_loader.py
uv run pytest -q apps/replay/tests/test_engine.py
uv run ruff check apps/replay/src/replay/engine.py apps/replay/tests/test_engine.py apps/comparator/src/comparator/main.py apps/comparator/tests/test_main.py
```

Results:

- Targeted regression tests: passed.
- Comparator relevant test suite: `86 passed`.
- Replay engine test suite: `14 passed`.
- Ruff: passed.

## Residual Notes

The manual replay invocation mentioned in the plan was not run during this
review. The automated regression coverage exercises the ORM expiration failure
path and the comparator CSV export path, but not the full Phase 4 replay config.
