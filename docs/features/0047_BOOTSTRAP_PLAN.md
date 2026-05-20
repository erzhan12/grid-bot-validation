# 0047 Bootstrap: GridStateWriter Initial-Snapshot on Startup

## Context

After bot restart, `GridStateWriter` only writes to `grid_state_snapshots` when
`runner._on_grid_change` fires — i.e., when a fill or grid recentering occurs.
In quiet markets the table stays at **0 rows for the new `run_id`** indefinitely,
even though the runner holds a fully-loaded valid grid in memory.

This breaks the Feature 0047 contract: replay
`load_grid_state_from_snapshots(at_or_before=seed.at_ts ...)` for a seed inside
such a quiet-run window returns `None` and falls back to the legacy JSON / fresh-build
path — defeating the v18 DB-backed grid-state design.

Source of truth: GitHub issue #108. Verified live on mainnet_live 2026-05-20:
bot restart at 12:13:12 → writer alive at 12:13:14 → grid loaded (21 levels) →
8 minutes later, `SELECT COUNT(*) FROM grid_state_snapshots` = 0.

## Goal

**Best-effort with operator-visible degradation signal.**

On the happy path, `Orchestrator.start()` returns with **≥1 row** in
`grid_state_snapshots` for every active strategy with a built grid
(`len(grid) > 1`), where each row reflects the in-memory grid as of bootstrap.
Subsequent identical `_on_grid_change` events must not produce a redundant row
(dedupe contract preserved). Strategies with an unbuilt grid (no restore, no
first ticker) are skipped — replay correctly falls back to a fresh build for
those.

**Failure semantics**: when a per-runner probe/write raises, OR the writer's
final `flush()` reports timeout, the bot **continues to start and trade** — a
transient DB issue must not knock a live trading bot offline for a replay-only
contract. Each such failure raises an operator alert:

- WARNING log with `exc_info=True` and `strat_id` / `run_id` context.
- `notifier.alert(...)` so on-call sees the degradation in the chat channel.
- `_total_bootstrap_failures` stat counter incremented (visible via
  `writer.get_stats()`).

The bot keeps trading; live `_on_grid_change` events will populate the table
once the market moves. The replay-seed window between restart and the first
live grid mutation may degrade to JSON / fresh-build fallback — operator can
size the impact from the alert + counter.

## Algorithm

Bootstrap runs **once** in `Orchestrator.start()`, immediately after
`self._grid_state_writer.start()`. At this point:

- `self._run_ids` is populated (`_create_run_records` ran on line 287).
- `self._runners` is populated (constructed in `_init_strategy`).
- WS streams are **not yet connected** (`_connect_websockets` called next on line 312).
- No fills are in flight, no grid mutations are queued.

Per active runner (where `self._grid_state_writer is not None` and `strat_id` ∈ `_run_ids`):

1. Resolve `account_id = self._account_id_for(account_name)` (where
   `account_name = self._get_account_for_strategy(strat_id)`).
2. Read current grid: `grid = runner.engine.grid.grid` (the `list[dict]` of
   `{side, price}` entries owned by `gridcore.Grid.grid`).
3. If `len(grid) <= 1`, skip (matches existing `_on_grid_change` guard for
   unbuilt / single-WAIT grids).
