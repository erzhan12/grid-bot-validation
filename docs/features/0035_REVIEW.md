# 0035 Review - Disable recorder private-WS message-gap watchdog

## Summary

No blocking findings.

The implementation matches `docs/features/0035_PLAN.md`:

- Passes `message_gap_watchdog_enabled=False` from `PrivateCollector.start()` when constructing `PrivateWebSocketClient`.
- Leaves the adapter default as `True`, so unmodified callers keep existing watchdog behavior.
- Leaves public WebSocket watchdog behavior untouched.
- Keeps `on_disconnect` and `on_reconnect` wired, making those callbacks dormant while the private message-gap watchdog is disabled.
- Adds a focused event_saver lifecycle test that verifies the constructor kwarg.

## Findings

None.

## Review Notes

- No data-shape or alignment issue was introduced. The production change only adds a constructor flag and does not alter execution/order/position/wallet message parsing, symbol filtering, callback payloads, or `{data: ...}` handling.
- The change is appropriately small for the plan. It does not add new abstractions, expand file size materially, or change ownership boundaries.
- The test follows the existing `TestLifecycle` pattern in `apps/event_saver/tests/test_private_collector.py`: patch `PrivateWebSocketClient`, call `collector.start()`, and inspect `MockWS.call_args[1]`.
- The plan's documented side effect remains true: recorder-side `PrivateCollector._handle_disconnect`, `_handle_reconnect`, and `on_gap_detected` are no longer actively triggered by this watchdog. That is intentional and mirrors feature 0026.

## Test Review

Coverage added for the new behavior:

- `test_start_disables_private_message_gap_watchdog` verifies the recorder private collector opts out by passing `message_gap_watchdog_enabled=False`.

Existing lifecycle tests still cover client construction, connection, testnet flag wiring, duplicate start behavior, stop/disconnect behavior, and callback handling. Adapter-level tests from feature 0026 already cover the flag semantics and default-on behavior in `PrivateWebSocketClient`.

Residual validation from the plan remains operational: restart the recorder and confirm a quiet private WS does not emit `Detected disconnection: no messages for 30.0s` during the first 5 minutes. Public WS watchdog behavior should remain unchanged.

## Verification

```bash
uv run pytest apps/event_saver/tests/test_private_collector.py packages/bybit_adapter/tests/test_ws_client.py -q
# 65 passed in 4.73s
```

```bash
uv run ruff check apps/event_saver/src/event_saver/collectors/private_collector.py apps/event_saver/tests/test_private_collector.py
# All checks passed!
```

I also ran the broader plan lint command:

```bash
uv run ruff check apps/event_saver/ packages/bybit_adapter/
```

It currently fails on unrelated pre-existing lint issues outside this feature's changed files:

- `apps/event_saver/src/event_saver/collectors/public_collector.py` unused `UTC`
- `apps/event_saver/src/event_saver/main.py` f-string without placeholders
- `apps/event_saver/tests/test_config.py` unused `pytest`
- `apps/event_saver/tests/test_reconciler.py` unused `patch`
- `packages/bybit_adapter/tests/test_normalizer.py` unused `pytest`
- `packages/bybit_adapter/tests/test_rate_limiter.py` unused `pytest`
- `packages/bybit_adapter/tests/test_rest_client.py` unused `logging`
