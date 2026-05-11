# Feature 0034 Code Review — Position Telemetry Parity

Review target: updated implementation of `docs/features/0034_PLAN.md`.

Verification run:

- `uv run pytest -q apps/comparator/tests/test_position_metrics.py apps/comparator/tests/test_reporter.py apps/replay/tests/test_engine.py apps/backtest/tests/test_runner.py shared/db/tests/test_repositories.py apps/event_saver/tests/test_writers.py apps/replay/tests/test_engine_seed.py` — **214 passed**
- `uv run ruff check` on the changed implementation files — **passed**

## Findings

No blocking findings found in this pass.

## Previously Reported Issues

- `cum_realised_pnl_final_delta` now aggregates final deltas per side instead of using `matched[-1]`.
- Missing `Run.account_id` now raises instead of silently disabling the backtest snapshot writer.
- REST initial snapshots now preserve `markPrice="0"` consistently with the websocket writer.
- Backtest snapshots now use cached `TickerEvent.mark_price` for `PositionSnapshot.mark_price`, with a regression test covering `last_price != mark_price`.
- Reporter CSV/export tests and replay writer/wind-down coverage are present.

## Notes

- The implementation now matches the major plan requirements: schema/model changes, repository source filtering, live recorder parsing, backtest fill and wind-down emission, seed propagation for `cum_realised_pnl`, comparator pairing/metrics, standalone comparator wiring, and CSV/reporting export.
- I did not rerun the entire repository test suite; the focused feature suite and changed-file lint checks passed.
