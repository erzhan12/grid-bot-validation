# 0025 Review — SAME ORDER time-window guard

## Summary

No blocking findings.

The implementation matches `docs/features/0025_PLAN.md`:

- Adds `_SAME_ORDER_TIME_WINDOW_SEC = 5.0` in `apps/gridbot/src/gridbot/runner.py`.
- Stores `exchange_ts` in each SAME ORDER buffer record.
- Applies a strict `delta_sec > 5.0` guard before flagging same-price/same-side/different-order fills.
- Preserves the existing kill-switch behavior, buffer size, long/short buffer isolation, diagnostic logging, notifier alert, and REST cross-check behavior.
- Adds tests for far-apart fills, within-window fills, exact boundary behavior, and hedge-pair isolation.

## Findings

None.

## Review Notes

- Timestamp alignment is correct: live execution events are normalized from Bybit `execTime` into UTC `datetime` values before reaching `StrategyRunner`, so subtracting `exchange_ts` values is safe for the live path.
- The strict boundary behavior is documented and tested: `delta_sec == 5.0` still triggers, while `delta_sec > 5.0` skips.
- The `return` on a time-window miss is consistent with the current `deque(maxlen=2)` design and is clearly documented for a future buffer-size change.
- Existing positive SAME ORDER tests continue to pass because their `datetime.now(UTC)` timestamps are naturally within the window. The new explicit positive and boundary tests make that behavior less implicit.
- The diagnostic REST cross-check remains best-effort and exception-contained, so keeping it after the new guard does not change detector semantics.

## Verification

- `uv run pytest apps/gridbot/tests/test_runner.py -k same_order -q` — 11 passed, 102 deselected.
- `uv run pytest apps/gridbot/tests/ packages/gridcore/tests/ -q` — 764 passed, 1 skipped.
- `uv run ruff check apps/gridbot/src/gridbot/runner.py apps/gridbot/tests/test_runner.py` — passed.
