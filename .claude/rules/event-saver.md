---
paths:
  - "apps/event_saver/**"
  - "apps/recorder/**"
---

## Private WS disconnect handling (event_saver / recorder)

**Feature 0035 — private WS message-gap watchdog disabled on recorder side**:
- **Parity with gridbot feature 0026**: pybit ping/pong frames bypass business-event handler, so the 30s message-gap watchdog produces false-positive disconnects on a healthy quiet private WS. Recorder now passes `message_gap_watchdog_enabled=False` to `PrivateWebSocketClient`, matching `gridbot.orchestrator._init_account`.
- **Feature 0037 follow-up**: Recorder keeps a private TCP-level health probe in `PrivateCollector` while the message-gap watchdog stays disabled. On a dead private socket it resets the client and invokes the existing private gap callback so REST execution reconciliation runs for the outage window.
- **Invariant**: Do not remove both private disconnect detectors. Message silence is not a private-stream failure signal, but the recorder still needs TCP-level liveness checks so real private WS outages do not silently skip execution backfill.
- File: `apps/event_saver/src/event_saver/collectors/private_collector.py`

**Feature 0039 — bound private WS reset/disconnect with daemon thread + wait_for**:
- **Why not `asyncio.to_thread` for pybit reset/disconnect**: the default `ThreadPoolExecutor` is joined by `concurrent.futures.thread._python_exit` at interpreter shutdown. A parked pybit call would block interpreter exit, moving the hang from `stop()` to `atexit` where it is also non-responsive to SIGTERM.
- **Pattern**: wrap any potentially-hanging blocking call from a recorder collector path in `_run_in_daemon_thread(fn)` (a daemon `threading.Thread` bridged to the loop via `loop.create_future()` + `call_soon_threadsafe`) and bound it with `asyncio.wait_for(...)`. The daemon flag is load-bearing — daemon threads are not joined at interpreter exit.
- **Cancellation safety**: the completer must guard on `fut.done()` before `set_result` / `set_exception` (so a late-returning abandoned worker does not raise `InvalidStateError`) and swallow `RuntimeError` from `call_soon_threadsafe` (so a worker that returns after the loop closed exits cleanly).
- **Shutdown invariant**: if a prior `reset()` timed out, the worker is still holding `PrivateWebSocketClient._lock`; `stop()` must **skip** `disconnect()` (it would deadlock on the same lock) and clear the client reference — the daemon thread leaks until the process exits. This is the explicit "abandon" trade-off documented in `docs/features/0039_PLAN.md`.
- **Don't touch an abandoned client from the event loop**: `PrivateWebSocketClient.is_socket_alive()` (`ws_client.py:504`) acquires the same `_lock` the parked reset worker holds. After `_ws_reset_abandoned` is set, `_ws_health_check_once()` must return early before any lock-taking method on the client runs — otherwise the next health tick blocks the event loop and reintroduces the SIGTERM hang.
- **Pybit daemon verification**: pybit's `WebSocket` worker thread is started with `self.wst.daemon = True` (`.venv/lib/python3.12/site-packages/pybit/_websocket_stream.py:168-169`). Verified once for the abandon strategy — the OS reclaims the leaked thread at process exit. If the pybit version changes, re-check this line.
- **Tests**: `try/finally` release of `threading.Event` gates is mandatory so parked worker threads do not leak between tests.
- Files: `apps/event_saver/src/event_saver/collectors/private_collector.py:_run_in_daemon_thread`, `_ws_health_check_once`, `stop`.

## event_saver — Data Capture

**Path**: `apps/event_saver/`

### Key Rules

- `DatabaseFactory` expects `DatabaseSettings` object, NOT a raw URL string
- `PrivateExecution` model uses `exec_price`, `exec_qty`, `exec_fee` (not `price`, `qty`, `fee`)
- `run_id` is REQUIRED for PrivateExecution FK; events without it are filtered out
- `symbols` field is string — use `config.get_symbols()` to get list
- `PublicTradeRepository.exists_by_trade_id()` takes only `trade_id` (no symbol param)

### Environment Variables

`EVENTSAVER_SYMBOLS`, `EVENTSAVER_TESTNET`, `EVENTSAVER_BATCH_SIZE`, `EVENTSAVER_FLUSH_INTERVAL`, `EVENTSAVER_GAP_THRESHOLD_SECONDS`, `EVENTSAVER_DATABASE_URL`

---

