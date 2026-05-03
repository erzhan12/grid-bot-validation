# 0024 Review — Active WS reconnect with socket-level health probe

## Verdict

The core plan is implemented: both WS wrappers expose `is_socket_alive()`, pass `retries=0` to pybit, provide `reset()` that disconnects and reconnects with subscription replay, and the orchestrator has both a 10s TCP-level health probe and a heartbeat-gap secondary reset path.

The previous P2 self-join finding is resolved. Both wrapper implementations now skip `Thread.join()` when `_stop_heartbeat_watchdog()` is called from the heartbeat thread itself, and the adapter test now covers direct inline `client.reset()` from `on_disconnect`.

The previous P3 lint finding is resolved. `packages/bybit_adapter/src/bybit_adapter/ws_client.py` no longer imports unused `time`.

Verification run:

```bash
uv run pytest packages/bybit_adapter/tests/test_ws_client.py apps/gridbot/tests/test_orchestrator.py -q
# 136 passed in 4.67s
```

Lint run:

```bash
uv run ruff check packages/bybit_adapter/src/bybit_adapter/ws_client.py packages/bybit_adapter/tests/test_ws_client.py apps/gridbot/src/gridbot/orchestrator.py apps/gridbot/tests/test_orchestrator.py
# All checks passed!
```

## Findings

No blocking findings found.

## Resolved Since Prior Review

- Public and private `_stop_heartbeat_watchdog()` now skip `join()` when the caller is the heartbeat thread, preventing `RuntimeError: cannot join current thread`.
- `test_reset_directly_from_disconnect_callback_does_not_self_join` now exercises the direct wrapper callback path instead of only the orchestrator worker-dispatch workaround.
- The orchestrator `_on_ws_disconnect()` docstring was updated to describe why it still dispatches reset to a worker even though wrapper-level direct reset no longer self-joins.
- Removed the unused `time` import from `packages/bybit_adapter/src/bybit_adapter/ws_client.py`, restoring a clean ruff check.

## Plan Alignment

Implemented correctly:

- `PublicWebSocketClient.is_socket_alive()` and `PrivateWebSocketClient.is_socket_alive()` delegate to pybit's `WebSocket.is_connected()` under the wrapper lock and return `False` on missing socket or pybit check exception.
- Public and private `connect()` pass `retries=0` to `WebSocket(...)`.
- Public and private `reset()` stop the watchdog, call `_disconnect_internal()`, then call `connect()`, so subscriptions are replayed by existing connect logic.
- `_WS_HEALTH_CHECK_INTERVAL = 10.0` exists and `_tick()` advances `_next_ws_health_check` before invoking `_ws_health_check_once()`.
- `_ws_health_check_once()` checks every public/private client, resets dead sockets, and isolates per-client exceptions with notifier alerts.
- `_init_account()` wires `on_disconnect` for public and private WS clients.
- `_on_ws_disconnect()` resets only the corresponding account/kind and catches reset exceptions.
- Direct wrapper-level `on_disconnect -> client.reset()` no longer self-joins.

No snake_case/camelCase or `{data: ...}` nesting issue was introduced by this feature. The only data-shape-sensitive callback touched here is ticker symbol extraction in `_init_account()`, which preserves the existing `msg.get("data", {}).get("symbol", "")` pattern.

## Test Review

Coverage strengths:

- Adapter tests cover `is_socket_alive()` before connect, true/false pybit delegation, pybit exception swallowing, public/private reset resubscription, exit-exception tolerance, direct heartbeat-callback reset without self-join, and `retries=0`.
- Orchestrator tests cover dead public/private reset, alive skip, rate limiting through `_tick()`, per-client reset isolation, corresponding-client reset from `_on_ws_disconnect()`, missing account safety, and notifier alert on reset failure.
- The suite is fast and uses mocks for pybit and orchestrator WS clients, so it remains isolated from external services.

Residual gaps:

- There is no private-client equivalent for `is_socket_alive()` exception swallowing or reset idempotence when `exit()` raises. Public coverage makes the shared pattern likely correct, but private has separate code and should get mirrored regression tests.
- There is no test that verifies `_init_account()` passes the `on_disconnect` callback into the mocked constructors with the expected account binding. Current behavior is indirectly covered by calling `_on_ws_disconnect()` directly, but constructor-wiring coverage would protect the integration point.

## Structure / Style

The implementation is small and stays in the expected modules. The orchestrator now has both the old logical `is_connected()` health check and the new socket-level check; that duplication is intentional per the plan because the two checks detect different failure modes.

The wrapper-level heartbeat stop comments now match the implementation. No style issue remains in the reviewed touched-file lint scope.
