# 0047 Bootstrap Code Review

Scope: changes on `fix/0047-bootstrap-snapshot` against `main`, implementing
`docs/features/0047_BOOTSTRAP_PLAN.md` (issue #108 — GridStateWriter
initial-snapshot on startup).

## Findings

**No blocking findings.** Implementation matches the plan; tests cover all
documented branches; lint is clean.

The previously reported insert-failure race is fixed:
`_bootstrap_grid_snapshots()` captures `errors_before` **before** any per-runner
`writer.write()` call (orchestrator.py:1307), so worker failures that occur
mid-enqueue land inside the post-`flush()` delta and trigger the
`bootstrap_insert` alert path. The dedicated regression test
`test_bootstrap_insert_failure_detected_when_worker_fails_during_enqueue`
locks the contract.

## Plan ↔ Implementation Matrix

| Plan item | Where | Status |
|---|---|---|
| `GridStateSnapshotRepository.get_latest()` ordered by `exchange_ts DESC, id DESC` | `shared/db/src/grid_db/repositories.py:1251` | ✅ |
| `GridStateWriter.flush(...) -> bool` (was `None`) | `apps/gridbot/src/gridbot/writers/grid_state_writer.py:171` | ✅ |
| `GridStateWriter.get_last_fingerprint(...)` returns `(fp_tuple, exchange_ts)` or `None`; errors propagate | `grid_state_writer.py:198` | ✅ |
| `GridStateWriter.prime_fingerprint(scope, fp_tuple)` seeds dedupe cache | `grid_state_writer.py:216` | ✅ |
| `_total_bootstrap_failures` counter + `get_stats()["total_bootstrap_failures"]` | `grid_state_writer.py:96, 288` | ✅ |
| `Orchestrator._run_start_ts: dict[str, datetime]` populated in `_create_run_records()` post-`session.flush()` | `orchestrator.py:130, 1291` | ✅ |
| `_bootstrap_grid_snapshots()` four-branch logic (empty / match / stale ≤ run_start / stale > run_start) | `orchestrator.py:1301` | ✅ |
| Stamps writes with `exchange_ts = Run.start_ts` (not `datetime.now(UTC)`) | `orchestrator.py:1335, 1345` | ✅ |
| Per-runner try/except → WARNING + `notifier.alert(error_key=f"bootstrap_{strat_id}")` + bump counter + continue | `orchestrator.py:1364-1376` | ✅ |
| Final `writer.flush(timeout=5.0)` only when `enqueued`; on `False` → `notifier.alert(error_key="bootstrap_flush")` | `orchestrator.py:1378-1387` | ✅ |
| Async insert-failure detection via `total_errors` delta around flush | `orchestrator.py:1307, 1380, 1388-1400` | ✅ |
| Call site in `start()` after writer thread start, before WS connect | `orchestrator.py:319-321` | ✅ |
| `RULES.md` bullet under "Grid State DB snapshots — feature 0047" | `RULES.md` (modified section) | ✅ |

## Test Coverage Matrix (plan spec → suites)

Repository (`shared/db/tests/test_repositories.py::TestGridStateSnapshotRepository`):
- `test_get_latest_picks_newest_row_by_exchange_ts_then_id` — locks the
  `exchange_ts DESC, id DESC` tie-break contract.

Writer (`apps/gridbot/tests/test_grid_state_writer.py`):
1. `test_get_last_fingerprint_returns_none_when_empty`
2. `test_get_last_fingerprint_returns_tuple_and_exchange_ts_from_latest_row` — verifies BOTH fields
3. `test_get_last_fingerprint_propagates_db_errors` — DB error does NOT collapse to `None`
4. `test_prime_fingerprint_blocks_identical_subsequent_write`
5. `test_flush_returns_true_on_clean_drain_false_on_timeout`

