# 0072 — Event-driven follower fill mode (issue #168)

Plan: docs/features/0072_PLAN.md  |  Branch: feature/0072-event-follower

## Status: implementation + tests + docs COMPLETE (awaiting review)

- [x] Step 1 — Foundations: repo secondary sort (exchange_ts, exec_id); FillMode.EVENT_FOLLOWER + _should_fill raise; config Literal + docstring
- [x] Step 2 — RecordedExecution dataclass + EventFollower (drain/match, tz-normalized, initial_prev_ts boundary) + unit tests
- [x] Step 3 — order_manager.apply_recorded_fill (placed-qty cap, pro-rated fee/pnl, partial/full) + unit tests
- [x] Step 4 — session.py: shared _pnl_delta + set_pending_wallet/clear_pending_wallet (pending fold-in)
- [x] Step 5 — runner.py: _event_follower attr, _prev_tick_ts, _dispatch_intents extraction (trigger-3 hook), iterative drain in process_fills, _FillRollup buffers + triggers 1-4, finalize_event_follower, EPS basis check
- [x] Step 6 — replay engine.py wiring: load+materialize inside session, symbol filter, _init_runner kwarg + post-construction stash, finalize call after tick loop before wind-down
- [x] Step 7 — reporter mode-aware relabel (backtest_only/live_only)
- [x] Step 8 — tests: test_fill_simulator_event_follower.py (23 unit), test_engine_event_follower.py (4 integration incl. oracle round-trip: live_only=0, backtest_only=0, pnl delta 0, partial aggregation, same-window open→close, ETH symbol canary)
- [x] Step 9 — RULES.md event_follower entry; regression: 830 passed (backtest+replay+comparator) + 155 (shared/db)

## Not committed — awaiting user review/approval before any commit.
