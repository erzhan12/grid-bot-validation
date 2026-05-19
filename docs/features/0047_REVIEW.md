# Feature 0047 — Code Review: Persist `grid_state` snapshots to DB

**Reviewer:** Cursor (against `docs/features/0047_PLAN.md` v18)  
**Revision:** 2 (post-fix re-review)  
**Scope:** Working tree — gridbot, gridcore, shared/db, replay  
**Tests run:** 44 targeted 0047 tests passed (`TestOnGridChangeDbWriter`, `TestOnGridChangeExchangeTsPropagation`, `test_grid_state_writer`, `TestLoadGridStateFromSnapshots`, `test_engine_seed` 0047 cases, `TestOrchestratorGridStateWriterWiring` including new provisioning test).

---

## Summary

The follow-up work addresses the main gaps from **revision 1**. Runner-level `exchange_ts` propagation is now covered for the highest-risk engine paths; loader tests include `restore_grid` round-trip and independent `grid_step` mismatch; the runbook branches by run vintage; and `Orchestrator.start()` calls `create_tables()` so pre-0047 production DBs get `grid_state_snapshots` without a separate migration step.

**Verdict:** Implementation and test coverage are **strong**. Remaining items are minor (one optional plan test path, stale docstring, acceptance run). **Ready to merge** from a code-review perspective; feature closure still depends on live acceptance (≥1 h recording + Phase 4 replay ≥95%).

---

## Changes since revision 1

| Revision 1 finding | Status |
|--------------------|--------|
| Missing runner `exchange_ts` / DB-guard tests | ✅ Fixed — `TestOnGridChangeDbWriter` (4 tests) + `TestOnGridChangeExchangeTsPropagation` (4 engine-path tests) |
| No `restore_grid` loader test | ✅ Fixed — `test_loader_output_round_trips_through_restore_grid` |
| No dedicated `grid_step` mismatch test | ✅ Fixed — `test_step_value_mismatch_returns_none` |
| Runbook still mandatory `cp` for all runs | ✅ Fixed — branched “0047+ vs pre-0047” in `0029_RUNBOOK.md` Step 7 |
| Schema rollout / no Alembic | ✅ Mitigated — `orchestrator.start()` calls `db.create_tables()` after `_create_run_records`; test `test_start_provisions_grid_state_snapshots_table_on_pre_0047_db` |
| Pre-merge grep gate not evidenced | ✅ Verified — no single-arg `on_change` / `on_grid_change` lambdas remain |

---

## 1. Plan implementation checklist

| Item | Status | Notes |
|------|--------|-------|
| `GridStateSnapshot` model + partial unique index | ✅ | Unchanged from rev 1 |
| Alembic migration | ⚠️ Intentional deviation | Project uses `create_all`; **now also invoked from `Orchestrator.start()`** (`orchestrator.py:289-302`) |
| `GridStateSnapshotRepository` | ✅ | |
| `grid_fingerprint` / `grid_fingerprint_hash` | ✅ | |
| `on_change(grid, exchange_ts)` + engine lifecycle | ✅ | |
| Lockstep call sites | ✅ | Grep: no single-arg callbacks |
| `GridStateWriter` | ✅ | |
| Runner + orchestrator wiring | ✅ | + `create_tables` on start |
| Replay loader + `_load_seed` | ✅ | |
| `RULES.md` | ✅ | |
| `0029_RUNBOOK.md` | ✅ | Vintage branching documented |
| Runner / engine `exchange_ts` tests | ✅ Mostly | See §3.1 for one optional gap |
| Loader `restore_grid` + step mismatch tests | ✅ | |
| Pre-merge grep gate | ✅ | Run locally; clean |
| Acceptance (new recording, ≥95% replay) | ⏳ | Still operator-owned |

---

## 2. Correctness (unchanged strengths)

- **Data shape:** `grid_json` as `list[{side, price}]`; defensive copy in writer; `Decimal(str(grid_step))` on write.
- **Scoping:** `run_id` / `account_id` / `strat_id` filters; UUID5 `account_id` matches `_create_run_records`.
- **Ordering:** FIFO same-`exchange_ts` inserts; `ORDER BY exchange_ts DESC, id DESC`; partial index + `index_where`.
- **Fallbacks:** Missing table → file path; DB preferred over file in `_load_seed`.
- **Lifecycle:** Writer in `__init__` when `db is not None`; thread after `_create_run_records` + schema provision; `flush` + `stop` on shutdown.
- **Silent-failure guard:** Independent file/DB backends; `account_id` required when writer wired.

