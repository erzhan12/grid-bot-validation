# 0024 Review — Active WS reconnect with socket-level health probe

## Verdict

The core plan is implemented: both WS wrappers expose `is_socket_alive()`, pass `retries=0` to pybit, provide `reset()` that disconnects and reconnects with subscription replay, and the orchestrator has both a 10s TCP-level health probe and a heartbeat-gap secondary reset path.

I found no blocking production issue in the orchestrator path. The main residual issue remains open after recheck: the orchestrator avoids self-join by dispatching heartbeat-triggered resets to a worker thread, but `PublicWebSocketClient.reset()` / `PrivateWebSocketClient.reset()` themselves are still unsafe to call directly from their own `on_disconnect` callbacks.

Verification run:

```bash
uv run pytest packages/bybit_adapter/tests/test_ws_client.py apps/gridbot/tests/test_orchestrator.py -q
# 133 passed in 4.85s
```

Recheck note: no tracked or staged WS-code diff was present during this review update, and both `_stop_heartbeat_watchdog()` implementations still call `self._heartbeat_thread.join(timeout=2.0)` without guarding against `threading.current_thread() is self._heartbeat_thread`.

## Findings

### P2 — `reset()` still self-joins when called directly from the heartbeat callback

`_heartbeat_loop()` fires `self.on_disconnect(disconnect_ts)` from the heartbeat thread. If a consumer of `PublicWebSocketClient` or `PrivateWebSocketClient` calls `client.reset()` directly in that callback, `reset()` calls `_stop_heartbeat_watchdog()`, which calls `self._heartbeat_thread.join(timeout=2.0)` on the current thread. Python raises `RuntimeError: cannot join current thread`, the callback is logged as an error, and no reconnect happens through that direct wrapper API path.

The orchestrator-specific implementation is safe because `_on_ws_disconnect()` dispatches `client.reset()` to a one-shot worker thread. The issue is that the wrapper still exposes the sharp edge, and the new test named `test_reset_from_disconnect_callback_does_not_hang` no longer tests the direct callback case described in the plan; it tests the orchestrator-style worker workaround instead.

Affected locations:

- `packages/bybit_adapter/src/bybit_adapter/ws_client.py:206`
- `packages/bybit_adapter/src/bybit_adapter/ws_client.py:492`
- `packages/bybit_adapter/src/bybit_adapter/ws_client.py:301`
- `packages/bybit_adapter/src/bybit_adapter/ws_client.py:602`
- `packages/bybit_adapter/tests/test_ws_client.py:635`

Suggested fix: make `_stop_heartbeat_watchdog()` skip `join()` when `threading.current_thread() is self._heartbeat_thread`, or make the wrapper-level callback path explicitly dispatch resets internally. Then add the direct regression test: set `on_disconnect=lambda ts: client.reset()`, let the heartbeat fire, and assert the client reconnects or at least does not raise/log the self-join path.

## Plan Alignment

Implemented correctly:

- `PublicWebSocketClient.is_socket_alive()` and `PrivateWebSocketClient.is_socket_alive()` delegate to pybit's `WebSocket.is_connected()` under the wrapper lock and return `False` on missing socket or pybit check exception.
- Public and private `connect()` pass `retries=0` to `WebSocket(...)`.
- Public and private `reset()` stop the watchdog, call `_disconnect_internal()`, then call `connect()`, so subscriptions are replayed by existing connect logic.
- `_WS_HEALTH_CHECK_INTERVAL = 10.0` exists and `_tick()` advances `_next_ws_health_check` before invoking `_ws_health_check_once()`.
- `_ws_health_check_once()` checks every public/private client, resets dead sockets, and isolates per-client exceptions with notifier alerts.
- `_init_account()` wires `on_disconnect` for public and private WS clients.
- `_on_ws_disconnect()` resets only the corresponding account/kind and catches reset exceptions.

No snake_case/camelCase or `{data: ...}` nesting issue was introduced by this feature. The only data-shape-sensitive callback touched here is ticker symbol extraction in `_init_account()`, which preserves the existing `msg.get("data", {}).get("symbol", "")` pattern.

## Test Review

Coverage strengths:

- Adapter tests cover `is_socket_alive()` before connect, true/false pybit delegation, pybit exception swallowing, public/private reset resubscription, exit-exception tolerance, and `retries=0`.
- Orchestrator tests cover dead public/private reset, alive skip, rate limiting through `_tick()`, per-client reset isolation, corresponding-client reset from `_on_ws_disconnect()`, missing account safety, and notifier alert on reset failure.
- The suite is fast and uses mocks for pybit and orchestrator WS clients, so it remains isolated from external services.

Residual gaps:

- The direct heartbeat-callback `client.reset()` edge is not covered; the current test covers the worker-dispatched workaround.
- There is no private-client equivalent for `is_socket_alive()` exception swallowing or reset idempotence when `exit()` raises. Public coverage makes the shared pattern likely correct, but private has separate code and should get mirrored regression tests.
- There is no test that verifies `_init_account()` passes the `on_disconnect` callback into the mocked constructors with the expected account binding. Current behavior is indirectly covered by calling `_on_ws_disconnect()` directly, but constructor-wiring coverage would protect the integration point.

## Structure / Style

The implementation is small and stays in the expected modules. The orchestrator now has both the old logical `is_connected()` health check and the new socket-level check; that duplication is intentional per the plan because the two checks detect different failure modes.

The only style concern is that the WS wrapper docstrings imply `reset()` is broadly safe around disconnect detection, while direct callback use still has the self-join edge noted above. Either harden the method or narrow the docstring contract.
