# 0053 Code Review: Unified account_id Resolution (Recorder ↔ Gridbot)

**Reviewer:** automated review (2026-05-26, pass 2)  
**Plan:** `docs/features/0053_PLAN.md`  
**Scope:** `grid_db.identity`, gridbot orchestrator refactor, recorder config/identity migration, `prepare_session`, `shared_db_parents`, `start_recorder.sh`, runbook/RULES/replay template updates, tests

**Reviewed at:** `feature/0053-unify-account-id` working tree (uncommitted).  
**HEAD:** `5573938` (`fix(0052): cross-run grid_state_snapshot seed lookup for replay (#126)`)

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

Pass-1 items resolved:

| Pass-1 ID | Resolution |
|---|---|
| N1 — gridbot test inlined uuid5 | Fixed — `test_writer_threaded_into_runner_with_correct_account_id` now asserts `runner._account_id == account_id_for(account_config.name)` (`test_orchestrator.py:1808`) |
| N4 — stale `$DB_PATH-wal` runbook text | Fixed — Step 4b item 4 now reads “Leaves the SQLite `-wal` / `-shm` sidecars intact” without referencing `$DB_PATH` (`0029_RUNBOOK.md:267–268`) |
| N6 — Step 7 uuid5 `ACCOUNT_ID` note | Fixed — post-0053 paragraph added after the capture queries explaining uuid5 vs legacy placeholder and local replay YAML migration (`0029_RUNBOOK.md:404–408`) |

### Note — Compact checklist still mentions grid_anchor copy (cosmetic)

The main runbook section correctly says Feature 0047+ shared-DB runs can **omit** the `grid_anchor.phase4.json` copy when seed logs show `grid seed source=db` (`0029_RUNBOOK.md:436–443`), but the compact checklist at line 611 still says `copy grid_anchor.json` without the optional qualifier. Not wrong as a fallback step, but operators skimming only the checklist may still do unnecessary file copies. Optional one-line tweak: “copy `grid_anchor.json` (optional fallback — omit for 0047+ shared DB if seed logs show `source=db`)”.

### Intentionally deferred (optional per plan)

| Item | Status |
|---|---|
| N2 — replay `seed.account_id` mismatch warning (plan §6.3) | Not implemented; plan marked optional. Hollow-seed behavior unchanged; operators rely on seed pre-check + updated `.example` templates. |
| N3 — dedicated `test_shared_db_parents.py` | Not created; plan marked optional. Coverage lives in `test_prepare_recorder_session.py` (direct `verify_shared_db_parents` calls) and `test_recorder_shared_db.py` (integration via `_seed_db_records`). |

**Extra (positive):** `test_wipe_succeeds_on_existing_empty_schemaless_file` in `test_prepare_recorder_session.py` goes beyond the plan — `_wipe_recorder_data` calls `create_tables()` before DELETE so an empty/schemaless SQLite file does not raise `no such table`. Sensible hardening.

---

## Verdict

**Approve.** The implementation matches the plan. Pass-1 actionable findings are addressed. Shared identity is centralized in `grid_db.identity`, recorder shared-DB mode is verify-only (no silent `merge` over gridbot parents), and `prepare_recorder_session` ships atomically with recorder startup so clean-DB recorder-first ordering is preserved.

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---:|---|
| **Phase 1** — `grid_db/identity.py` with namespace + three helpers | Pass | `shared/db/src/grid_db/identity.py`; exported from `grid_db/__init__.py:25–30` |
| Pin exact UUID outputs | Pass | `shared/db/tests/test_identity.py` |
| **Phase 2** — Remove orchestrator inline uuid5 / `_account_id_for` | Pass | `grep uuid5 orchestrator.py` → no matches; `from uuid import UUID` only at line 17 |
| Replace call sites (745, 1318, `_create_run_records`) | Pass | `orchestrator.py:736, 1224–1225, 1251, 1305` |
| `test_orchestrator.py` uses `account_id_for` | Pass | `test_orchestrator.py:16, 1808, 1997` |
| `runner.py` error string updated | Pass | `runner.py:208–209` |
| **Phase 3** — `AccountConfig.name` + `strat_id` required | Pass | `recorder/config.py:19–36` |
| YAML configs + runbook Step 3 snippet | Pass | `recorder_ltcusdt.yaml:38–41`, `recorder.yaml.example:29–36`, `0029_RUNBOOK.md:170–191` |
| All `AccountConfig` test fixtures updated | Pass | `conftest.py`, `test_config.py`; `grep AccountConfig apps/recorder/` |
| `db_with_gridbot_seed` fixture + account-mode tests wired | Pass | `conftest.py:73–105`; 9 tests in `test_recorder.py`, 5 in `test_initial_rest_snapshot.py` |
| `test_recorder.py` sentinel / live-id refactor | Pass | `TEST_RECORDER_*` for fallback; `recorder._account_id` / `_user_id` for shared-DB |
| **Phase 4** — Remove `_RECORDER_*` constants; sentinel attrs in `__init__` | Pass | `recorder.py:97–104` |
| Shared-DB branch: uuid5 + `verify_shared_db_parents` | Pass | `recorder.py:730–750` |
| Fallback branch: legacy upserts on sentinel attrs | Pass | `recorder.py:751–773` |
| All `_RECORDER_*` call-site replacements | Pass | Handlers, REST snapshot, Run insert, private-gap writes use `self._*\_id` |
| **`shared_db_parents.py`** — 3 existence + 5 metadata checks | Pass | `shared_db_parents.py:47–92` |
| **`prepare_session.py`** — wipe, bootstrap, verify, CLI | Pass | Full module; exports per plan |
| Thin CLI wrapper | Pass | `scripts/phase4/prepare_recorder_session.py` |
| `gridbot` workspace dep on recorder | Pass | `apps/recorder/pyproject.toml:10, 20` |
| **`start_recorder.sh`** — remove shell grep wipe; single prepare call | Pass | `start_recorder.sh:60–64`; no `DB_PATH` / inline `sqlite3` |
| Runbook Step 4b + manual equivalent rewrite | Pass | `0029_RUNBOOK.md:235–291` |
| Runbook Step 7 uuid5 `ACCOUNT_ID` note | Pass | `0029_RUNBOOK.md:404–408` |
| **Phase 5** — §5.1 + §5.2 wipe in Python | Pass | `prepare_session.py:105–141` |
| `ticker_snapshots` excluded | Pass | Not in DELETE list; documented in runbook + RULES |
| **Phase 5b** — `test_recorder_shared_db.py` | Pass | Positive + 8 negative cases |
| **Phase 4c tests** — `test_prepare_recorder_session.py` | Pass | 14 tests (plan table + 2 extras: schemaless wipe, pysqlite driver) |
| **Phase 6** — replay example templates | Pass | `replay_0045_validation.yaml.example`, `replay_ltcusdt_phase4.yaml.example` |
| **Phase 6.3** — replay mismatch warning | Deferred | Optional per plan; not implemented |
| **RULES.md** updates | Pass | `RULES.md:360`, `RULES.md:2021` |

