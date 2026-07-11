# Feature 0086 — Code Review (fail-closed startup reconciliation, issue #206)

Reviewed diff: `origin/main@76aaf79` → working tree, branch
`feature/0086-fail-closed-startup-reconcile`.
Files: `orchestrator.py` (+75/-7), `test_orchestrator.py` (+132),
`test_issue_206_startup_reconcile_race.py` (new), `0086_PLAN.md` (new).

## 1. Plan conformance — PASS

Checked every plan item against the diff:

| Plan item | Code | Verdict |
|---|---|---|
| `_STARTUP_RECONCILE_BACKOFFS = (2.0, 5.0, 10.0)` by other timing constants | `orchestrator.py:60` | match |
| `StartupReconciliationError` module-level, `StartupTimeoutError` precedent | `orchestrator.py:66-71` | match |
| `start()` loop delegates to `_reconcile_startup_with_retry` | `orchestrator.py:342-348` | match |
| 1 + 3 retries, WARNING per failed attempt, recovery WARNING | `orchestrator.py:425-447` | match |
| Unchanged "Reconciliation complete" INFO wording (log parsers) | `orchestrator.py:430-434` | match |
| Exhaustion → alert (`startup_reconcile_<strat>`) → raise; main.py untouched | `orchestrator.py:449-459`, `main.py:157-159` | match |
| `_order_sync_once`: alert on `result.errors` + `alert_exception` on raise, shared `order_sync_<strat>` key | `orchestrator.py:1797-1801,1816-1819` | match |
| `reconciler.py` unchanged | — | match |
| Test list (5 orchestrator tests + repro-file inversion) | `test_orchestrator.py:583-712`, repro file | match¹ |

¹ One gap found during this review and fixed: the exhausted test now also
asserts `place_order` was never called ("no order placement was attempted"
was claimed by the plan but not asserted).

## 2. Bugs — none found

- `result` after the loop is always bound (`attempts >= 1` guaranteed).
- Loop indexing `_STARTUP_RECONCILE_BACKOFFS[attempt - 1]` is safe: only
  reached when `attempt < attempts = 1 + len(backoffs)`.
- Raise ordering (alert before raise) means a Telegram send failure inside
  `alert()` cannot suppress the abort — `Notifier.alert` swallows send
  errors internally (`notifier.py:79-86`).
- Abort point precedes run records, grid-state writer start, and WS
  connect; `stop()` no-op via `_started` guard is correct (verified in
  round-1/2 external reviews).

## 3. Data alignment — n/a

No new external payload parsing; retry loop consumes the existing
`ReconciliationResult` dataclass only.

## 4. Over-engineering — none

~50 code lines. No new config surface, no per-strategy safe-mode machinery,
no speculative state. Matches the "minimum code" bar.

## 5. Style — consistent

- Lazy `%` logging for new WARNING/ERROR lines, f-string preserved on the
  relocated INFO line (matches surrounding file conventions).
- `_UPPER_SNAKE` module constant, `_leading_underscore` method, Google-style
  docstrings — all consistent.
- `alert_exception` context format `"order_sync <strat>"` mirrors the
  existing `"ws_reset <account>/<kind>"` pattern.

## 6. Tests — PASS

- `TestStartupReconcileRetry` (4 tests): transient recovery (sleep 2.0 once,
  no alert), first-try success (no sleep), exhaustion (raise + single alert
  with correct error_key + no run records / WS connect / order placement),
  periodic-sync `result.errors` alert, periodic-sync exception alert.
- Repro file: orchestrator-level fail-closed test + runner-level
  duplicate-collapse documentation test (real engine, mocked executor).
- All hermetic (REST/WS classes patched), `time.sleep` patched in retry
  tests, suite runs in ~2.6s. Full gridbot suite: 821 passed, ruff clean.

## Notes (pre-existing, out of scope)

- If an account has no reconciler in `self._reconcilers`, startup
  reconciliation is silently skipped (`orchestrator.py:346-348` guard) —
  pre-existing behavior; unreachable in practice because `_init_account`
  always registers one per account.
- Engine same-price duplicate collapse (`engine.py:359`) remains open —
  fix 2 of the issue #206 analysis, tracked separately.

## External review trail

- codex plan review (3 rounds): 8 findings → 5 accepted (doc corrections,
  sync exception alert, test hardening), 2 rejected with evidence, round 3
  clean.
- cursor agent plan review (2 passes): 1 valid mismatch (plan test-class
  name), fixed; verification table otherwise all-match.
- 5-category subagent code review (1 iteration): 0 critical; start()
  docstring `Raises:` added; warnings/info triaged in session notes.
- codex code review per code_review.md (1 round): NO P1/P2 FINDINGS.
  P3: "recovers on final attempt" test not added (accepted gap); stale
  "fail-open" repro-file header — fixed.
- cursor agent code review per code_review.md (1 round): NO P1/P2
  FINDINGS; 17-point verification log all-PASS. 6 P3s noted and accepted
  (caplog recovery-warning check, final-attempt recovery case,
  multi-runner sweep-continues assert, exception base-class precedent
  Exception vs RuntimeError, repro alert assert tightness, pre-existing
  orchestrator.py size).
