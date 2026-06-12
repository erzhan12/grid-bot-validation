# Feature 0072 Code Review -- Event-driven follower fill mode

Review date: 2026-06-12

## Summary

No blocking correctness issues found. The implementation matches the plan's main
contracts:

- `event_follower` is configured as a fill mode but bypasses the per-order
  simulator.
- Recorded `private_executions` are materialized to plain `RecordedExecution`
  rows inside the DB session and consumed in repository order.
- The runner uses an iterative within-tick drain, including synthetic ticker
  dispatch after applied fills so same-window reactive close orders can be
  placed and matched.
- Recorded price/qty/fee/closed_pnl values are applied to the wallet path, with
  placed-qty capping and pro-rated fee/pnl on excess recorded qty.
- Partial fills aggregate to one `BacktestTrade` per `(matcher_key,
  recorded_order_id)` lifecycle, matching `LiveTradeLoader` grouping.
- Session pending wallet sums make in-flight partial PnL/fees visible to both
  intra-drain placement and the tick-level equity update.
- `backtest_only = 0` is structurally enforced for matched follower fills by
  stamping backtest trades with the same matcher key rule as live
  normalization.

## Findings

### No Blocking Issues

I did not find a bug that should block merge.

### Non-blocking Coverage Gaps

1. `pos_value_final_delta` is not asserted in the equal-strategy integration
   round trip.

   The round trip in
   `apps/replay/tests/test_engine_event_follower.py:340` asserts trade counts,
   `live_only_count`, `backtest_only_count`, `cumulative_pnl_delta`, and
   `fee_delta`, but not the plan's acceptance expectation that final position
   value parity is zero. This is a useful extra guard because event follower's
   main value is avoiding long-window wallet/position drift.

2. Reporter relabeling has no direct unit test.

   `apps/comparator/src/comparator/reporter.py:469` changes printed labels for
   `fill_mode == "event_follower"`. The logic is correct on inspection, but a
   small `print_summary` capture test would lock the user-facing terminology
   down.

### Maintainability Note

`apps/backtest/src/backtest/runner.py` is now 1690 lines. The feature is
cohesive enough to leave in place for this change, but if another fill source or
wallet mode lands, the event-follower rollup/drain helpers should be a prime
candidate for extraction.

### Manual Acceptance Still Needed

The code and focused tests cover the equal-strategy and differ-strategy shape,
but the plan's real-data acceptance still needs operator validation:

- 7-day equal-strategy replay window with `live_only = 0`,
  `backtest_only = 0`, and wallet/position parity.
- A/B sweep where simulator-only overfill noise disappears and remaining
  `live_only` rows represent intent-set divergence.

## Test Review

The new tests are isolated, fast, and cover the important edge cases:

- `apps/backtest/tests/test_fill_simulator_event_follower.py`
  - drain window and monotonic cursor semantics
  - key-faithful matching and fallbacks
  - partial fill decrement/full-fill pop
  - qty-excess capping and fee/pnl pro-rating
  - pending wallet visibility
  - aggregation triggers 1, 2, 3, and end-of-replay finalize
  - unmatched recorded rows leaving position state untouched
- `apps/replay/tests/test_engine_event_follower.py`
  - config acceptance
  - equal-strategy round trip with real `GridEngine`
  - differ-strategy `live_only` behavior
  - default `last_cross` path unaffected
- `apps/backtest/tests/test_fill_simulator.py`
  - guard that `event_follower` never reaches per-order simulator checks

## Verification

```text
uv run pytest -q apps/backtest/tests/test_fill_simulator.py apps/backtest/tests/test_fill_simulator_event_follower.py apps/replay/tests/test_engine_event_follower.py
# 90 passed in 0.10s

uv run ruff check apps/backtest/src/backtest/fill_simulator.py apps/backtest/src/backtest/runner.py apps/backtest/src/backtest/order_manager.py apps/backtest/src/backtest/session.py apps/replay/src/replay/engine.py apps/replay/src/replay/config.py apps/comparator/src/comparator/reporter.py shared/db/src/grid_db/repositories.py
# All checks passed!
```

## Recommendation

Merge-ready from code review. Add the two non-blocking test assertions when
convenient, and run the real-window acceptance before closing issue #168.
