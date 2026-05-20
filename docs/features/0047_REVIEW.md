# 0047 Code Review: Bootstrap Grid Snapshot

## Findings

### P1 - Bootstrap insert failures are not operator-visible

Files:
- `apps/gridbot/src/gridbot/orchestrator.py:1377`
- `apps/gridbot/src/gridbot/writers/grid_state_writer.py:171`
- `apps/gridbot/src/gridbot/writers/grid_state_writer.py:260`

`_bootstrap_grid_snapshots()` treats `writer.flush(timeout=5.0) == True` as the durable-success signal after enqueueing bootstrap snapshots. But `flush()` only waits for `queue.join()`, and `_insert_one()` catches DB/repository exceptions internally, increments `_total_errors`, logs, and still reaches `task_done()`. That means a bootstrap insert can fail, `flush()` can return `True`, startup can continue without a row for the new `run_id`, and no `notifier.alert(...)` or `_total_bootstrap_failures` increment occurs.

This violates the feature goal of "best-effort with operator-visible degradation signal" for transient DB write failures during bootstrap. Probe failures and flush timeouts alert correctly; async insert failures do not. A focused regression test should monkeypatch `GridStateSnapshotRepository.insert` or `writer._insert_one` to fail during the bootstrap enqueue path and assert that startup returns normally, no row exists, and the bootstrap degradation alert/counter fires. Implementation options include tracking `total_errors` before/after the bootstrap flush or having bootstrap writes use a writer API that reports per-item failure status.

## Notes

- The repository ordering contract is implemented as planned: `get_latest()` uses `ORDER BY exchange_ts DESC, id DESC`, matching `get_at_or_before()`.
- The bootstrap branch logic matches the plan: empty scope writes at `Run.start_ts`, matching persisted state primes dedupe, stale `<= Run.start_ts` writes a superseding row at exactly `Run.start_ts`, and future-stale rows alert without writing.
- `Run.start_ts` tracking is populated after `session.flush()`, so the bootstrap path has the intended anchor without re-querying `runs`.
- The added tests cover the main happy paths, stale correction branches, anomalous future-stale alerting, probe failure alerting, flush timeout alerting, unbuilt-grid skip, fingerprint priming, and repository tie-break ordering.

## Verification

Ran:

```bash
uv run pytest -q apps/gridbot/tests/test_grid_state_writer.py apps/gridbot/tests/test_orchestrator.py::TestOrchestratorBootstrapGridSnapshots shared/db/tests/test_repositories.py::TestGridStateSnapshotRepository
uv run ruff check apps/gridbot/src/gridbot/orchestrator.py apps/gridbot/src/gridbot/writers/grid_state_writer.py apps/gridbot/tests/test_grid_state_writer.py apps/gridbot/tests/test_orchestrator.py shared/db/src/grid_db/repositories.py shared/db/tests/test_repositories.py
```

Results:
- `23 passed`
- `All checks passed!`
