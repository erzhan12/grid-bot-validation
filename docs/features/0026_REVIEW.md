# 0026 Review - Disable private-WS message-gap watchdog

## Summary

No blocking findings.

The implementation matches `docs/features/0026_PLAN.md`:

- Adds `message_gap_watchdog_enabled: bool = True` to `PrivateWebSocketClient`.
- Gates only the private heartbeat watchdog start in `connect()`.
- Leaves public watchdog behavior unchanged.
- Passes `message_gap_watchdog_enabled=False` from `Orchestrator._init_account()` for gridbot private WS clients.
- Keeps the private `on_disconnect` callback wired but dormant while the watchdog is disabled.
- Leaves private business-event timestamp updates in place.
- Adds focused adapter and orchestrator tests for the new flag.

## Findings

None.

## Review Notes

- Default behavior is preserved for callers outside gridbot, including `apps/event_saver/src/event_saver/collectors/private_collector.py`, because they do not pass the new flag.
- The reset path is covered by the gate inside `connect()`: `reset()` still stops any existing watchdog, disconnects, and reconnects, and a disabled client does not recreate `_heartbeat_thread`.
- No data-shape or alignment issue was introduced. The changed production code only adds a constructor flag and does not alter message payload parsing, callback payloads, or Bybit `{data: ...}` handling.
- The public client remains untouched, so public message-gap detection still acts as the secondary disconnect signal.
- The implementation is small and does not add new abstractions or large-file pressure.

## Test Review

Coverage added for the new behavior:

- `test_private_watchdog_disabled_skips_heartbeat_thread` verifies disabled private connect still marks the wrapper connected and subscribes execution/order/position/wallet streams.
- `test_private_watchdog_disabled_never_fires_on_disconnect` verifies silence past the threshold does not call `on_disconnect` when disabled.
- `test_private_watchdog_disabled_survives_reset` verifies `reset()` does not re-enable the private watchdog.
- `test_init_account_disables_private_message_gap_watchdog` verifies the orchestrator constructor wiring.

Existing default-on watchdog tests remain unchanged and still cover backwards compatibility.

Residual validation from the plan remains manual: the dev fault-injection Wi-Fi/network-drop test is still required before merge to prove the TCP probe catches real private socket failures within the expected window.

## Verification

```bash
uv run pytest -q packages/bybit_adapter/tests/test_ws_client.py apps/gridbot/tests/test_orchestrator.py
# 154 passed in 5.19s
```

```bash
uv run ruff check packages/bybit_adapter/src/bybit_adapter/ws_client.py packages/bybit_adapter/tests/test_ws_client.py apps/gridbot/src/gridbot/orchestrator.py apps/gridbot/tests/test_orchestrator.py
# All checks passed!
```

I also ran full-repo `uv run ruff check`; it still fails on pre-existing unrelated files under backtest/reference/test areas, not on the files touched by this feature.
