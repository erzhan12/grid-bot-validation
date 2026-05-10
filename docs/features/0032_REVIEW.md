# Feature 0032 — Code Review (final pass)

**Plan:** `docs/features/0032_PLAN.md`
**Branch:** `feature/0032-orderlinkid-retry-idempotency` (no commits yet, all changes unstaged).

## Verdict

**Approved. No blocking findings, no remaining stylistic suggestions.**

The first review pass surfaced three minor items (F1 helper consistency, F2 fallback comment, F3 cancelled-reemission test). All three are addressed in the current tree. This pass re-verifies plan compliance and the applied fixes.

## Plan compliance

| Plan item | Status | Location |
| --- | --- | --- |
| `PlaceLimitIntent.order_link_id: str \| None = field(default=None, compare=False)`, frozen unchanged | ✅ | `packages/gridcore/src/gridcore/intents.py:64` |
| `_IDENTITY_PARAMS` unchanged; `client_order_id` derivation unaffected | ✅ | `intents.py:69` |
| New module `gridbot.order_link_id.make_order_link_id(client_order_id, *, now_ms=...)` with pluggable time source | ✅ | `apps/gridbot/src/gridbot/order_link_id.py` |
| `OrderResult.order_link_id` added | ✅ | `apps/gridbot/src/gridbot/executor.py:33` |
| `IntentExecutor.execute_place`: preset wins; fallback via helper; shadow path also surfaces wire id | ✅ | `executor.py:157-218` |
| `_assign_wire_link_id(intent, *, existing_order_link_id=None)` static, frozen-friendly via `replace()` | ✅ | `runner.py:838-848` |
| Reuse-on-reattempt lookup for `status == "failed"` + non-`None` `intent.order_link_id` | ✅ | `runner.py:875-881` |
| Assigned intent threaded into TrackedOrder, executor call, and `_on_intent_failed` | ✅ | `runner.py:888-905` |
| `inject_open_orders` upgrade path: `existing.intent is None` defensive bail; `replace()` preserves placement context; documented fallback for missing exchange link id; `existing.intent = upgraded_intent` + `existing.mark_placed(order_id)`; callback invocation; INFO log with `retry_cancelled=<count>` | ✅ | `runner.py:1001-1029` |
| `RetryQueue.cancel_for_prefix(prefix)`: primary `client_order_id == prefix`, fallback `extract_client_order_prefix(order_link_id) == prefix`, ignores `CancelIntent`, idempotent | ✅ | `retry_queue.py:152-167` |
| `RetryQueue.add()` defensive WARN only for `PlaceLimitIntent` with `order_link_id is None` | ✅ | `retry_queue.py:117-123` |
| `on_retry_cancel_for_prefix: Callable[[str], int] \| None = None` with default `None` (no-op) | ✅ | `runner.py:147,169` |
| Orchestrator `_init_strategy` wires `lambda prefix: retry_queue.cancel_for_prefix(prefix)` | ✅ | `orchestrator.py:676` |
| Reconciler comments updated to point at feature 0032 | ✅ | `reconciler.py:75-78,104-108` |
| RULES.md item #7 extended with retry-idempotency invariant + reconcile-upgrade sub-bullets, files list updated | ✅ | `RULES.md:911-918` |

## Tests

| Scenario | File / Test |
| --- | --- |
| Frozen + `replace` immutability of original intent | `test_intents.py::test_place_intent_order_link_id_defaults_to_none_and_is_frozen`, `..._replace_preserves_original` |
| `compare=False` ⇒ equality and hash unaffected by wire id | `test_intents.py::test_place_intent_order_link_id_does_not_affect_equality_or_hash` |
| `_IDENTITY_PARAMS` exclusion | `test_intents.py::test_place_intent_order_link_id_not_in_identity_params` |
| Helper shape + round-trip via `extract_client_order_prefix` | `test_order_link_id.py` (both tests) |
| Executor uses preset verbatim | `test_executor.py::test_execute_place_reuses_preset_order_link_id` |
| Executor fallback surfaces wire id in success and failure `OrderResult` | `test_executor.py::test_place_order_failure`, existing `test_place_order_*` updated |
| Shadow path surfaces wire id (preset + fallback) | `test_executor.py::test_place_order_shadow_mode`, `..._reuses_preset_link_id` |
| Runner returns `assigned` (not original) intent to executor / TrackedOrder / callback | `test_runner.py::test_execute_place_intent`, `..._failure`, `TestStrategyRunnerFailureCallback` |
| **Engine re-emission reuse after `failed`** — counting fake, helper called exactly once across two attempts | `test_runner.py::test_failed_reemission_reuses_previous_wire_id` |
| **Engine re-emission after `cancelled` mints a fresh wire id** (F3) — counting fake, helper called twice with different ids | `test_runner.py::test_cancelled_reemission_mints_fresh_wire_id` |
| RetryQueue process_due retries with same wire id | `test_runner.py::test_retry_queue_receives_and_retries_assigned_wire_id` |
| Reconcile-upgrade + retry cancellation E2E inside runner | `test_runner.py::test_inject_open_orders_upgrades_failed_order_and_cancels_retry` |
| Defensive `existing.intent is None` upgrade-skip | `test_runner.py::test_inject_open_orders_upgrade_skips_when_existing_intent_missing` |
| `cancel_for_prefix` primary + fallback match, idempotent, ignores `CancelIntent` | `test_retry_queue.py::test_cancel_for_prefix_*` |
| `RetryQueue.add` WARN policy (PlaceLimitIntent only) | `test_retry_queue.py::test_add_warns_for_place_without_assigned_link_id`, `..._does_not_warn_for_cancel_intent` |
| Orchestrator wiring — full Orchestrator construction, real `_init_strategy`, end-to-end retry cancellation observed | `test_orchestrator.py::test_reconcile_upgrade_cancels_retry_queue_via_wiring` |
| Integration: REST kwargs include `order_link_id`, shadow path surfaces it | `tests/integration/test_engine_to_executor.py` |
| Replay seed loader still keys on prefix-only after wire-format input | `apps/replay/tests/test_engine_seed.py` |

