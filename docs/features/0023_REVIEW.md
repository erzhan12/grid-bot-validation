# 0023 Review — Push WS position updates to runner via main-loop drain

## Verdict

No blocking findings found in the current implementation.

The two issues from the prior review are resolved:

- P1 incomplete WS cache could zero the opposite runner side: fixed by skipping publication from `_on_position()` until both `Buy` and `Sell` sides are present in the WS cache.
- P2 startup priming could iterate a WS-mutated dict: fixed by extracting `_prime_position_seq()` and snapshotting both outer and inner dict items via `list(...)`.

The implementation now matches the feature plan's core behavior: `PositionFetcher` publishes a post-cache-write callback, the orchestrator coalesces by `(account, symbol)`, `_tick()` drains before ticker processing, unchanged seqs are skipped, runner exceptions are isolated, incomplete WS snapshots are not dispatched, and the REST position cycle remains in place as fallback.

Verification run:

```bash
uv run pytest apps/gridbot/tests/test_orchestrator.py apps/gridbot/tests/test_position_fetcher.py -q
# 128 passed in 0.75s

uv run ruff check apps/gridbot/src/gridbot/orchestrator.py apps/gridbot/src/gridbot/position_fetcher.py apps/gridbot/tests/test_orchestrator.py apps/gridbot/tests/test_position_fetcher.py
# All checks passed
```

## Findings

No actionable findings in the current implementation.

## Resolved Since Prior Review

### P1 — Incomplete WS cache no longer publishes half-snapshots

`Orchestrator._on_position()` now reads both sides and returns early if either side is missing:

- `apps/gridbot/src/gridbot/orchestrator.py:731-749`

That prevents `_tick()` from calling `runner.on_position_update(long_position=<fresh>, short_position=None)` or the symmetric case, which previously could coerce the missing side to zero in `runner.py`.

Regression coverage was added:

- `apps/gridbot/tests/test_orchestrator.py:974` verifies a one-sided WS message is not dispatched and does not create a half-snapshot.
- `apps/gridbot/tests/test_orchestrator.py:996` verifies dispatch resumes once the second side arrives and the snapshot is complete.

### P2 — Startup priming no longer iterates live WS-written dict views

Startup now calls `_prime_position_seq()`, which snapshots both dict levels before iterating:

- `apps/gridbot/src/gridbot/orchestrator.py:261-265`
- `apps/gridbot/src/gridbot/orchestrator.py:674-695`

This removes the `RuntimeError: dictionary changed size during iteration` risk while pybit WS threads are already running.

Regression coverage was added:

- `apps/gridbot/tests/test_orchestrator.py:1017` exercises `_prime_position_seq()` against a mutating inner mapping and confirms the originally visible snapshot is primed without raising.

## Plan Alignment

Implemented correctly:

- `PositionFetcher.__init__` accepts an optional `on_position_changed` callback.
- `on_position_message()` keeps the existing cache write and notifies after successful writes.
- Notifications are deduped to one callback per `(account, symbol)` per WS message.
- Orchestrator wires the callback into `PositionFetcher`.
- Orchestrator uses a coalesced latest-wins position slot and a seq map rather than FIFO queueing.
- `_tick()` drains position snapshots before ticker processing, so a complete position push can affect same-tick order placement.
- Startup seq priming avoids duplicate first-tick redispatch.
- The periodic REST-backed `fetch_and_update()` path is still present and handles incomplete WS cache gaps.

No snake_case/camelCase or `{data: ...}` nesting mismatch was found. `on_position_message()` still expects Bybit's existing `{"data": [...]}` shape and stores each `pos` dict as-is, matching the prior cache contract.

## Test Review

Coverage strengths:

- Orchestrator tests cover dispatch, unchanged-seq idempotency, latest-wins coalescing, new push after drain, unmatched symbols, runner exception isolation, incomplete-cache skip, dispatch after the second side arrives, and startup seq priming.
- Position fetcher tests cover callback firing, side-deduping by symbol, unregistered callback backward compatibility, filtered messages, callback exception isolation, and skipping callback after cache-write failure.
- Tests are fast, isolated, and use mocks for REST and WebSocket clients.

Residual non-blocking note:

- The `_prime_position_seq()` regression test is a reasonable unit-level guard for the extracted helper, but it is not a true multithreaded stress test. Given the implementation snapshots with `list(...)`, this is acceptable for unit scope.

## Structure / Style

The feature remains small and placed in the right modules. `_prime_position_seq()` is a useful extraction because it makes the startup priming behavior testable. The incomplete-cache guard is documented where the decision is made, and the REST fallback remains the right place to handle missing WS sides.
