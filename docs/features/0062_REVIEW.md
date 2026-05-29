# Feature 0062 Code Review

**Plan:** `docs/features/0062_PLAN.md`  
**Scope:** Run-active guard on `GridStateSnapshotRepository.get_at_or_before`; docstring/RULES updates; repository, replay-loader, and gridbot test fixes  
**Branch:** `feature/0062-exclude-ended-runs-seed-lookup` (uncommitted working tree)  
**Review pass:** 2 (post-fix)

---

## Verdict

**Approve with one minor scope note.** Production change matches the locked design: INNER JOIN `runs`, `run_type IN ('live','shadow')`, `start_ts <= at_ts`, inclusive `end_ts` window, unchanged `ORDER BY`. `get_latest` is untouched. All plan-mandated test suites pass. Pass-1 polish (shadow `run_type` coverage) is resolved. The only remaining pre-merge item is dropping or splitting an unrelated `conf/gridbot_test.yaml` edit.

---

## Resolved From Prior Review

| Pass-1 ID | Resolution |
|---|---|
| Note — No `shadow` run_type unit test | Fixed — `test_get_at_or_before_includes_active_shadow_run` added at `test_repositories.py:1980-1999`; locks the `shadow` branch of `run_type.in_(("live","shadow"))`. |

---

## Plan Implementation Check