Three test designs deserve specific call-out:
- **Counting fake `make_order_link_id`** in both re-emission tests (`test_failed_reemission_reuses_previous_wire_id`, `test_cancelled_reemission_mints_fresh_wire_id`) cleanly distinguishes "reuse" vs "fresh mint" by counting calls and comparing recorded ids.
- **`test_inject_open_orders_upgrades_failed_order_and_cancels_retry`** verifies tracked-state transition AND retry queue size AND that a subsequent `process_due()` does NOT call the executor — three observable consequences in one test.
- **`test_reconcile_upgrade_cancels_retry_queue_via_wiring`** exercises real Orchestrator construction (not a mocked runner), catching the "individually correct but wired wrong" failure mode flagged in the plan.

## Verification of applied fixes

### F1 — `inject_open_orders` upgrade now uses `mark_placed()` helper

`runner.py:1018-1019`:

```python
existing.intent = upgraded_intent
existing.mark_placed(order_id)
```

Replaces the prior three direct field writes. Consistent with every other state-transition site in the runner. ✅

### F2 — fallback `order_link_id or existing.intent.order_link_id` is now documented

`runner.py:1009-1011`:

```python
# Bybit should echo our wire id; fallback keeps the prior
# assigned id if an older/direct exchange payload omits it.
exchange_link_id = order_link_id or existing.intent.order_link_id
```

Defensive intent is now explicit; no behavior change. ✅

### F3 — re-emission after `cancelled` mints a fresh wire id

`test_runner.py:597-631` (`test_cancelled_reemission_mints_fresh_wire_id`):

- Sets up a successful first placement, then forces `tracked.mark_cancelled()`.
- Re-emits a fresh frozen `PlaceLimitIntent` (via `replace(intent)`) with the same `client_order_id`.
- Asserts `minted == [first_assigned.order_link_id, second_assigned.order_link_id]` — the helper was invoked twice (once per placement, no reuse).
- Asserts `second_assigned.order_link_id != first_assigned.order_link_id` — the second id is genuinely fresh.
- Asserts the original re-emission intent remains `order_link_id is None` — frozen-replace semantics preserved.

This pins the intended contrast with `test_failed_reemission_reuses_previous_wire_id`: reuse only fires for `status == "failed"`. ✅

## Subtle correctness checks (re-verified)

- **`compare=False` and `__hash__`**: `@dataclass(frozen=True)` auto-generates `__hash__` from comparison fields only. `compare=False` excludes `order_link_id` from both `__eq__` and `__hash__`. The equality+hash test verifies this; `RetryQueue.remove()` and any set-based tracking remain correct after wire-id assignment.
- **`PlaceLimitIntent.create()` factory**: builds `client_order_id` from `_IDENTITY_PARAMS` only; new field `order_link_id` defaults to `None`.
- **Production callers of `IntentExecutor.execute_place`**: `StrategyRunner._execute_place_intent` (assigns wire id) and `RetryQueue` via `_dispatch_intent` (already-assigned intent in queued item). `IntentExecutor.execute_batch` exists but is only called from tests. Defensive `RetryQueue.add` WARN guards future regressions.
- **State-machine coverage**: re-emission paths after `failed` (reuse) and `cancelled` (fresh mint) are both tested. `placed` returns early via the existing duplicate-skip block. `pending` and `filled` fall through to fresh assignment — pre-existing behavior, unchanged by this feature.

## Summary

Implementation matches the v5 plan. All three minor items from the first review pass have been applied. Ready to commit and open a PR when authorized.
