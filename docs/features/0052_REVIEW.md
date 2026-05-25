# 0052 Code Review: Fix DB-backed grid_state_snapshot seed in replay (cross-run lookup)

**Reviewer:** automated review (2026-05-25, pass 2)  
**Plan:** `docs/features/0052_PLAN.md`  
**Scope:** `repositories.py`, `snapshot_loader.py`, `engine.py`, `RULES.md`, `0029_RUNBOOK.md`, related tests

**Reviewed at:** `feature/0052-db-seed-cross-run` working tree (uncommitted).  
**HEAD:** `0f5950d` (`feat(0051): flip replay default fill mode to last_cross (#125)`)

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

Pass-1 items are resolved:

| Pass-1 ID | Resolution |
|---|---|
| N1 — stale runbook | Fixed — `docs/features/0029_RUNBOOK.md:367–397` now documents DB seeding as the primary path (0052 cross-run lookup), lists file-fallback conditions, and tells operators to confirm `grid seed source=db` / `grid snapshot loaded from run_id=…` in seed logs |
| N2 — RULES.md gap | Fixed — `RULES.md:355` adds the full cross-run contract, success-log shape, and explicit “do not unify `get_latest` / `get_at_or_before`” guard; `RULES.md:2021` Phase 4 section cross-references the fix |
| N3 — mismatch log guard untested | Fixed — `test_step_count_mismatch_returns_none` and `test_step_value_mismatch_returns_none` assert the success log is absent (`test_snapshot_loader.py:507–511, 573–577`) |

### Note — Compact checklist still mentions grid_anchor copy (cosmetic)

The main runbook section correctly says Feature 0047+ shared-DB runs can **omit** the `grid_anchor.phase4.json` copy (`0029_RUNBOOK.md:387–388`), but the compact checklist at line 562 still says `copy grid_anchor.json`. Not wrong as a fallback step, but operators skimming only the checklist may still do unnecessary file copies. Optional one-line tweak: “copy `grid_anchor.json` (optional fallback — omit for 0047+ shared DB if seed logs show `source=db`)”.

---

## Verdict

**Approve.** The implementation matches the plan. All pass-1 findings are addressed. Targeted tests pass (63/63).

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---:|---|
| Drop `run_id` from `get_at_or_before` signature + WHERE | Pass | `repositories.py:1287–1318` |
| Add required `symbol` arg + WHERE predicate | Pass | `repositories.py:1291, 1314` |
| Preserve `ORDER BY exchange_ts DESC, id DESC` | Pass | `repositories.py:1317–1318` |
| Leave `get_latest` unchanged (per-run) | Pass | `repositories.py:1250–1285` |
| Document intentional asymmetry in both docstrings | Pass | `repositories.py:1257–1271, 1294–1306` |
| Remove `run_id` from `load_grid_state_from_snapshots` | Pass | `snapshot_loader.py:271–276` |
| Add `symbol` param + pass-through to repo | Pass | `snapshot_loader.py:275, 323–325` |
| Update loader docstring (cross-run semantics) | Pass | `snapshot_loader.py:280–294` |
| Success-path INFO log after mismatch guards | Pass | `snapshot_loader.py:331–346` |
| Engine call: drop `run_id`, add `config.symbol` | Pass | `engine.py:654–661` |
| Position/wallet loaders still use recorder `run_id` | Pass | `engine.py:678–688` |
| Replace `test_cross_run_isolation` → `test_cross_run_match_succeeds` | Pass | `test_snapshot_loader.py:365–397` |
| Add `test_cross_run_logs_actual_run_id` | Pass | `test_snapshot_loader.py:400–437` |
| Add `test_cross_symbol_isolation` | Pass | `test_snapshot_loader.py:439–467` |
| Mismatch tests assert success log absent | Pass | `test_snapshot_loader.py:507–511, 573–577` |
| Add `test_load_seed_prefers_db_snapshot_from_other_run_id` | Pass | `test_engine_seed.py:384–446` |
| Repo tests: cross-run + symbol filter | Pass | `test_repositories.py:1761–1847` |
| Gridbot writer + orchestrator test call sites updated | Pass | `test_grid_state_writer.py:344–346`, `test_orchestrator.py:2152–2154` |
| Orchestrator `get_latest` call sites unchanged | Pass | L2103, L2151, L2195 |
| Schema / writer unchanged | Pass | No writer or migration diffs |
| Operator docs updated | Pass | `0029_RUNBOOK.md:367–397`, `RULES.md:355, 2021` |

---

## Code Quality Review

### Correctness

The root cause and fix remain sound: gridbot writes under its live `run_id`, replay receives the recorder's `run_id`, and the old WHERE clause matched zero rows. Post-fix lookup scopes by `(account_id, strat_id, symbol, exchange_ts <= at_ts)` without `run_id`.

The intentional asymmetry between `get_latest` (per-run, writer dedupe + bootstrap probe) and `get_at_or_before` (cross-run, replay seed) is preserved in code and now mirrored in `RULES.md`. Changing `get_latest` would break issue #108 bootstrap invariants.

`config.symbol` aligns grid seed lookup with position and active-order loaders at the same seed step.

No snake_case / camelCase or nested-object alignment issues: `row.grid_json` flows directly into `GridStateSeed` / `Grid.restore_grid` as `list[{side, price}]`.

**Operational note (not a code defect):** symbol matching is exact string equality — replay YAML `symbol` must match what gridbot wrote (typically uppercase `BTCUSDT` / `LTCUSDT`).

### Over-engineering / file size

Minimal, focused diff. No new abstractions, no schema migration, no writer changes.

### Style

Matches existing patterns throughout. Doc updates follow the same feature-referenced style as other recent plans (0047, 0049, 0051).

---

## Test Review

| Criterion | Assessment |
|---|---|
| Happy path (loader + engine) | Covered |
| Cross-run regression (production failure mode) | Covered |
| Cross-symbol isolation (F-1-2) | Covered |
| Cross-strat isolation | Preserved |
| Step/count mismatch → None + no false success log | Covered (N3 fix) |
| Success log includes actual writer `run_id` | Covered |
| Engine end-to-end `grid seed source=db` | Covered |
| Repo ordering / tie-break | Covered |
| Pre-0047 missing table guard | Preserved |
| Isolation / mocking | DB fixtures only — appropriate |
| Naming / patterns | Clear, plan-referenced docstrings |
| Speed | 63 tests in 0.64s |

---

## Verification

```bash
uv run pytest apps/replay/tests/test_snapshot_loader.py \
             apps/replay/tests/test_engine_seed.py \
             shared/db/tests/test_repositories.py::TestGridStateSnapshotRepository \
             apps/gridbot/tests/test_grid_state_writer.py::TestGridStateWriter::test_get_at_or_before_picks_largest_id_on_tie \
             apps/gridbot/tests/test_orchestrator.py::TestOrchestratorBootstrapGridSnapshots \
             -q --tb=short
# 63 passed in 0.64s
```

---

## Notes

- Post-merge operator validation: run a Phase 4 replay against a shared DB where gridbot has written snapshots and confirm logs show `grid seed source=db` plus `grid snapshot loaded from run_id=<live-run-id>`, not `source=file`.
- Historical plan docs (`0047_PLAN.md`, `0047_BOOTSTRAP_PLAN.md`) still show the old `get_at_or_before(run_id, …)` signature — acceptable as archival.
- Commit working-tree changes (including this review and `0052_PLAN.md`) before opening the PR.