---

## 3. Remaining issues

### 3.1 Low — `test_check_and_place_rebuild_carries_ticker_ts` not added

Plan listed a runner test for the `_check_and_place` rebuild path (`engine.py:331` — too many orders → `build_grid`). **Not present** in `test_runner.py`. Other ticker-scoped paths are covered (`test_first_ticker_build_carries_ticker_ts`, restored OOB rebuild, deferred fill). Risk is **low** because that path runs inside `_handle_ticker_event`, which already sets `_current_exchange_ts` in try/finally — same mechanism as the covered paths. Add only if you want exhaustive plan checklist closure.

### 3.2 Low — documented dedupe trade-off (unchanged)

In-memory dedupe on `(run_id, account_id, strat_id)` without `exchange_ts` can skip re-persisting an identical grid geometry later in the run. Deliberate per plan; replay may see an older row if state recurs. No change needed unless product wants full timeline fidelity.

### 3.3 Nit — stale `models.py` module docstring

Header still says “Supports 10 tables” and omits `grid_state_snapshots`. Cosmetic only.

### 3.4 Out of scope for code review — acceptance

Issue #101 bar: new recording ≥1 h, Phase 4 replay ≥95%. Rollback policy (revert orchestrator wiring only) unchanged.

---

## 4. New test coverage (revision 2)

### `TestOnGridChangeDbWriter` (`test_runner.py`)

| Test | Plan mapping |
|------|----------------|
| `test_writes_to_db_when_writer_configured` | `test_on_grid_change_writes_to_db_when_writer_configured` |
| `test_skips_db_write_when_writer_not_configured` | ✓ |
| `test_db_write_fires_when_state_store_is_none` | v12 independent guards |
| `test_skips_db_when_exchange_ts_none` | ✓ |

### `TestOnGridChangeExchangeTsPropagation` (`test_runner.py`)

| Test | Plan mapping |
|------|----------------|
| `test_first_ticker_build_carries_ticker_ts` | Initial `build_grid` path |
| `test_restored_grid_oob_rebuild_carries_ticker_ts` | `engine.py:160-181` |
| `test_deferred_fill_consumption_uses_ticker_ts` | `engine.py:194` — asserts exec ts **not** used |
| `test_update_grid_out_of_bounds_rebuild_carries_execution_ts` | Execution OOB double-notify |

Tests drive real `StrategyRunner.on_ticker` / `on_execution` with `Mock` writer — good integration depth without brittle engine internals.

### Replay loader (`test_snapshot_loader.py`)

- `test_step_value_mismatch_returns_none` — `grid_step` branch isolated from count.
- `test_loader_output_round_trips_through_restore_grid` — `Grid.restore_grid` on loader output.

### Orchestrator (`test_orchestrator.py`)

- `test_start_provisions_grid_state_snapshots_table_on_pre_0047_db` — simulates DB without table; `start()` creates it.

All **44** targeted tests passed in re-review run.

---

## 5. Data alignment / regressions

No new alignment issues found. Re-checked:

- Callback arity lockstep across `apps/` and `packages/`.
- `exchange_ts` on writer mock matches event timestamps in propagation tests.
- Loader `Decimal(str(...))` still covered by `test_step_binary_imprecise_match_accepted`.

---

## 6. Over-engineering / style

No new concerns. `create_tables()` in `start()` is a small, idempotent addition appropriate for this repo’s schema model.

---

## 7. Verdict (revision 2)

| Category | Revision 1 | Revision 2 |
|----------|------------|------------|
| Plan fidelity | Good | **Very good** |
| Test completeness vs plan | Fair | **Good** (one optional path omitted) |
| Production safety | Good | **Very good** (schema provision on start) |
| Ready to merge (code) | Yes, with follow-up tests | **Yes** |
| Ready to close feature | No | **No** — acceptance run still required |

### Pre-merge / post-merge checklist

1. ~~Add runner `exchange_ts` tests~~ — done.
2. ~~Runbook vintage branching~~ — done.
3. ~~Schema provision on deploy~~ — `create_tables()` in `start()` + test.
4. **Operator:** fresh recording ≥1 h post-deploy; Phase 4 replay ≥95%; record rate in PR.
5. **Optional:** `test_check_and_place_rebuild_carries_ticker_ts`; fix `models.py` table count in docstring.
