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

**Feature 0110 B1b — readiness, silent reconnects, gap start**:
- `PrivateCollector` builds its client with `track_subscription_acks=True` and confirms readiness (`_confirm_ready`: identity baseline read BEFORE `wait_ready(_PRIVATE_READY_TIMEOUT)`, run in a daemon thread bounded by a timeout) at `start()` and after every reset. In a health probe the wait also races `_ws_health_stop_event`, so `stop()` never waits out a slow auth reply (`test_auth_wait_keeps_shutdown_responsive`); the not-ready ERROR is skipped when stopping.
- Healthy = confirmed ready AND `is_socket_alive()` AND `is_authenticated()` AND `socket_identity() is _identity_baseline`. A healthy probe stamps `_last_healthy_ts`. Message age is NOT a health signal.
- Gap start = `_last_healthy_ts − _LIVENESS_MARGIN` (75 s), never `last_message_ts` (that gave multi-day windows on quiet accounts). A short blip therefore shows as a ~90 s gap; dedupe makes the wider REST query harmless.
- Not ready at `start()`: ERROR log, no raise — the first probe resets. Not ready after a reset: ERROR log, no gap reported, `_last_healthy_ts` unchanged, so the next probe retries and reports the gap from the original start. A reset timeout still reports no gap (B1c opens the gap row before reset). A socket that can never become ready (e.g. a subscription rejected for missing API-key permission) is reset every probe with an ERROR each time — deliberate: without its execution subscription the socket records nothing. `connect()` in `start()` still runs on the loop (pre-B1b); moving it off-loop is B1c.
- Test fakes for the private client must provide `wait_ready`, `socket_identity`, `is_authenticated` and a `reset` that changes identity (see `FakePrivateWS` in `apps/recorder/tests/test_recorder_gap_reconciliation.py`); tests that expect a gap set `_last_healthy_ts`, not `last_message_ts`.

**Feature 0039 — bound private WS reset/disconnect with daemon thread + wait_for**:
- **Why not `asyncio.to_thread` for pybit reset/disconnect**: the default `ThreadPoolExecutor` is joined by `concurrent.futures.thread._python_exit` at interpreter shutdown. A parked pybit call would block interpreter exit, moving the hang from `stop()` to `atexit` where it is also non-responsive to SIGTERM.
- **Pattern**: wrap any potentially-hanging blocking call from a recorder collector path in `_run_in_daemon_thread(fn)` (a daemon `threading.Thread` bridged to the loop via `loop.create_future()` + `call_soon_threadsafe`) and bound it with `asyncio.wait_for(...)`. The daemon flag is load-bearing — daemon threads are not joined at interpreter exit.
- **Cancellation safety**: the completer must guard on `fut.done()` before `set_result` / `set_exception` (so a late-returning abandoned worker does not raise `InvalidStateError`) and swallow `RuntimeError` from `call_soon_threadsafe` (so a worker that returns after the loop closed exits cleanly).
- **Shutdown invariant**: if a prior `reset()` timed out, the worker is still holding `PrivateWebSocketClient._lock`; `stop()` must **skip** `disconnect()` (it would deadlock on the same lock) and clear the client reference — the daemon thread leaks until the process exits. This is the explicit "abandon" trade-off documented in `docs/features/0039_PLAN.md`.
- **Don't touch an abandoned client from the event loop**: `PrivateWebSocketClient.is_socket_alive()`, `is_authenticated()` and `socket_identity()` acquire the same `_lock` the parked reset worker holds. After `_ws_reset_abandoned` is set, `_ws_health_check_once()` must return early before any lock-taking method on the client runs — otherwise the next health tick blocks the event loop and reintroduces the SIGTERM hang.
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
- **REST execution recovery (feature 0110, audit #270 F4)** — `GapReconciler._executions_to_models` must NOT filter on a per-row `category` (REST rows carry none; `BybitRestClient.get_executions` validates the envelope). PnL comes from `reconciler._rest_exec_pnl`: explicit `execPnl`/`closedPnl` wins; else `closedSize` parsing to exactly 0 → known `Decimal("0")` (opening fill: nothing closed); else `None` (unknown). Any parse error degrades to `None` — never let a malformed field raise into the per-row `except Exception` (which skips the row: a fail-open missing fill). The original REST row is stored in `raw_json`.
- **Recovery outcome (feature 0110 B1a)** — `reconcile_executions` returns `ExecutionRecoveryResult(status, inserted, duplicates, reason)` (`grid_db.RecoveryStatus`) and never raises: `SKIPPED` below `gap_threshold_seconds`; `TRUNCATED` at `max_pages` with a cursor left (nothing persisted); `FAILED` for no `run_id`, a REST/DB/commit/conversion error, or any dropped row (a Trade row that fails conversion, a non-dict entry, or a dict without `execType` — valid rows are still persisted); else `RECOVERED` (zero rows is fine). Query window is `[gap_start − 5 s, gap_end + 5 s]` (`_RECOVERY_WINDOW_MARGIN`). Over Bybit's 7-day `endTime − startTime` cap it queries only the latest 7 days, persists them, and returns `FAILED` naming the unrecovered head. `duplicates` counts distinct `exec_id`s (the batch is deduped before insert). The old back-anchor to the account's last persisted execution (`get_last_execution_ts`, removed) is gone: executions missed for reasons other than a detected socket outage are no longer re-queried. Each dropped row is logged at WARNING by `execId` (or a truncated repr for a non-dict). Map a finished recovery future with `recovery_result_from_future` (cancelled / crashed → `FAILED`). Standalone EventSaver only logs outcomes; the recorder persists them (see `recorder.md`).

### Environment Variables

`EVENTSAVER_SYMBOLS`, `EVENTSAVER_TESTNET`, `EVENTSAVER_BATCH_SIZE`, `EVENTSAVER_FLUSH_INTERVAL`, `EVENTSAVER_GAP_THRESHOLD_SECONDS`, `EVENTSAVER_DATABASE_URL`

---

