# 0017 Review

Scope reviewed: merged implementation of `0017_PLAN.md` against the current `main` branch, with focus on plan fidelity, runtime regressions, data-shape assumptions, and test coverage.

## Findings

### [P1] Startup `SIGINT` is lost and the bot can continue running after the user already asked it to stop

- Files: `apps/gridbot/src/gridbot/main.py:123-156`, `apps/gridbot/src/gridbot/orchestrator.py:201-270`, `apps/gridbot/src/gridbot/orchestrator.py:440-464`
- `main()` installs the signal handler before calling `orchestrator.start()`, and the handler correctly calls `request_stop()`. The problem is that `start()` never observes that request and unconditionally sets `_running = True` at the end of startup.
- Result: if the operator presses `Ctrl+C` during a long startup path (WS connect, startup reconciliation, initial REST fetch), the first interrupt is effectively ignored. When startup eventually completes, `run()` still starts and the process keeps going. The next interrupt then goes down the hard-exit path (`os._exit(130)`), so there is no graceful shutdown path during startup anymore.
- This needs either a separate shutdown-request flag that `start()` checks between phases, or `start()` must avoid re-arming `_running` when a stop was already requested.

### [P1] Late startup failures skip cleanup entirely because `_started` flips only after the final startup fetch succeeds

- Files: `apps/gridbot/src/gridbot/orchestrator.py:250-270`, `apps/gridbot/src/gridbot/orchestrator.py:466-500`
- `start()` creates run records and connects both WebSockets before the initial position fetch. If that fetch raises (for example `StartupTimeoutError`), `_started` is still `False`.
- `main()` does call `orchestrator.stop()` in `finally`, but `stop()` immediately returns when `_started` is false. That skips WS disconnect and `_update_run_records_stopped()`.
- Impact: failed startups can leave `Run` rows stuck in `"running"` state, and the cleanup path is not executed for partially initialized orchestrators. The current tests only verify that `main()` calls `stop()` on exceptions with a mock orchestrator; they do not cover the real partial-start failure path.

### [P2] Failed position fetches are recorded as if they completed successfully, delaying retries by a full rotation floor

- Files: `apps/gridbot/src/gridbot/orchestrator.py:989-1003`, `apps/gridbot/src/gridbot/orchestrator.py:1023-1032`
- The comments say `_last_position_fetch` stores the timestamp of the last completed fetch, but both startup and steady-state paths update it even when `_fetch_one_account()` raised.
- In steady state this means a transient wallet/REST failure can suppress the next retry for `max(position_check_interval, N * 15s)`. With default settings that is at least 63 seconds, during which wallet balance and position-dependent sizing can stay stale.
- The timestamp should only move on success, or failures need a separate backoff path that is shorter than the normal “fresh data” floor.

### [P2] Ticker routing still assumes `message["data"]["symbol"]` even though the normalizer already supports topic-based fallback

- Files: `apps/gridbot/src/gridbot/orchestrator.py:533-538`, `apps/gridbot/src/gridbot/orchestrator.py:629-648`
- The public WS callback passes `msg.get("data", {}).get("symbol", "")` into `_on_ticker()`, but `BybitNormalizer.normalize_ticker()` already contains fallback logic that extracts the symbol from `topic` when `data.symbol` is absent.
- If a ticker frame arrives without `data.symbol`, the normalizer still produces a valid `TickerEvent`, but the orchestrator stores it under `self._latest_ticker[""]`. That event will never match any real symbol in `_symbol_to_runners`, so the ticker is silently dropped.
- The fix is simple: route by `event.symbol` after normalization instead of by a second parse of the raw message. There is no regression test covering this shape today.

## Tests

Executed:

- `uv run pytest apps/gridbot/tests/test_main.py apps/gridbot/tests/test_retry_queue.py apps/gridbot/tests/test_reconciler.py apps/gridbot/tests/test_runner.py packages/bybit_adapter/tests/test_rest_client.py tests/integration/test_runner_lifecycle.py -q`
- `uv run pytest apps/gridbot/tests/test_orchestrator.py -q`

Result: `314 passed`

## Coverage Gaps

- No test covers `SIGINT` arriving during `orchestrator.start()`.
- No test covers cleanup after a real late-start failure (for example, startup position fetch raising after WS connect / run-record creation).
- No test covers ticker routing when `data.symbol` is missing but `topic` is present.
- No test asserts that failed position fetches do not advance `_last_position_fetch`.