4. Compute the **current** fingerprint:
   `current_fp = grid_fingerprint(grid, grid_step, grid_count)` (same helper the
   writer's dedupe gate uses).
5. Probe `last = writer.get_last_fingerprint(run_id, account_id, strat_id)`,
   which returns `Optional[tuple[tuple, datetime]]` — the fingerprint of the
   latest row AND its `exchange_ts`. Let `last_fp, last_ts = last` when not
   `None`.
   - **`last is None`** (DB empty for this scope) → call
     `writer.write(strat_id, grid, grid_step, grid_count, account_id, symbol,
     exchange_ts=run_start_ts)` where `run_start_ts = self._run_start_ts[strat_id]`
     is the just-created `Run.start_ts` (set in `_create_run_records`). See
     **Timestamp anchor** below for why this beats `datetime.now(UTC)`.
   - **`last_fp == current_fp`** (persisted state matches in-memory) → call
     `writer.prime_fingerprint(scope, current_fp)` so the next live
     `_on_grid_change` correctly dedupes against persisted state.
   - **`last_fp != current_fp`** (persisted state is stale) — branch further on
     the relative position of `last_ts` vs `run_start_ts`:
     - **`last_ts <= run_start_ts`** (realistic stale case: a prior write
       landed before — or exactly at — this run's start, e.g. a residual row
       from an earlier lifecycle that somehow survived) → call
       `writer.write(..., exchange_ts=run_start_ts)`. The new row anchors
       **literally** at the run start. Because
       `GridStateSnapshotRepository.get_latest` / `get_at_or_before` order by
       `exchange_ts DESC, id DESC`, a same-`exchange_ts` row with a higher
       autoincrement `id` strictly supersedes the stale row even in the
       equality edge (`last_ts == run_start_ts`). No `+1ms` math required —
       it would just push the anchor past the run-start moment for the
       equality case, breaking `seed.at_ts == run_start_ts` queries. Safety:
       the partial unique index includes `raw_fingerprint`, and stale/new
       rows have different fingerprints (the very reason this branch fires),
       so same `exchange_ts` cannot conflict. `enqueued = True`. No separate
       `prime_fingerprint` call — `write()` already updates
       `_last_fingerprint[scope]` itself before enqueue.
     - **`last_ts > run_start_ts`** (anomalous case: a stale row sits in the
       "future" of this run's start — should not occur because
       `_create_run_records()` mints a fresh `run_id` per `start()` call, so
       no prior rows exist for the fresh run_id). A single bootstrap write
       cannot cover `[run_start_ts, last_ts]` honestly: either we stamp at
       `run_start_ts` (loses to the stale row under DESC order) or at
       `last_ts + 1ms` (leaves the historical window in `[run_start_ts, last_ts]`
       returning the stale row or nothing). **Treat as alert-only**: WARNING
       log with `exc_info=False` and "anomalous future stale row" context,
       `notifier.alert(f"Bootstrap grid snapshot anomaly for {strat_id}: stale row exchange_ts {last_exchange_ts} is after run start {run_start_ts}; skipping correction (investigate run_id reuse)", error_key=f"bootstrap_anomalous_{strat_id}")`, bump
       `writer._total_bootstrap_failures`, and **skip the write**. Operator
       must investigate the anomaly (likely root cause: deterministic run_id
       collision or external writer). `enqueued` is **not** flipped for this
       branch.
6. Any per-runner internal failure (probe raises, `runner.engine` access fails,
   `_account_id_for` raises) is caught at the runner-loop boundary. The handler:
   - Logs WARNING with `exc_info=True` and the failing `strat_id` / `run_id`.
   - Calls `self._notifier.alert(f"Bootstrap grid snapshot failed for {strat_id} (run {run_id}): {exc}", error_key=f"bootstrap_{strat_id}")`. Readable message follows the existing project style (`Notifier.alert(message: str, error_key: Optional[str])` in `notifier.py:57`); the `error_key` collapses floods to one alert per strategy.
   - Bumps `writer._total_bootstrap_failures`.
   - `continue`s to the next runner. Startup is **not** aborted — see Goal.
7. **After all runners are enqueued**, call
   `success = self._grid_state_writer.flush(timeout=5.0)`. The new `bool`
   return is required because `queue_size==0` does not prove durability — an
   item dequeued by the worker and stuck in `_insert_one` shows `qsize()==0`
   while `task_done()` is still pending. `flush()` already waits on
   `queue.join()` internally; the new return value just surfaces whether the
   join completed within `timeout`. On `success is False`:
   - WARNING already emitted inside `flush()` (existing behavior).
   - `self._notifier.alert(f"Bootstrap grid snapshot flush timed out after 5.0s ({writer.get_stats()['queue_size']} items still queued)", error_key="bootstrap_flush")`.
   - Bump `writer._total_bootstrap_failures`.
   - Return normally (startup continues — best-effort).

**Timestamp anchor — use `Run.start_ts`, not `datetime.now(UTC)`**:

`Run.start_ts` is set inside `_create_run_records()` (via SQLAlchemy
`default=utc_now` on the column) which runs **before** WS connects. It is the
**design-intended lower-bound anchor** for this run: by construction every
live `exchange_ts` written later is from a WS event that arrived after the
run was created. The "intended" qualifier matters — `Run.start_ts` is
wall-clock-derived while live `exchange_ts` is the Bybit exchange clock, so
there is a residual sub-second exchange-vs-wall-clock skew at run start. In
practice this is irrelevant — replay seeds target events that exist (real
`exchange_ts`), not the millisecond of startup — but future implementers
should not build invariants on an absolute ordering guarantee that does not
exist.

Stamping bootstrap rows with `Run.start_ts` (empty-scope branch) means the
loader's `at_or_before(seed.at_ts)` returns the bootstrap row for any seed
in `[Run.start_ts, first_live_event_exchange_ts)` and the live row
afterward. `datetime.now(UTC)` would be called even later (after
`writer.start()`), opening a wider `[run_start, bootstrap_now]` miss window.

The stale-scope branch is narrower than it first appears: it only covers
`last_ts <= run_start_ts`, where a single write at `exchange_ts = run_start_ts`
both anchors literally at the run start AND supersedes the stale row via the
`id DESC` tie-break in the repository's ordering. The "anomalous future stale
row" sub-case (`last_ts > run_start_ts`) is alert-only — a single bootstrap
write cannot honestly cover both the historical window `[run_start_ts, last_ts]`
(where replay would still see the stale row or nothing) and the supersede goal
past `last_ts`. Dual-write with dedupe bypass was considered and rejected as
over-engineering for a scenario a fresh `run_id` should never produce in
production.

`grid_fingerprint(grid, grid_step, grid_count)` from `gridcore.persistence` is the
canonical tuple used for dedupe — `get_last_fingerprint` MUST rebuild via this same
helper to stay byte-compatible with the live dedupe gate.

## Files to change

### `shared/db/src/grid_db/repositories.py`
Add one method to `GridStateSnapshotRepository` (DRY with existing
`get_at_or_before` ordering, no `at_ts` predicate):

- **`get_latest(run_id, account_id, strat_id) -> Optional[GridStateSnapshot]`**
  - SQLAlchemy query: `filter(run_id=?, account_id=?, strat_id=?)` →
    `order_by(exchange_ts.desc(), id.desc())` → `first()`.
  - One-line docstring referencing `get_at_or_before` as the tie-break
    canonical pattern.

### `shared/db/tests/test_repositories.py`
Add one test locking the ordering contract. Group under a new
`TestGridStateSnapshotRepository` class for consistency with the other
per-repository test classes already in this file (this is the first
`GridStateSnapshotRepository` test — no existing class to extend):

- **`test_get_latest_picks_newest_row_by_exchange_ts_then_id`** — insert three rows with (`exchange_ts`, `id`) ordering such that `id DESC` resolves a tie; assert `get_latest(run_id, account_id, strat_id)` returns the expected row.

### `apps/gridbot/src/gridbot/writers/grid_state_writer.py`
Two changes:

**A. Signature change to existing method**: `flush(timeout: float = 10.0) -> bool`
(was: returns `None`). Returns `True` when `queue.join()` completes within
`timeout`, `False` on timeout. The existing WARNING log on timeout stays.
Rationale: `qsize()==0` is not a durability proof (item can be in-flight
inside `_insert_one()` with `task_done` pending); callers need the explicit
bool. This is a low-risk signature change — current callers ignore the return
value. The orchestrator's `stop()` path (the only other caller) keeps
ignoring the return for now; an optional follow-up could log on timeout
there too, but is out of scope for this fix.

**B. New methods**:

- **`get_last_fingerprint(run_id, account_id, strat_id) -> Optional[tuple[tuple, datetime]]`**
  - Implementation: open `self._db.get_session()`, call
    `GridStateSnapshotRepository(session).get_latest(run_id, account_id, strat_id)`.
  - On row found: return `(grid_fingerprint(row.grid_json, float(row.grid_step), row.grid_count), row.exchange_ts)`. Both the fingerprint AND the row's `exchange_ts` are required by the bootstrap caller to compute a superseding timestamp on the stale branch.
  - On no row: return `None` ("DB empty for this scope").
  - On DB / SQL exception: **let it propagate** — do NOT swallow. The caller
    (`_bootstrap_grid_snapshots`) catches it per-runner, logs WARNING, alerts,
    bumps stat, skips that runner. This avoids the "probe error mistaken for
    empty DB → duplicate insert" failure mode.

- **`prime_fingerprint(scope: tuple[str, str, str], fp_tuple: tuple) -> None`**
  - Acquire `self._dedupe_lock`, set `self._last_fingerprint[scope] = fp_tuple`, release.
  - One-line method; idempotent.

**C. New stat**: `_total_bootstrap_failures: int = 0`, surfaced through
`get_stats()` as `"total_bootstrap_failures"` (matches the existing
`_total_written` → `"total_written"`, `_total_dedup_skipped` →
`"total_dedup_skipped"` naming pattern in `get_stats()`). Incremented by
`_bootstrap_grid_snapshots()` (in `orchestrator.py`) on each per-runner
failure and on flush timeout — the counter is owned by the writer for
visibility parity with the other stats, but the writer doesn't mutate it
itself; the orchestrator touches the attribute directly (consistent with how
other counters are handled in this codebase).

Module docstring footer gets one sentence noting the new bootstrap hooks (mirrors how the file currently documents bootstrap-window drops).

### `apps/gridbot/src/gridbot/orchestrator.py`

**A. New per-strat start-ts tracking**: alongside the existing
`self._run_ids: dict[str, UUID]` (line 121), add
`self._run_start_ts: dict[str, datetime]`. Populate it inside
`_create_run_records()` at the same point `_run_ids[strat_id]` is set
(line 1278). Concretely, in the session block (after `session.flush()` so
`run.start_ts` is populated from the column default):

```
self._run_ids[strat_config.strat_id] = UUID(run.run_id)
self._run_start_ts[strat_config.strat_id] = run.start_ts  # new
```

This gives the bootstrap path a canonical lower-bound anchor without
re-querying the `runs` table.

**B. New private method `_bootstrap_grid_snapshots()`**:

- Early-return when `self._grid_state_writer is None`.
- Track `enqueued = False` for the final flush decision.
- Iterate `self._runners.items()`. For each `(strat_id, runner)`:
  - Wrap the per-runner body in `try/except Exception`. On exception:
    - `logger.warning(..., exc_info=True)` with `strat_id` / `run_id`.
    - `self._notifier.alert(f"Bootstrap grid snapshot failed for {strat_id} (run {run_id}): {exc}", error_key=f"bootstrap_{strat_id}")`.
    - `self._grid_state_writer._total_bootstrap_failures += 1`.
    - `continue` to the next runner.
  - Skip when `strat_id` not in `self._run_ids` (defensive — shouldn't happen
    post-`_create_run_records`).
  - Compute `run_id = str(self._run_ids[strat_id])` and
    `run_start_ts = self._run_start_ts[strat_id]`.
  - `account_name = self._get_account_for_strategy(strat_id)`.
  - `account_id = self._account_id_for(account_name)`.
  - Source `grid_step` / `grid_count` / `symbol` from `runner._config` (same
    path `_on_grid_change` uses on lines 425–428 / 434–438). Use the existing
    `runner.symbol` property and read `_config.grid_step` /
    `_config.grid_count` directly.
  - `grid = runner.engine.grid.grid`; skip if `len(grid) <= 1`.
  - `current_fp = grid_fingerprint(grid, grid_step, grid_count)`.
  - `last = writer.get_last_fingerprint(run_id, account_id, strat_id)`
    (exceptions here propagate to the outer try → runner alerted + skipped).
    `last` is `Optional[tuple[fingerprint_tuple, datetime]]`.
  - Branch:
    - `last is None` → `writer.write(..., exchange_ts=run_start_ts)`;
      `enqueued = True`.
    - `last is not None and last[0] == current_fp` →
      `writer.prime_fingerprint(scope, current_fp)`.
    - `last is not None and last[0] != current_fp` (stale correction):
      - If `last[1] <= run_start_ts`: `writer.write(..., exchange_ts=run_start_ts)`; `enqueued = True`. Same-`exchange_ts` tie is broken by `id DESC` in repository ordering, so the new row supersedes the stale row while anchoring literally at run start.
      - If `last[1] > run_start_ts` (anomalous future stale row):
        WARNING log, `self._notifier.alert(f"Bootstrap grid snapshot anomaly for {strat_id}: stale row exchange_ts {last[1]} is after run start {run_start_ts}; skipping correction (investigate run_id reuse)", error_key=f"bootstrap_anomalous_{strat_id}")`, `writer._total_bootstrap_failures += 1`, **no write** (do not flip `enqueued`).
- After the loop, if `enqueued`:
  - `success = self._grid_state_writer.flush(timeout=5.0)`.
  - If `success is False`:
    - `self._notifier.alert(f"Bootstrap grid snapshot flush timed out after 5.0s ({writer.get_stats()['queue_size']} items still queued)", error_key="bootstrap_flush")`.
    - `self._grid_state_writer._total_bootstrap_failures += 1`.
    - Do NOT re-raise — startup continues (best-effort).

Call site in `start()`: insert immediately after `self._grid_state_writer.start()`
(currently line 309), guarded by `if self._grid_state_writer is not None`. Exact
new line:

```
self._bootstrap_grid_snapshots()
```

The existing comment block at lines 304–308 about the bootstrap window gets a brief
extension noting the new probe and the alert-on-failure semantic.

### `apps/gridbot/tests/test_grid_state_writer.py`
Add five tests using the existing `db` and `grid` fixtures plus `_make_writer` helper:

1. **`test_get_last_fingerprint_returns_none_when_empty`** — fresh DB, probe returns `None`.
2. **`test_get_last_fingerprint_returns_tuple_and_exchange_ts_from_latest_row`** — seed two rows with different `exchange_ts`, verify the probe returns `(fingerprint, exchange_ts)` of the later one (tie-break is `id DESC` like the loader). Assert both fields, not just the fingerprint.
3. **`test_get_last_fingerprint_propagates_db_errors`** — monkeypatch session to raise; assert exception bubbles (does NOT collapse to `None`).
4. **`test_prime_fingerprint_blocks_identical_subsequent_write`** — call `prime_fingerprint` with `grid_fingerprint(grid, ...)` then call `write` with same grid; assert no row inserted, `_total_dedup_skipped` incremented.
5. **`test_flush_returns_true_on_clean_drain_false_on_timeout`** — happy path: enqueue one row, `flush(timeout=5)` returns `True`. Timeout path: monkeypatch `_insert_one` to block on an event; call `flush(timeout=0.1)`, assert returns `False` and the existing WARNING is emitted; release the event and call `flush(timeout=5)` again — returns `True`.

### `apps/gridbot/tests/test_orchestrator.py`
Add eight tests using the existing orchestrator harness (in-memory DB, mocked WS/REST). All tests assert state **synchronously after the bootstrap call returns** — no `sleep`, no polling — because `_bootstrap_grid_snapshots()` calls `flush(timeout=5.0)` before returning.

**Test-harness pattern — run_id coupling + DDL ordering + grid pre-population (critical)**:

Three constraints stack:
- `_create_run_records()` always inserts a **new** `Run` row with a fresh UUID, so pre-seeding `grid_state_snapshots` *before* calling `start()` cannot match the probe's `run_id`. Tests 2–8 must drive bootstrap explicitly, after capturing the new `run_id`.
- `_create_run_records()` requires the `users` / `bybit_accounts` / `strategies` / `runs` tables to exist — so `db.create_tables()` must run **before** `_create_run_records()`, not after.
- Fresh `_init_strategy` produces an engine with an empty grid (`runner.engine.grid.grid == []`) when no `state_store` restore is wired. Bootstrap skips `len(grid) <= 1` — so the test grid must be built before calling `_bootstrap_grid_snapshots()` (except test 6, which deliberately leaves it empty).

Sequence for tests 2–8 (test 6 skips step 4; tests 7/8 add monkeypatches per their description):

  1. `db.create_tables()` — fixture-level (existing in-memory-DB fixture handles this).
  2. Construct orchestrator with that DB.
  3. Call `_init_account` / `_init_strategy` / `_build_routing_maps`.
  4. Call `runner.engine.grid.build_grid(<seed_price>)` so `len(grid) > 1` (same pattern as `test_runner.py`). Alternative: wire a `GridStateStore` with a saved grid so `_load_grid_state()` restores it.
  5. Call `orchestrator._create_run_records()` → captures `orchestrator._run_ids[strat_id]` and `orchestrator._run_start_ts[strat_id]`.
  6. (Tests that pre-seed) Use the captured `run_id` to insert the seed row via `GridStateSnapshotRepository.insert(...)`. **All seed rows must set `raw_fingerprint=grid_fingerprint_hash(grid_json, grid_step, grid_count)`** (same pattern as existing writer tests) — without it, the partial unique index `WHERE raw_fingerprint IS NOT NULL` doesn't bind and the probe's `get_latest` may return unexpected rows. Per-test setup:
     - Test 2 (stale-before-run-start): `exchange_ts = run_start_ts - 1h`, mutated `grid_json`, recomputed `raw_fingerprint`.
     - Test 3 (stale-equal-to-run-start): `exchange_ts = run_start_ts`, mutated `grid_json`, recomputed `raw_fingerprint`.
     - Test 4 (anomalous-future-stale): `exchange_ts = run_start_ts + 1h`, mutated `grid_json`, recomputed `raw_fingerprint`.
     - Test 5 (matches): `exchange_ts = run_start_ts - 1h`, **copy `grid_json` from `runner.engine.grid.grid` post-`build_grid`** (not hand-written) so the fingerprint matches exactly; reuse `grid_step` / `grid_count` from `runner._config`; `raw_fingerprint` recomputed from the copied grid (will match the runner's fingerprint exactly).
     - Tests 6/7/8 do not pre-seed.
  7. Call `orchestrator._grid_state_writer.start()` and then `orchestrator._bootstrap_grid_snapshots()`.
  8. Assert against `grid_state_snapshots` for the captured `run_id`.

Test 1 (`test_start_bootstraps_initial_snapshot_when_db_empty`) uses full `start()` because it doesn't need a pre-seed — but the engine grid still must be populated before `_bootstrap_grid_snapshots()` runs. Inject by wiring a `GridStateStore` fixture with a saved grid before constructing the orchestrator, so `_init_strategy` → `_load_grid_state()` restores it into `engine.grid.grid`. (Calling `build_grid` after `_init_strategy` won't help for full-`start()` tests because the bootstrap inside `start()` would race the test.)

**Restore-compatibility reminder**: `GridStateStore.restore` only re-hydrates the grid when the saved `grid_step` and `grid_count` match the strategy config (verified by `is_grid_correct()` in `runner._load_grid_state()`). The test fixture must save with the same `grid_step` / `grid_count` the strategy_config uses, or `_load_grid_state()` returns `None` and the grid stays empty.

1. **`test_start_bootstraps_initial_snapshot_when_db_empty`** (full `start()`) — call `start()`, assert `SELECT COUNT(*) FROM grid_state_snapshots WHERE run_id=orchestrator._run_ids[strat_id]` = 1, assert the row's `grid_json` matches `runner.engine.grid.grid`, AND assert the row's `exchange_ts == orchestrator._run_start_ts[strat_id]` (proves we anchored to `Run.start_ts`, not wall clock). Durability is proven by the row-count assert, not by `qsize()` — the new `flush() -> bool` API does the real job under the hood.
2. **`test_bootstrap_writes_fresh_row_when_persisted_grid_is_stale_before_run_start`** (harness pattern above; strict-less-than stale case) — pre-seed a row for the captured `run_id` with `exchange_ts = run_start_ts - timedelta(hours=1)` and a `grid_json` that differs from the runner's current grid. Run `_bootstrap_grid_snapshots()`. Assert:
    - Row count = 2.
    - `GridStateSnapshotRepository.get_latest(run_id, account_id, strat_id)` returns the NEW row (not the stale one).
    - New row's `grid_json` matches `runner.engine.grid.grid`.
    - New row's `exchange_ts == run_start_ts` (literally at run start).
    - New row's `exchange_ts > seeded_stale_ts` (later by 1h, sorts above via `exchange_ts DESC`).
3. **`test_bootstrap_writes_fresh_row_when_persisted_grid_is_stale_equal_to_run_start`** (harness pattern above; equality edge case) — pre-seed a row for the captured `run_id` with `exchange_ts = run_start_ts` exactly and a `grid_json` that differs from the runner's current grid. Run `_bootstrap_grid_snapshots()`. Assert:
    - Row count = 2.
    - `get_latest(run_id, account_id, strat_id)` returns the NEW row (proves the `id DESC` tie-break supersedes the stale row at the same `exchange_ts`).
    - New row's `exchange_ts == run_start_ts` (literally at run start; NOT `+1ms`).
    - `get_at_or_before(run_id, account_id, strat_id, at_ts=run_start_ts)` returns the NEW row (proves a seed at exactly `run_start_ts` reads the correction, not the stale row).
    - New row's `id > stale_row.id`.
4. **`test_bootstrap_alerts_and_skips_when_stale_row_is_after_run_start`** (harness pattern above; anomalous future-stale case) — pre-seed a row for the captured `run_id` with `exchange_ts = run_start_ts + timedelta(hours=1)` (deliberately **after** `run_start_ts` — the anomalous case a fresh `run_id` should not produce) and a `grid_json` that differs from the runner's current grid. Provide a mock `notifier`. Run `_bootstrap_grid_snapshots()`. Assert:
    - Row count = 1 (NO new row written — narrow scope).
    - `get_latest(...)` still returns the original stale row (unchanged).
    - `notifier.alert` called once with `error_key=f"bootstrap_anomalous_{strat_id}"`.
    - `writer.get_stats()["total_bootstrap_failures"] == 1`.
    - `_bootstrap_grid_snapshots()` returned normally (no exception, startup continues).
5. **`test_bootstrap_primes_dedupe_when_persisted_grid_matches`** (harness pattern above) — pre-seed a row for the captured `run_id` whose `grid_json` matches the runner's current grid and uses the same `grid_step` / `grid_count`, run `_bootstrap_grid_snapshots()`, assert total rows = 1 (no second row written), then call `runner._on_grid_change(runner.engine.grid.grid, exchange_ts=datetime.now(UTC))` (must pass `exchange_ts != None` and grid with `len > 1`) and assert still 1 row (dedupe held via primed cache). No extra `flush()` needed — dedupe drops before enqueue, so the queue is unchanged.
6. **`test_bootstrap_skips_when_grid_unbuilt`** (harness pattern, but skip step 4 grid-build) — leave `runner.engine.grid.grid == []`, run `_bootstrap_grid_snapshots()`, assert total rows for the captured `run_id` = 0. Locks parity with `_on_grid_change`'s `len(grid) <= 1` guard.
7. **`test_bootstrap_alerts_and_counts_on_probe_failure`** (harness pattern, with a mock `notifier`) — monkeypatch `writer.get_last_fingerprint` to raise `RuntimeError("simulated DB outage")`, run `_bootstrap_grid_snapshots()`. Assert: `_bootstrap_grid_snapshots()` returned normally (no exception, startup continues); no row inserted for this strat; `notifier.alert` called once with `error_key=f"bootstrap_{strat_id}"`; `writer.get_stats()["total_bootstrap_failures"] == 1`.
8. **`test_bootstrap_alerts_and_counts_on_flush_timeout`** (harness pattern, with a mock `notifier`) — monkeypatch `writer.flush` to return `False`, run `_bootstrap_grid_snapshots()`. Assert: `_bootstrap_grid_snapshots()` returned normally; row was enqueued (writer received the `write()` call); `notifier.alert` called once with `error_key="bootstrap_flush"`; `writer.get_stats()["total_bootstrap_failures"] == 1`.

Note: tests 2, 3, 5, and 6 exercise defensive / stale-correction branches; tests 4, 7, and 8 exercise the degradation/alert path (anomalous future-stale row, probe failure, flush timeout). Production behavior on a fresh restart is almost always test 1 (empty DB → write at `run_start_ts` → flush success).

### `RULES.md`
Append one bullet under the existing "Grid State DB snapshots — feature 0047" section (around line 343–360):

> - **Startup bootstrap probe (issue #108) — best-effort, not blocking**: `Orchestrator.start()` calls `_bootstrap_grid_snapshots()` immediately after the writer's worker thread starts. For each runner, it probes `grid_state_snapshots` for the latest row per `(run_id, account_id, strat_id)` via `get_last_fingerprint(...)` (returns `(fingerprint, exchange_ts)` or `None`) and compares against `grid_fingerprint(current_grid, …)`. Four branches:
>     - **Empty (no row)** → writes the current in-memory grid with `exchange_ts=Run.start_ts` (just-created run row's `start_ts`, also tracked in `_run_start_ts`) as the design-intended lower-bound anchor.
>     - **Match (`last_fp == current_fp`)** → primes `_last_fingerprint` only; no row written.
>     - **Stale, `last_exchange_ts <= Run.start_ts`** (realistic case: residual row from before — or exactly at — this run's start) → writes with `exchange_ts = Run.start_ts` (literally; no `+1ms`). The new row anchors at run start AND supersedes the stale row: same-`exchange_ts` ties are broken by `id DESC` in repository ordering (`ORDER BY exchange_ts DESC, id DESC`), and the autoincrement `id` is higher for the newer insert. Earlier drafts of this plan used `max(Run.start_ts, last_exchange_ts + 1ms)`; that broke the equality edge (`last_exchange_ts == Run.start_ts`) because it pushed the correction to `Run.start_ts + 1ms`, leaving a seed at exactly `Run.start_ts` reading the stale row. The partial unique index includes `raw_fingerprint`, and stale/new rows have different fingerprints (the very reason this branch fires), so same `exchange_ts` cannot conflict.
>     - **Stale, `last_exchange_ts > Run.start_ts`** (anomalous: a fresh `run_id` should not have rows in its own future) → **alert-only, no write**. WARNING log + `notifier.alert(..., error_key="bootstrap_anomalous_{strat_id}")` + bump `writer._total_bootstrap_failures`. Replay seeds in `[Run.start_ts, last_exchange_ts]` cannot honestly be repaired by a single bootstrap write (writing at `Run.start_ts` loses to the stale row; writing at `last + 1ms` leaves the historical window broken) — dual-write with dedupe bypass was considered and rejected as over-engineering for a scenario that shouldn't occur in production. Operator must investigate.
>
>   The method then calls `flush(timeout=5.0) -> bool`; on `False` (timeout) AND on per-runner probe/write exceptions, the bot keeps starting but emits `notifier.alert(...)` + WARNING + bumps `writer._total_bootstrap_failures`. Probe errors are NEVER collapsed to "DB empty" — that would risk duplicate inserts. **Clock-domain caveat**: `Run.start_ts` is wall-clock-derived (`utc_now` default on the column), while live `exchange_ts` is the Bybit exchange clock. `Run.start_ts` is the *design-intended* lower bound, not a strict ordering guarantee — sub-second exchange-vs-wall-clock skew at run start can leave a tiny window in which a seed.at_ts (exchange domain) just below `Run.start_ts` (wall domain) misses the bootstrap row via `at_or_before`. Practically impossible in normal use; flagged so future debugging of "missing-by-milliseconds" replay misses lands on this path, and so future implementers do not build invariants assuming `Run.start_ts <= every live exchange_ts` holds absolutely. Operator-visible degradation: watch `writer.get_stats()["total_bootstrap_failures"]` and the `bootstrap_grid_state_*` notifier channel.

## Implementation Phases

Single phase — all changes are in one process, tightly coupled, and small enough
to land in one feature branch: **`fix/0047-bootstrap-snapshot`** (already created
locally; matches the issue/feature number).

## Out of Scope

- Periodic re-bootstrap (e.g., after `_run_id_provider` returns None mid-run) — current orchestrator guarantees `_run_ids` is set before bootstrap runs; no need for retry logic.
- Changing `_on_grid_change`'s `exchange_ts=None` drop policy — that path is correct for constructor-time `restore_grid`. Bootstrap supplies `Run.start_ts` explicitly.
- Cross-run cleanup of old `grid_state_snapshots` rows — retention is a separate concern (no issue filed).
- Schema migration — `grid_state_snapshots` table already exists post-PR #107.
- Writing snapshots for runners whose grid is unbuilt (`len(grid) <= 1`). If a restart leaves a strategy without a valid grid (e.g., never reached first ticker pre-shutdown), bootstrap writes nothing and replay correctly falls back to a fresh build. Matches `runner._on_grid_change`'s existing guard.
