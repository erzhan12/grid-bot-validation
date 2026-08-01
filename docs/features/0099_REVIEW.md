# 0099 — Block retry placements during SAME ORDER latch (review trail)

Reproduces PR #239 (cursor bot) on branch `feature/0099-retry-same-order-latch`,
plus fixes surfaced by external review that #239 does not carry.

## Change

1. `retry_dispatch_place` returns `OrderResult(error="same_order_blocked")` when
   the feature-0031 SAME ORDER soft-block (`_same_order_error`) is latched, so
   queued place-retries cannot bypass the tick-path placement suppression.
2. `retry_queue.process_due` treats `"same_order_blocked"` as a terminal drop
   sentinel (alongside `safety_cap*` / `truncate_breaker_blocked` /
   `duplicate_order_blocked`) — a latched place-retry is dropped, not
   re-backed-off. Without this the runner logged "dropping retry" while the
   queue actually retained the item and burned `max_attempts`.
3. `_execute_cancel_intent` drains queued place-retries via
   `_on_retry_cancel_for_prefix` on a successful `reason=="duplicate"` cancel,
   so a feature-0087 duplicate-healing cancel cannot leave a stale retry that
   resurrects the same-price shadow.

## External review trail

- **Engines:** codex (`gpt-5.6-sol`) + cursor `agent`, read-only.
- **Rounds:** 2.

**Iteration 1 — raised, both engines convergent:**
- P2 `same_order_blocked` absent from `process_due` terminal-drop list →
  re-backoff burns attempts; log "dropping" is false. **ACCEPTED, fixed** (#2
  above) + `test_process_due_drops_same_order_blocked_failure`.
- P1 (codex) retried `CancelIntent`s dispatch via orchestrator `_dispatch_intent`
  → `executor.execute_cancel`, bypassing `_execute_cancel_intent`, so a
  retry-succeeded duplicate cancel does not itself drain. **ACCEPTED as known
  out-of-scope gap** — pre-existing architectural limitation shared by PR #239,
  now largely mitigated because `same_order_blocked` place-retries are
  terminal-dropped while latched. Not fixed here (would require re-routing all
  cancel retries through the runner).
- P3 negative drain coverage + `is not None` style drift — see below.

**Iteration 2 — both engines: NO P1/P2 FINDINGS.** Only P3s.
- P3 failed-`duplicate` cancel must not drain → **fixed**,
  `test_failed_duplicate_cancel_does_not_drain_queued_retry`.
- P3 `on_retry_cancel_for_prefix` docstring said reconcile-only → **fixed**,
  docstring updated.
- P3 `is not None` vs truthiness at the reconcile-upgrade site → **rejected**,
  harmless; `is not None` is correct for a callable.

## Tests added

- `test_retry_dispatch_place_blocks_when_same_order_latched`
- `test_duplicate_healing_cancel_drains_queued_retry`
- `test_non_duplicate_cancel_does_not_drain_queued_retry`
- `test_failed_duplicate_cancel_does_not_drain_queued_retry`
- `test_process_due_drops_same_order_blocked_failure`

## Verification

- `uv run pytest apps/gridbot/tests -q` — **841 passed**.
- `uv run ruff check apps/gridbot/src apps/gridbot/tests` — clean.

## Result

SUCCESS — zero valid P1/P2 outstanding; tests + lint green.