---

## Code Quality Review

### Correctness

Root cause and fix remain sound: recorder previously stamped the placeholder `00000000-…-002` while gridbot used `uuid5(NAMESPACE, "account:mainnet_live")`, so replay seed loaders filtered on the wrong `account_id` and returned hollow defaults. Centralizing identity in `grid_db.identity` and deriving recorder IDs from `config.account.name` / `strat_id` aligns child rows with gridbot parent FKs.

Shared-DB mode correctly avoids `session.merge` on gridbot-owned parents — verify-only prevents silent metadata corruption (e.g. flipping `BybitAccount.environment` or overwriting `Strategy.config_json`). Bootstrap insert-if-missing mirrors gridbot's `_create_run_records` (`orchestrator.py:1228–1262` ↔ `prepare_session.py:217–239`).

Initialization order preserves identity before handler lambdas capture `self._account_id`: `_seed_db_records` completes inside `_init_writers` before `_init_collectors` constructs `PrivateCollector`.

No snake_case / nested-object alignment issues observed: verify helpers take `str` UUIDs; recorder stores `UUID` and uses `str(self._*\_id)` at DB/write boundaries consistently.

**Operational notes (not code defects):**

- `verify_shared_db_parents` rejects recorder configs whose `symbols[0]` differs from the gridbot Strategy row symbol — intentional 1:1 strategy scope.
- Bootstrap writes `BybitAccount.environment` from **gridbot** `account_config.testnet`; recorder top-level `testnet` must match (enforced at prepare time).
- Local gitignored replay YAMLs under `apps/replay/conf/` may still hardcode the placeholder; operators must migrate per plan §6.2 (committed `.example` files and Step 7 note cover this).

### Over-engineering / file size

Appropriate decomposition: thin `identity` module, shared `verify_shared_db_parents`, testable `prepare_session` with shell wrapper. No unnecessary abstractions.

### Style

Matches existing patterns throughout. Gridbot tests now import `account_id_for` consistently with production code.

---

## Test Review

| Criterion | Assessment |
|---|---|
| Identity formula pinned | `test_identity.py` — 7 tests |
| Gridbot refactor regression | `test_orchestrator.py` — full suite in verification run |
| Recorder config validation | `test_config.py` — all fixtures include `name` / `strat_id` |
| Shared-DB `_seed_db_records` happy path | `test_recorder_shared_db.py::test_seed_does_not_mutate_gridbot_parents` |
| Verify failure modes (8 negatives) | `test_recorder_shared_db.py` |
| Prepare session wipe / bootstrap / verify | `test_prepare_recorder_session.py` — 14 tests |
| Account-mode recorder integration | `test_recorder.py` + `test_initial_rest_snapshot.py` with `db_with_gridbot_seed` |
| Fallback mode unchanged | `TEST_RECORDER_ACCOUNT_ID` sentinels in handler-drain test |
| Replay seed regression | `test_engine_seed.py` — included in verification |
| Isolation / mocking | In-memory SQLite + mocked WS/REST; no live exchange |
| Naming / patterns | Plan-referenced docstrings; descriptive test names |
| Speed | 235 passed, 2 skipped in 1.52s |

---

## Verification

```bash
uv run pytest shared/db/tests/test_identity.py \
             apps/gridbot/tests/test_orchestrator.py \
             apps/recorder/tests/test_prepare_recorder_session.py \
             apps/recorder/tests/test_recorder_shared_db.py \
             apps/recorder/tests/test_config.py \
             apps/recorder/tests/test_recorder.py \
             apps/recorder/tests/test_initial_rest_snapshot.py \
             apps/replay/tests/test_engine_seed.py \
             -q --tb=line
# 235 passed, 2 skipped in 1.52s
```

---

## Notes

- Post-merge operator validation: run `scripts/phase4/start_recorder.sh` on a clean shared DB; confirm prepare stdout shows bootstrap + verify, recorder log shows `"Initial REST snapshot"`, and `SELECT account_id FROM runs WHERE run_type='recording' ORDER BY start_ts DESC LIMIT 1` returns `9bdb9748-f9e0-5c13-b144-0ad6a8dbcaba` for `mainnet_live`.
- Deploy constraint from plan: Phase 4 verify-only recorder + `prepare_recorder_session` wired in `start_recorder.sh` must ship together — satisfied in this branch.
- Update local replay configs still using `00000000-0000-0000-0000-000000000002` before expecting position/wallet/order seeds to resolve from DB.