| Step | Status | Evidence |
|---|---|---|
| **1** — Add `or_` import | Pass | `shared/db/src/grid_db/repositories.py:5` |
| **2** — Rewrite `get_at_or_before` query | Pass | `repositories.py:1330-1346` — JOIN + three Run predicates per §4.3 |
| **3** — Update docstrings (repo + loader) | Pass | `repositories.py:1309-1328`, `snapshot_loader.py:305-313` |
| **4** — Update RULES.md cross-run paragraph | Pass | `RULES.md:355` — run-active guard + pitfalls 1 & 2 |
| **5a** — `test_get_at_or_before_cross_run` recording→live | Pass | `test_repositories.py:1809` |
| **5b** — New repository unit tests (plan §6.1 #1–#6 + shadow) | Pass | `test_repositories.py:1940-2084` — seven tests via `_seed_grid_run` helper |
| **5c** — `test_get_at_or_before_filters_by_symbol` fix | Pass | Dedicated `writer_run` with `start_ts <= at_ts` |
| **6a/6b** — Engine seed tests repoint grid snapshots to per-test live runs | Pass | `test_engine_seed.py:369-384`, `769-784` |
| **6c** — `test_load_seed_prefers_db_snapshot_from_other_run_id` unchanged | Pass | Still uses local `gridbot-live-run` |
| **6e** — Engine reproducer `test_load_seed_raises_when_only_ended_gridbot_run_has_snapshots` | Pass | `test_engine_seed.py:848-920` |
| **6f** — `grid_writer_run` fixture + nine loader test repoints | Pass | `test_snapshot_loader.py:109-127`; zero `_make_grid_row(sample_run` hits |
| **8** — Gridbot writer fixture `start_ts` for fixed-past `at_ts` | Pass | `test_grid_state_writer.py:47-50` |
| **Non-goals** — No `get_latest` change, no migration, no 0054 policy change | Pass | Production + test diff on plan files (+ stray config) |

---

## Findings

### N1 — Unrelated config change (Minor — pre-merge cleanup)

`conf/gridbot_test.yaml` still changes SOLUSDT `grid_step: 1.0 → 0.3`. This is **not** mentioned in the 0062 plan and is unrelated to the seed lookup fix. Revert it or move to a separate commit/PR so the 0062 diff stays focused and reviewable.

No blocking or non-blocking code defects against the current working tree beyond N1.

### Notes (not defects)

- **Unclean shutdown gap (documented).** Predicate does not exclude orphaned `end_ts=NULL` runs after crash/kill. Plan §4.6 accepts this for v1; docstrings and RULES.md document the follow-up (startup orphan cleanup).
- **Engine reproducer timestamps.** Test uses `end_ts = snapshot_ts + 30s` (after snapshot write, before `at_ts = seed_ts`) rather than the plan's simplified `seed_ts - 5m`. More realistic and still satisfies `end_ts < at_ts`.
- **`_seed_grid_run` helper.** Small DRY helper on the test class for seven repository tests — reasonable, matches existing insert style, not over-engineered.
- **Data alignment.** No snake_case/camelCase or nested-object issues introduced. `GridStateSnapshot.strat_id` vs `Run.strategy_id` naming split is pre-existing (0052); JOIN is on `run_id` only, snapshot-side `(account_id, strat_id, symbol)` filters unchanged.
- **EXPLAIN / index follow-up.** Plan §4.7/§8 #5 expects production `EXPLAIN (ANALYZE, BUFFERS)` in PR notes — operator follow-up, not a code defect.

---

## Code Quality Review

### Correctness

- Query matches §4.3 exactly: INNER JOIN (orphan-safe), inclusive boundaries, `run_type` guard, preserved tie-break.
- Returning `None` for stale ended runs correctly triggers 0054 fail-loud when `seed.enabled=True` and no file fallback — no new exception types.
- `get_latest` (live gridbot hot path) unchanged — no bot restart required.

### Over-engineering / style

- Production diff is ~20 lines; bulk of change is tests and docs (~398 lines total across 8 files). No new abstractions in production code.
- Comments use existing `0062` / feature-tag conventions; ruff clean on changed sources.
- Docstrings honestly note unclean-shutdown limitation and index non-coverage — good alignment with plan §4.7.

### Unit tests

| Area | Assessment |
|---|---|
| Bug reproducer (ended run → `None`) | Covered — `test_get_at_or_before_excludes_run_ended_before_at_ts` |
| Active run, NULL `end_ts` | Covered — `test_get_at_or_before_includes_active_run_null_end_ts` |
| Active `shadow` run | Covered — `test_get_at_or_before_includes_active_shadow_run` |
| Boundary inclusivity (`start_ts`, `end_ts`) | Covered — boundary tests |
| Recording run exclusion | Covered — `test_get_at_or_before_excludes_recording_run` |
| Overlapping active runs / ORDER BY | Covered — `test_get_at_or_before_two_active_runs_latest_wins` |
| Cross-run (0052) still works | Covered — updated `test_get_at_or_before_cross_run` |
| Symbol isolation (not spurious run guard) | Covered — fixed `test_get_at_or_before_filters_by_symbol` |
| Engine-level fail-loud on stale grid | Covered — `test_load_seed_raises_when_only_ended_gridbot_run_has_snapshots` |
| Fixture trap (recording run for grid) | Fixed — per-test live runs in engine tests; `grid_writer_run` in loader tests |
| Loader happy path + mismatch paths | Covered — nine repointed `TestLoadGridStateFromSnapshots` tests |
| Gridbot writer tie-break | Covered — fixture `start_ts` fix |
| Isolation | Good — in-memory SQLite, no external I/O; repository tests build own runs for fixed-past `at_ts` |
| Naming | Clear `0062` docstrings; reproducer names match plan |

**Not covered in v1 (per plan):** unclean restart with orphaned open run.

---

## Commands Run

```bash
uv run pytest shared/db/tests/test_repositories.py -q
```

Result: **72 passed**

```bash
uv run pytest apps/replay/tests/test_engine_seed.py -q
```

Result: **14 passed**

```bash
uv run pytest apps/replay/tests/test_snapshot_loader.py -q
```

Result: **41 passed**

```bash
uv run pytest apps/gridbot/tests/test_grid_state_writer.py -q
```

Result: **14 passed**

```bash
uv run pytest apps/replay -q
```

Result: **102 passed**

```bash
uv run pytest shared/db -q
```

Result: **144 passed**

```bash
uv run pytest apps/gridbot/tests/test_orchestrator.py -q -k "get_at_or_before or bootstrap"
```

Result: **10 passed**

```bash
uv run ruff check shared/db/src/grid_db/repositories.py apps/replay/src/replay/snapshot_loader.py \
  shared/db/tests/test_repositories.py apps/replay/tests/test_engine_seed.py \
  apps/replay/tests/test_snapshot_loader.py apps/gridbot/tests/test_grid_state_writer.py
```

Result: All checks passed

---

## Acceptance / Operator Follow-up

- **Pre-merge:** Revert or split `conf/gridbot_test.yaml` (`grid_step` change).
- **PR notes (optional per plan):** Capture `EXPLAIN (ANALYZE, BUFFERS)` for `get_at_or_before` on production-like Postgres; defer index migration unless seq scan at expected row counts.
- **Follow-up feature:** Gridbot startup orphan-run cleanup for unclean shutdown (§4.6).
- **Commit:** Not performed in this review.
