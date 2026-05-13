# 0037 Review — Recorder private WS TCP health reconciliation (second pass)

## Summary

The latest commit on `feature/0037-private-ws-health-reconciliation` addresses
the most impactful finding from the first review (**F3 — event-loop blocking
during reset**) and adds a dedicated test for the stop-during-reset race.

No blocking findings. Two non-blocking design notes from the first review
(F1, F2) remain valid as future follow-ups; they were not in scope for this
change and the recorder behaves correctly without them. Tests, lint, and the
event_saver + recorder suites all pass.

## What changed since the first review

- `client.reset()` is now offloaded to a worker thread:
  `await asyncio.to_thread(client.reset)` at
  `private_collector.py:229`. The event loop no longer stalls during the
  TCP teardown + WS handshake + 4-stream re-subscribe sequence. **F3
  resolved.**
- Shutdown switched from `task.cancel()` to cooperative shutdown via an
  `asyncio.Event` (`_ws_health_stop_event`). The loop races
  `stop_event.wait()` against the sleep interval through
  `asyncio.wait_for(stop_event.wait(), timeout=...)`
  (`private_collector.py:191-205`). On `stop()`, the event is set, the loop
  observes it on the next iteration, and exits without raising
  `CancelledError`.
- `stop()` ordering hardened: flips `_running = False`, sets the event,
  awaits the in-flight task to completion, then disconnects the WS client
  (`private_collector.py:160-179`). This guarantees `reset()` (which may be
  running in `asyncio.to_thread`) finishes before `disconnect()` runs — no
  pybit-state corruption from interleaved teardown.
- New test
  `TestLifecycle.test_stop_waits_for_in_flight_health_reset_before_disconnect`
  (`test_private_collector.py:212-256`) injects a real
  `threading.Event` gate into the mocked `reset()` and asserts the strict
  event order `["reset_start", "reset_done", "disconnect"]` plus that
  `stop()` does not complete while reset is in flight.
- Existing focused test converted to `async` to match the now-async
  `_ws_health_check_once`
  (`test_private_collector.py:189-210`).

## Implementation vs Plan

All plan items still satisfied, with the algorithm reading as follows:

- `message_gap_watchdog_enabled=False` preserved → `private_collector.py:147`.
- `ws_health_check_interval` kwarg with module default
  `_PRIVATE_WS_HEALTH_CHECK_INTERVAL = 10.0` → `private_collector.py:18, 86,
  97, 117`.
- `start()` creates the `asyncio.Event`, spawns the loop task, in that order,
  after `connect()` succeeds → `private_collector.py:150-152`.
- `stop()` flips `_running`, signals the event, awaits the task, nulls the
  event + task, then disconnects → `private_collector.py:164-177`.
- Health algorithm steps 1–10 are present and async-correct
  → `_ws_health_check_loop` and `_ws_health_check_once` at
  `private_collector.py:191-237`.
- Gap-start fallback chain `last_message_ts → disconnected_at → now(UTC)` →
  `private_collector.py:218-222`.
- `_handle_disconnect` → off-loop `reset()` → `_handle_reconnect` ordering →
  `private_collector.py:223-230`.
- `try / except Exception` around the whole check protects the loop →
  `private_collector.py:231-237`.

Recorder-side integration is unchanged (`Recorder._handle_private_gap` still
schedules `reconcile_executions` per symbol via
`asyncio.run_coroutine_threadsafe`).

## Findings

### Resolved

- **F3 — `client.reset()` blocking the event loop. RESOLVED.** Now executed
  via `asyncio.to_thread`. Verified by inspection and by the new
  stop-during-reset test, which exercises a real `threading.Event` gate and
  confirms the event loop continues to run while the worker thread is held
  inside `reset()` (`stop_task` is created, `await asyncio.sleep(0.01)`
  proceeds, `stop_task.done()` is `False`, and `"disconnect"` is not yet in
  the trace).

### Still open (non-blocking, deferred follow-ups)

