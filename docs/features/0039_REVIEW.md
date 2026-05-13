# Feature 0039 Review — Bound private WS reset & disconnect

## Findings

No blocking issues found in the updated implementation.

The previous P1 is addressed: `_ws_health_check_once()` now returns early when
`_ws_reset_abandoned` is set, before calling any lock-taking method on the
abandoned `PrivateWebSocketClient`. This preserves the shutdown guarantee after
a reset timeout because the next health tick cannot block the event loop on the
adapter lock held by the parked daemon worker.

The previous N1 is also addressed in `RULES.md`: the pybit daemon-thread
verification is documented, including the installed pybit location where
`self.wst.daemon = True` is set before `start()`.

## Coverage

- Reset timeout skips REST gap reconciliation and sets `_ws_reset_abandoned`.
- `stop()` skips `disconnect()` after an abandoned reset and clears the client
  reference.
- A subsequent health check does not touch the abandoned client.
- Restart clears the abandoned flag for a fresh client.
- Disconnect is bounded when there was no prior reset timeout.
- `_run_in_daemon_thread()` is covered for daemon creation, late completion
  after cancellation, and late completion after loop close.
- Existing happy-path reset ordering is still covered by
  `test_stop_waits_for_in_flight_health_reset_before_disconnect`.

## Verification

- `uv run pytest apps/event_saver/tests/test_private_collector.py -q` — passed
  (`39 passed`)
- `uv run pytest apps/event_saver/tests -q` — passed (`159 passed`)
- `git diff --check` — passed
- `uv run ruff check apps/event_saver` — failed on pre-existing unrelated lint
  issues:
  - `apps/event_saver/src/event_saver/collectors/public_collector.py:4` unused
    `UTC`
  - `apps/event_saver/src/event_saver/main.py:504` f-string without placeholders
  - `apps/event_saver/tests/test_config.py:3` unused `pytest`
  - `apps/event_saver/tests/test_reconciler.py:7` unused `patch`
