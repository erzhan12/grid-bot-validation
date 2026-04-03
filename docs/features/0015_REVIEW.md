# 0015 Review — Auth Error Cooldown (Final)

Plan: `docs/features/0015_PLAN.md`  
Rubric: `commands/code_review.md`

## Findings (ordered by severity)

No open findings.

## Resolved From Previous Reviews

- Cooldown activation is no longer tied only to the ticker path; the executor now notifies the orchestrator directly via `on_cooldown_entered`, so retry-queue-triggered cooldowns also get an expiry timer.
- `RetryQueue.process_due()` now re-checks pause state before each retry, so it stops once cooldown activates mid-batch.
- `StrategyRunner._execute_intents()` now also stops mid-batch when cooldown activates.
- `RetryQueue.start()` now clears `_stopped`, so stop/start cycles work again.
- Cooldown cycle numbering now persists across expiry via `_auth_cooldown_cycles`.
- Runner and orchestrator cooldown tests were added.
- Focused `ruff` is clean on the touched gridbot files.
- Retry queue clearing on cooldown entry was accepted as a deliberate design choice (not a defect). Auth-error retries are not useful work, stale prices are worse than regeneration, and post-cooldown recovery is handled by the ticker path (engine regenerates placements) and order-sync loop (reconciler syncs via REST). Plan updated to document this decision.

## Plan Implementation Coverage

Implemented and verified:

- `GridbotConfig` has `auth_cooldown_minutes`.
- `IntentExecutor` classifies auth errors, counts consecutive failures, exposes cooldown state, and notifies the orchestrator when cooldown activates via `on_cooldown_entered` callback.
- `StrategyRunner` skips order execution while cooldown is active and stops mid-batch if cooldown flips on during a ticker cycle.
- `RetryQueue` pauses itself based on executor state, stops mid-batch when cooldown flips on, and is cleared on cooldown entry.
- `Orchestrator` starts cooldown timers, tracks cumulative cycle counts, clears retry queues, alerts on cooldown entry and expiry, and resets executors when cooldown expires.
- Tests cover executor auth parsing/callbacks, retry queue pause/restart/mid-batch behavior, runner cooldown skip and mid-batch stop, and orchestrator cooldown lifecycle (entry, cycle counting, expiry, queue clearing, config minutes).

## Test/Lint Evidence

- `uv run pytest -q apps/gridbot/tests` -> `235 passed`
- `uv run ruff check apps/gridbot/src/gridbot/config.py apps/gridbot/src/gridbot/executor.py apps/gridbot/src/gridbot/orchestrator.py apps/gridbot/src/gridbot/retry_queue.py apps/gridbot/src/gridbot/runner.py apps/gridbot/tests/test_config.py apps/gridbot/tests/test_executor.py apps/gridbot/tests/test_retry_queue.py apps/gridbot/tests/test_runner.py apps/gridbot/tests/test_orchestrator.py` -> `All checks passed!`
