# Feature 0083 Code Review — Execution-identity idempotency guard

## Findings

### P0 — Feature 0083 is not implemented

The plan requires a top-of-`on_execution` idempotency guard keyed by
`ExecutionEvent.exec_id`, backed by a bounded FIFO `OrderedDict`, before
tracked-order lookup and before `_check_same_orders`. The current code has
none of the required pieces:

- `runner.py:14` imports only `deque`, not `OrderedDict`.
- `runner.py:83-114` defines the existing SAME ORDER constants but no
  `_EXEC_DEDUP_MAX_ENTRIES`.
- `runner.py:462-483` initializes the SAME ORDER buffers/cache but no
  `self._processed_exec_ids`.
- `runner.py:777-798` resets the phantom-drop flag, then immediately performs
  tracked-order lookup and `_check_same_orders(event)`; there is no
  `_seen_exec_id(event.exec_id)` early return.
- No `_seen_exec_id` helper exists in `StrategyRunner`.

Impact: the exact incident described in the plan is still reproducible. A
redelivered execution with the same `exec_id` still reaches
`tracked.mark_filled()` (`runner.py:817-818`), emits another `"Order filled:"`
INFO line (`runner.py:819-822`), and calls `self._engine.on_event(event)`
again (`runner.py:825`). Full-fill redeliveries also still reach
`_check_same_orders`, so they can mutate the maxlen=2 buffers and reset/clear a
latched `_same_order_error`, which is the P2 ordering issue the plan explicitly
called out.

Implement the planned guard before line 779:

1. Add `OrderedDict` and `_EXEC_DEDUP_MAX_ENTRIES = 4096`.
2. Initialize `self._processed_exec_ids: OrderedDict[str, None]`.
3. Add `_seen_exec_id(self, exec_id: str) -> bool` with no TTL/clock and FIFO
   eviction via `popitem(last=False)`.
4. Call it immediately after
   `self._drop_phantom_event_for_current_call = False`; on repeat, DEBUG-log
   and `return []`.

### P1 — Required regression tests are missing

The plan asks for six new tests in `apps/gridbot/tests/test_runner.py` covering:

- redelivery dedup by identical `exec_id`;
- distinct `exec_id`s not being deduped;
- FIFO cap/eviction;
- empty `exec_id` not being deduped;
- composition with Feature 0031 phantom drop;
- redelivery not clearing a latched SAME ORDER error.

The current test file contains prior Feature 0031 SAME ORDER and phantom-drop
tests, but no tests for `_processed_exec_ids`, `_seen_exec_id`,
`_EXEC_DEDUP_MAX_ENTRIES`, or single-`exec_id` redelivery behavior. Because the
primary guard is absent, these tests would fail if added as specified.

Add the tests from the plan once the guard is implemented. The placement
regression test is especially important because a guard inserted after
`_check_same_orders` would still be wrong.

### P2 — RULES.md was not updated for the new invariant

The plan requires a short RULES.md note under `### SAME ORDER detection`
documenting the complementary single-`exec_id` FIFO guard and its placement
before `_check_same_orders`. The existing section (`RULES.md:615-623`) only
documents Feature 0031 pair-level dedup, warning throttling, and phantom event
drop order. It still says the `on_execution` order is:

`lookup tracked -> run _check_same_orders -> drop-check -> mark_filled + engine + place`

That is the old order and would be incorrect after Feature 0083 is implemented.
Update the rule text alongside the code so future edits preserve the required
top-of-handler exec-id guard.

## Notes

I did not find data-shape or snake_case/camelCase issues in the existing
normalizer path relevant to this feature: `ExecutionEvent.exec_id` already
exists and Bybit `execId` is mapped to it in the adapter. The blocker is that
the runner never consumes that identity for live execution idempotency.

I did not run the test suite because the reviewed feature is absent from the
current code; the missing tests above are the actionable verification gap.