Orchestrator (`apps/gridbot/tests/test_orchestrator.py::TestOrchestratorBootstrapGridSnapshots`):
1. `test_start_bootstraps_initial_snapshot_when_db_empty` — full `start()`; asserts row, `grid_json`, and `exchange_ts == _run_start_ts`
2. `test_bootstrap_writes_fresh_row_when_persisted_grid_is_stale_before_run_start`
3. `test_bootstrap_writes_fresh_row_when_persisted_grid_is_stale_equal_to_run_start` — equality-edge tie-break via `id DESC`
4. `test_bootstrap_alerts_and_skips_when_stale_row_is_after_run_start` — anomalous future-stale path; no write, alert + counter
5. `test_bootstrap_primes_dedupe_when_persisted_grid_matches`
6. `test_bootstrap_skips_when_grid_unbuilt`
7. `test_bootstrap_alerts_and_counts_on_probe_failure`
8. `test_bootstrap_alerts_and_counts_on_flush_timeout`
9. `test_bootstrap_alerts_and_counts_on_insert_failure` *(extra over spec — sync probe-time insert failure via failing repo)*
10. `test_bootstrap_insert_failure_detected_when_worker_fails_during_enqueue` *(extra over spec — deterministic race regression for the `errors_before` capture timing)*

## Review Notes

- Probe error path (`get_last_fingerprint` raising) is correctly NOT swallowed
  to `None`; the caller's outer `try/except Exception` at the runner-loop
  boundary catches it, alerts with `error_key=f"bootstrap_{strat_id}"`, bumps
  the counter, and `continue`s — exactly the contract the plan called out as
  the "probe error mistaken for empty DB → duplicate insert" failure mode.
- Equality edge (`last_ts == run_start_ts`) is handled with `<=`, no `+1ms`
  math — the `id DESC` tie-break in `get_latest` / `get_at_or_before` makes
  same-`exchange_ts` insertion order the supersession criterion. Test 3
  (`...stale_equal_to_run_start`) asserts `get_at_or_before(at_ts=run_start_ts)`
  returns the new row, which is the operational contract.
- `_ensure_utc_aware()` (orchestrator.py:54) defensively normalizes naive
  datetimes returned by SQLite when comparing `run_start_ts` against
  `last_ts`. Not explicit in the plan but matches existing patterns and
  keeps SQLite-backed tests honest.
- Writer's defensive copy of `grid` payload at enqueue
  (`grid_state_writer.py:147`) survives callers mutating the in-memory grid
  before the worker drains; preserves the "verbatim snapshot" contract.
- Insert-failure delta uses `writer.get_stats()["total_errors"]` before and
  after `flush()`; this catches BOTH sync probe-time inserts and worker-thread
  failures during the enqueue→flush window. Both regression tests (9, 10)
  hit this branch.
- Orchestrator touches `writer._total_bootstrap_failures` directly (private
  attribute access). Counter ownership is documented in the plan as
  "writer-owned for visibility parity, orchestrator-mutates" — consistent
  with how other counters are handled in this codebase.
- `flush()`'s waiter thread is started fresh on every call but `daemon=True`
  and short-lived; not a leak concern at orchestrator-startup cadence.
- Bootstrap runs **before** WS connect (orchestrator.py:319-321 vs. 323+),
  matching the plan's "no fills in flight, no grid mutations queued"
  invariant.
- `_create_run_records()` correctly calls `session.flush()` (line 1288)
  before reading `run.start_ts` so the column default `utc_now` materialises
  in time for `_run_start_ts[strat_id] = run.start_ts`.

## Verification

```bash
uv run pytest -q \
  apps/gridbot/tests/test_grid_state_writer.py \
  apps/gridbot/tests/test_orchestrator.py::TestOrchestratorBootstrapGridSnapshots \
  shared/db/tests/test_repositories.py::TestGridStateSnapshotRepository

uv run ruff check \
  apps/gridbot/src/gridbot/orchestrator.py \
  apps/gridbot/src/gridbot/writers/grid_state_writer.py \
  apps/gridbot/tests/test_grid_state_writer.py \
  apps/gridbot/tests/test_orchestrator.py \
  shared/db/src/grid_db/repositories.py \
  shared/db/tests/test_repositories.py
```

Results:
- `25 passed in 0.37s`
- `All checks passed!`