- **F1 — `gap_start` can be arbitrarily stale on a quiet account.** With the
  message-gap watchdog disabled, `state.last_message_ts` is only refreshed
  by business messages. If the channel is quiet for an hour and the socket
  then dies, the gap window passed to `reconcile_executions` is an hour
  wide. Cost is REST work, not correctness (writer is idempotent on
  `exec_id`). Mitigation when needed: stash the last "socket alive"
  timestamp inside the probe and use it as the lower bound.
- **F2 — Sustained outage causes per-tick reconciliation.** If `reset()`
  cannot restore the socket within one interval, every subsequent tick
  re-runs `_handle_disconnect → reset → _handle_reconnect`. Each iteration
  increments `Recorder._gap_count` (`recorder.py:854-855`) and schedules
  one reconcile per configured symbol. Idempotent at DB level, but the
  metric and the REST call rate inflate. Mitigation when needed: track the
  last reconciled `gap_start` and skip when unchanged, or only call
  `_handle_reconnect` when `is_socket_alive()` is true *after* reset.
- **F4 — `Recorder._handle_private_gap` is now called from inside its own
  event loop.** `asyncio.run_coroutine_threadsafe(coro, self._event_loop)`
  works in-loop because `.result()` is never invoked on the returned
  future, but the natural API for in-loop scheduling is
  `asyncio.create_task` / `asyncio.ensure_future`. Cosmetic; refactor if
  the callback ever grows a `result()` call.

### New observations

- **N1 — `stop()` has no timeout on the awaited task.** If `client.reset()`
  truly hangs (e.g., pybit's `connect()` gets stuck on a half-open TCP
  handshake without surfacing an error), `await self._ws_health_task` will
  block forever and so will the recorder's shutdown path. Low likelihood;
  worth knowing for SIGTERM behavior. If observed, wrap the await with
  `asyncio.wait_for(..., timeout=...)` and fall back to canceling /
  abandoning the task. **Addressed by Feature 0039** — `reset()` and
  `disconnect()` now run on a daemon thread bounded by `asyncio.wait_for`,
  with abandonment-aware shutdown.
- **N2 — `contextlib.suppress(asyncio.CancelledError)` in `stop()` is
  defensive only.** The new design removed the explicit `cancel()` call, so
  the loop should now exit cleanly via the stop event. Keeping `suppress`
  is harmless and protects against external cancellation, but the comment
  trail (it used to be the primary shutdown signal) is now stale.
- **N3 — The `_ws_health_check_loop` checks `while self._running` and also
  `if stop_event is None: return`.** Two redundant exit conditions in the
  same loop. Not a bug, but a single source of truth (the stop event)
  would be tidier.

## Subtle points worth knowing (non-issues)

- The new test bypasses `start()` and constructs the task directly with
  `asyncio.create_task(collector._ws_health_check_once())`. This is the
  right level for testing the interlock semantics, since `start()` would
  immediately spawn the regular loop and create extra timing variables.
- `_ws_health_check_once` catches `Exception`, not `BaseException`, so
  `asyncio.CancelledError` is correctly excluded. Combined with the new
  no-cancel shutdown, the only way the task surfaces an unexpected
  `CancelledError` is via outside-the-collector cancellation, which the
  `suppress` in `stop()` handles.
- The `disconnected_at` fallback chain still produces a "best-effort"
  timestamp; `now(UTC)` is only reached if both `last_message_ts` and
  `disconnected_at` are `None`, which only happens if the socket dies
  immediately after connect with no prior detected disconnect.
- Logging style remains mixed (`_handle_*` use f-strings; the new probe uses
  lazy `%s`). Cosmetic.

## Test Review

### New / updated tests

- `test_private_ws_health_resets_dead_socket_and_reconciles`
  (`test_private_collector.py:189-210`) — now async; awaits
  `collector._ws_health_check_once()`. Asserts `reset` called once and
  `on_gap_detected` called with `gap_start == last_message_ts` and
  `gap_end ≥ gap_start`.
