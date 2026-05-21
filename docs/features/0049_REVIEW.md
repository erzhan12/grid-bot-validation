# 0049 Code Review: Surgical recorder wipe for shared Phase 4 DB

**Reviewer:** automated review (2026-05-21, pass 3)  
**Plan:** `docs/features/0049_PLAN.md`  
**Scope:** `scripts/phase4/start_recorder.sh`, `scripts/phase4/stop_recorder.sh`, `docs/features/0029_RUNBOOK.md`, `RULES.md`

**Reviewed at:** `feature/0049-surgical-recorder-wipe` working tree (uncommitted).  
**HEAD:** `55e1e96` (`fix(0048): remove per-tick drift detector, restore bbu2 grid semantics (#112)`)

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

The implementation matches the plan's narrow scope: shell helpers and docs only. The tracked recorder YAML was not changed by this feature, and no gridbot, replay, or comparator code was modified.

### Residual test gap

- No automated test was added for the shell-helper wipe. This is not a plan violation: the plan asked for local temp-DB verification rather than a committed test. Still, a small regression test that extracts/runs the wipe SQL against a seeded SQLite DB would be useful if Phase 4 shell helpers keep changing.

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---:|---|
| Replace DB-file deletion in `start_recorder.sh` with surgical SQL wipe | Pass | Uses `.bail on`, `PRAGMA foreign_keys = ON`, `BEGIN IMMEDIATE`, ordered DELETEs, and `COMMIT`. |
| Preserve DB file plus `-wal` / `-shm` sidecars | Pass | `start_recorder.sh` now removes only `/tmp/recorder.log`; no `rm -f "$DB_PATH"` or sidecar delete remains. |
| Skip SQL wipe when DB file is missing | Pass | `[[ -f "$DB_PATH" ]]` gates the SQL batch and logs first-start behavior. |
| Wipe only recorder-owned data | Pass | Wipes `private_executions`, `orders`, `wallet_snapshots`, `position_snapshots WHERE source='live'`, `ticker_snapshots`, and `runs WHERE run_type='recording'`. |
| Do not wipe `public_trades` | Pass | Not present in the SQL wipe; verified preserved in temp DB. |
| Preserve gridbot/shared data | Pass | `grid_state_snapshots`, live runs, accounts, strategies, and users survive the verified wipe. |
| Update `stop_recorder.sh` identifier queries | Pass | `RUN_ID` and `ACCOUNT_ID` both come from the latest `runs.run_type='recording'` row; missing recording run prints a warning. |
| Update runbook for shared DB, absolute URL, credentials, Steps 4b/7/8/10 | Pass | Runbook documents four-slash absolute SQLite URL, readonly recorder creds, surgical helper behavior, scoped run selection, and absolute replay/comparator DB URLs. |
| Document Feature 0047 grid-state run-id caveat | Pass | Step 7 says to keep using `seed.grid_state_path` until replay resolves live-run vs recording-run lookup. |
| Add operational rule in `RULES.md` | Pass | Rule records shared DB contract, surgical wipe table list, preserved data, scoped recording-run identifiers, and known replay caveat. |

---

## Verification

Commands/checks run in this pass:

```bash
git diff --check
# no output

rg -n 'rm -f "\$DB_PATH"' scripts/phase4/start_recorder.sh
# no matches

uv run python -c '...'  # seeded real grid_db schema in /private/tmp/phase4_0049_review.db and ran the wipe SQL
```

Temp-DB verification result:

| Check | Result |
|---|---:|
| `runs WHERE run_type='recording'` | 0 |
| `runs WHERE run_type='live'` | 1 |
| `runs WHERE run_type='backtest'` | 1 |
| `grid_state_snapshots` | 1 |
| `bybit_accounts` | 1 |
| `strategies` | 1 |
| `ticker_snapshots` | 0 |
| `private_executions` | 0 |
| `orders` | 0 |
| `wallet_snapshots` | 0 |
| `position_snapshots WHERE source='live'` | 0 |
| `position_snapshots WHERE run_id='recording-old' AND source='backtest'` | 0, removed by FK cascade from deleted recording run |
| `position_snapshots WHERE run_id='backtest-keep' AND source='backtest'` | 1 |
| `public_trades` | 1 |

---

## Notes

- `stop_recorder.sh` still exits early without printing identifiers when no recorder process is running. That behavior predates 0049 and is outside the plan scope.
- The runbook still has a Step 6 monitoring example using `sqlite3 data/recorder_ltcusdt_phase4.db`; it is explicitly a live monitoring helper and remains safe when run from the repo root, though the operator path elsewhere now prefers absolute DB URLs.

## Verdict

Approve the current working tree. Commit the 0049 working-tree changes before merge so PR reviewers see the updated shell helpers, runbook, and review file.
