# 0054 Code Review: Fail-Loud on Missing Grid State When `seed.enabled=true`

**Reviewer:** automated review (2026-05-27, pass 2)  
**Plan:** `docs/features/0054_PLAN.md`  
**Scope:** `engine.py`, `snapshot_loader.py` (docs only), `RULES.md`, `test_engine_seed.py`

**Reviewed at:** `feature/0054-fail-loud-missing-grid-seed` working tree (uncommitted).  
**HEAD:** `8a54519` (`feat(0053): unify account_id resolution between recorder and gridbot (#128)`)

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

Pass-1 items are resolved or were out of plan scope:

| Pass-1 ID | Resolution |
|---|---|
| N1 — stacked wallet warnings | **Acceptable / out of scope.** The plan requires one engine-level `WARNING` on `replay.engine` when `wallet_seed is None` (`engine.py:716–720`). Loader-level refusal warnings in `load_wallet_seed_full` (`snapshot_loader.py:513–541`) are pre-existing, use a different logger, and carry diagnostic detail the engine message intentionally omits. `test_load_seed_warns_when_wallet_seed_missing` correctly asserts exactly one engine warning on the missing-coin path (no loader warning). No dedup logic required by the plan. |
| N2 — thin message coverage in test 2 | **Not a plan gap.** `0054_PLAN.md` requires only `pytest.raises(SeedDataQualityError)` for `test_load_seed_raises_when_no_grid_db_and_file_path_missing`; full message-field assertions are required for test 1 only. Test 2 meets the plan. |
| N3 — no file step/count mismatch test | **Not a plan gap.** Plan item 3 explicitly prefers a DB mismatch fixture with `grid_state_path=None`. File-only mismatch exercises the same `grid_source = None` branch already covered by the missing-file and DB-mismatch tests. |

---

## Verdict

**Approve.** The implementation matches the plan. All pass-1 findings are addressed or were optional enhancements beyond plan scope. Targeted tests pass (52/52).

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---:|---|
| Import `SeedDataQualityError` in engine | Pass | `engine.py:63` |
| Replace `grid_source = "fresh"` with raise when both loaders return `None` | Pass | `engine.py:677–689` |
| Exception message includes `strat_id`, `account_id`, `symbol`, `at_ts`, `grid_state_path` | Pass | `engine.py:683–688` |
| Keep INFO log for `"db"` / `"file"` success paths | Pass | `engine.py:690–692` |
| Wallet `None` → single engine WARNING, no raise | Pass | `engine.py:716–720` |
| Wallet warning outside DB session block | Pass | After `with` closes at `engine.py:714` |
| Do not check position/orders loaders for `None` | Pass | No new guards added |
| Update `_load_seed` docstring (grid hard-required, wallet soft) | Pass | `engine.py:626–634` |
| Update inline comment (remove "blank build") | Pass | `engine.py:653–656` |
| Broaden `SeedDataQualityError` class docstring | Pass | `snapshot_loader.py:175–190` |
| Update module docstring (loaders return `None`; engine policy) | Pass | `snapshot_loader.py:11–15` |
| Update `load_grid_state` docstring | Pass | `snapshot_loader.py:240–245` |
| Update `load_grid_state_from_snapshots` docstring | Pass | `snapshot_loader.py:308–312` |
| Loader log text unchanged | Pass | No loader INFO/WARNING text edits |
| `RULES.md` replay loader priority bullet | Pass | `RULES.md:353` |
| Rename `test_load_seed_falls_back_to_fresh_when_no_db_and_no_file` | Pass | Replaced by `test_load_seed_raises_when_no_grid_db_and_no_file_path` — old name absent |
| Test 1: no DB, no file path → raise + message fields | Pass | `test_engine_seed.py:673–716` |
| Test 2: no DB, missing file → raise | Pass | `test_engine_seed.py:718–752` |
| Test 3: DB step/count mismatch → raise | Pass | `test_engine_seed.py:754–811` |
| Test 4: wallet missing → WARNING + no raise | Pass | `test_engine_seed.py:813–868` |
| `seed.enabled=false` behaviour unchanged | Pass | Early return at `engine.py:641–642`; `test_disabled_seed_returns_all_none` preserved |
| Loader logic unchanged | Pass | No functional diffs in loader bodies |

---

## Code Quality Review

### Correctness

Grid guard matches the plan algorithm: initialise `grid_source = "db"`, try DB, fall back to file when `grid_state_path` is set, set `grid_source = None` when both paths fail, raise `SeedDataQualityError` before the success-path INFO log.

Fail-fast inside the DB session (before position/wallet/order loaders) prevents wasted loader calls and partial seed state on a hard-required grid miss.

`grid_source` semantics are correct: stays `"db"` when the DB loader succeeds; becomes `"file"` only when DB returned `None` and the file loader succeeded.

Wallet soft-fallback is wired consistently with the caller at `engine.py:222–225`. The engine WARNING covers the simple missing-coin path; loader refusal paths retain their own detailed warnings on `replay.snapshot_loader`.

No snake_case / camelCase or nested-object alignment issues.

### Over-engineering / file size

Minimal, focused diff (+271 / −61 across four files). Reuses existing `SeedDataQualityError`. New `TestReplayEngineSeedFailLoud` class groups fail-loud cases without bloating `TestReplayEngineSeedingPipeline`.

### Style

Matches existing patterns: feature-referenced comments (`0047 + 0054`), typed `grid_source: Optional[str]`, docstring and test conventions consistent with 0052/0047.

---

## Test Review

| Criterion | Assessment |
|---|---|
| Primary regression (0052 silent fresh-build) | Covered — `test_load_seed_raises_when_no_grid_db_and_no_file_path` |
| Missing file path | Covered — plan requires `pytest.raises` only |
| Step/count mismatch ≡ absence (DB path) | Covered — per plan preference |
| Wallet soft-required WARNING | Covered — BTC coin trick; one engine WARNING asserted |
| Happy paths preserved (DB prefer, file fallback, cross-run) | Preserved |
| `seed.enabled=false` | Preserved |
| Isolation / mocking | DB fixtures + `caplog`; no unnecessary mocks |
| Naming / patterns | Clear, plan-referenced docstrings |
| Speed | 52 tests in 0.32s |

---

## Verification

```bash
uv run pytest apps/replay/tests/test_snapshot_loader.py \
             apps/replay/tests/test_engine_seed.py \
             -q --tb=short
# 52 passed in 0.32s
```

---

## Notes

- Post-merge operator validation: run a seed-enabled replay with no `grid_state_snapshots` row and no file — confirm replay aborts with `SeedDataQualityError` (not `grid seed source=fresh`).
- On wallet loader refusal paths (legacy NULL `total_equity`, zero balance), expect loader-level warnings from `replay.snapshot_loader` in addition to the engine fallback WARNING — this is pre-existing loader behaviour, not a 0054 defect.