- `test_stop_waits_for_in_flight_health_reset_before_disconnect`
  (`test_private_collector.py:212-256`) — new. Uses two `threading.Event`s
  to gate `reset()` start/finish. Asserts that while `reset()` is blocked
  in the worker thread:
  - `stop()` does not return,
  - `disconnect()` has not been called,
  - the event loop is still responsive (the test's own `await
    asyncio.sleep(0.01)` resolves),
  and that after releasing the gate the order is
  `["reset_start", "reset_done", "disconnect"]`.

This second test is the most valuable addition: it directly exercises the
F3/N1 boundary (loop responsiveness during reset) and the stop-ordering
invariant.

### Coverage gaps still open (not blocking)

- Alive-socket short-circuit: `is_socket_alive() → True` should produce no
  `reset`, no `on_gap_detected`. Trivial async test.
- Fallback chain: `last_message_ts=None` → `disconnected_at`; both `None` →
  `datetime.now(UTC)`. Currently only the `last_message_ts` branch is
  pinned.
- Exception isolation: have `is_socket_alive` or `reset` raise; assert the
  task does not propagate and a subsequent call still works.
- Loop integration: set `ws_health_check_interval=0.01`, run one or two
  ticks, then `stop()` — verifies the wait_for/timeout path *and* the
  stop-event exit path end-to-end.

### Placement

The two probe tests sit inside `TestLifecycle` but are about the health
check, not the start/stop API. A `TestHealthCheck` class (consistent with
the file's behavior-grouped layout) would make the file easier to navigate.
Cosmetic.

## Data alignment

- `ConnectionState` field names (`last_message_ts`, `disconnected_at`)
  match the dataclass at `ws_client.py:34-51`. No camelCase confusion.
- `_handle_reconnect(disconnected_at, reconnected_at)` argument order
  matches `Recorder._handle_private_gap(gap_start, gap_end)`
  (`recorder.py:849-851`).
- `asyncio.to_thread(client.reset)` passes the bound method; no positional
  arguments needed — `reset()` takes none.

## Over-engineering / file size

None. Net change vs the previous round adds ~10 lines (the `Event`, the
`wait_for`/timeout pattern, the `to_thread` call) plus one focused
asyncio test. No new abstractions or files beyond PLAN / REVIEW.

## Verification

```
uv run pytest apps/event_saver/tests/test_private_collector.py packages/bybit_adapter/tests/test_ws_client.py -q
# 68 passed in 4.77s

uv run pytest apps/event_saver/tests -q
# 151 passed in 1.90s

uv run pytest apps/recorder/tests -q
# 76 passed, 2 skipped in 0.82s

uv run ruff check apps/event_saver/src/event_saver/collectors/private_collector.py apps/event_saver/tests/test_private_collector.py
# All checks passed!
```

`uv run ruff check apps/event_saver packages/bybit_adapter` still surfaces
7 unrelated pre-existing F401 warnings in `test_rate_limiter.py`,
`test_rest_client.py`, etc. — not introduced by this branch.

## Branch hygiene

- Branch: `feature/0037-private-ws-health-reconciliation`, one commit
  (`6ea3aa4 Add recorder private WS health reconciliation`).
- `git status` still shows uncommitted changes to:
  - `apps/replay/conf/replay_ltcusdt_phase4.yaml`
  - `apps/replay/src/replay/engine.py`
  These are unrelated to 0037 and must not be included in the PR. Stash,
  revert, or commit on a separate branch.
- PR #80 (`cursor/critical-bug-inspection-0c30`) remains intentionally
  superseded by this branch per `0037_PLAN.md` Context. Close it after
  merge.

## Recommendation

Ship. The F3 fix removes the only "likely to surprise in production"
behavior. F1, F2, and N1 are bounded risks worth tracking as follow-ups
but do not block merging this PR; the recorder's REST reconciliation is
idempotent and the event loop is no longer stalled by reset. The new
stop-during-reset test is a strong correctness guarantee for the shutdown
path.
