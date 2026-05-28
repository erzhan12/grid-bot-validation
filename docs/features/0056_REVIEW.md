# Feature 0056 Code Review

## Findings

### P3 — Untracked replay config appears unrelated to the feature plan

`apps/replay/conf/iterative_v9.yaml` is still untracked and was not listed in the 0056 plan. It contains a concrete DB path, run id, account id, and output directory for an iterative replay run.

If this is a local validation artifact, leave it out of the feature change. If it is intended to be committed, add an explicit rationale because it is not part of the schema, writer, backtest, comparator, reporter, or test wiring requested by the plan.

## Resolved From Prior Review

- Recorder REST snapshot coverage was added in `apps/recorder/tests/test_initial_rest_snapshot.py` for parsed `curRealisedPnl`, missing values, explicit `"0"`, and the synthesized zero-row placeholder.
- The prior `ruff` failures from unused imports in `apps/backtest/tests/test_position_tracker.py` and `shared/db/tests/test_repositories.py` are fixed.

## Notes

- The implementation matches the requested data flow: ORM column, idempotent migration, repository insert dict, event-saver writer, recorder writer, replay seed, backtest deferred reset semantics, comparator aggregation, and reporter export wiring are all present.
- The 0056 schema probe is added after the existing 0034 `source` probe and replay calls it before repository queries.
- `cur_realised_pnl_delta` is intentionally not included in `has_missing_telemetry`, matching the plan’s legacy-row compatibility requirement.
- No obvious camelCase/snake_case mismatch was found: live writers read Bybit `curRealisedPnl`, while DB/model/reporter code uses `cur_realised_pnl`.

## Commands Run

```bash
uv run pytest -q apps/backtest/tests/test_position_tracker.py apps/backtest/tests/test_runner.py apps/replay/tests/test_snapshot_loader.py apps/comparator/tests/test_position_metrics.py apps/comparator/tests/test_reporter.py shared/db/tests/test_repositories.py apps/event_saver/tests/test_writers.py apps/recorder/tests/test_initial_rest_snapshot.py
```

Result: `345 passed in 1.15s`

```bash
uv run ruff check apps/backtest/src/backtest/position_tracker.py apps/backtest/src/backtest/runner.py apps/replay/src/replay/snapshot_loader.py apps/comparator/src/comparator/position_loader.py apps/comparator/src/comparator/position_metrics.py apps/comparator/src/comparator/metrics.py apps/comparator/src/comparator/reporter.py apps/event_saver/src/event_saver/writers/position_writer.py apps/recorder/src/recorder/recorder.py shared/db/src/grid_db/models.py shared/db/src/grid_db/repositories.py scripts/migrate_0056_cur_realised_pnl.py apps/backtest/tests/test_position_tracker.py apps/backtest/tests/test_runner.py apps/replay/tests/test_snapshot_loader.py apps/comparator/tests/test_position_metrics.py apps/comparator/tests/test_reporter.py shared/db/tests/test_repositories.py apps/event_saver/tests/test_writers.py apps/recorder/tests/test_initial_rest_snapshot.py
```

Result: `All checks passed!`
