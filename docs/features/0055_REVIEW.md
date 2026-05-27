# 0055 Code Review: Fail-Loud on Incomplete REST Snapshot in `start_recorder.sh`

**Reviewer:** automated review (2026-05-27, pass 2)  
**Plan:** `docs/features/0055_PLAN.md`  
**Scope:** `recorder.py`, `start_recorder.sh`, `lib/recorder_snapshot_check.sh`, `lib/recorder_stop.sh`, `0029_RUNBOOK.md`, `RULES.md`, `status.sh`, `.gitignore`, related tests

**Reviewed at:** `feature/0055-fail-loud-rest-snapshot` working tree (uncommitted).  
**HEAD:** `59b6c06` (`feat(0054): fail-loud on missing grid state when seed.enabled=true (#131)`)

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

Pass-1 items are resolved:

| Pass-1 ID | Resolution |
|---|---|
| N1 — rc=1 message assumed zero-count failure | Fixed — `start_recorder.sh:101–103` now reads `(auth failure or zero wallet/position rows)` and directs the operator to the classifier diagnostic above for the specific cause |
| N2 — early return without sentinel on `_run_id` guard | Fixed — `_run_id` clause removed; guard is `if not self._config.account: return` only (`recorder.py:296–297`). Docstring documents the intentional no-sentinel no-account exception for Phase 4 (`recorder.py:291–294`) |

### Notes (not defects)

- **`recorder_stop.sh` is a beneficial scope extension.** Centralises kill+pgrep used in three launcher branches; covered by `TestStopRecorderPattern`.
- **Timeout path kills the recorder.** rc=2 handler stops the process before exit — correct for cron/CI retry safety.
- **`.gitignore` negation for `scripts/phase4/lib/` is required.** Global `lib/` ignore would otherwise exclude the new shell libs.
- **`status.sh` sentinel grep** is a sensible operator tweak (out of plan, aligned with runbook).

---

## Verdict

**Approve.** The implementation matches the plan. All pass-1 findings are addressed. Targeted tests pass (19/19).

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---:|---|
| Emit `RECORDER_SNAPSHOT_INCOMPLETE` on auth-client construction failure | Pass | `recorder.py:309–312` |
| Emit `RECORDER_SNAPSHOT_OK` on success (wallet+position > 0) | Pass | `recorder.py:347–349` |
| Emit `RECORDER_SNAPSHOT_INCOMPLETE` on zero-count path (after WARNING) | Pass | `recorder.py:339–347` |
| Keep human-readable INFO/WARNING lines for operator tailing | Pass | `recorder.py:328–346` |
| New `lib/recorder_snapshot_check.sh` — classifier only, no side effects | Pass | `recorder_snapshot_check.sh:12–27` |
| Classifier rc 0/1/2 for OK / INCOMPLETE / timeout | Pass | `recorder_snapshot_check.sh:15–25` |
| Classifier checks INCOMPLETE before OK | Pass | `recorder_snapshot_check.sh:15–20` |
| Source classifier in `start_recorder.sh` | Pass | `start_recorder.sh:31–32` |
| Wait loop breaks on sentinels only (not human-readable INFO) | Pass | `start_recorder.sh:81–86` |
| Post-loop dispatch on classifier exit code with `set +e` | Pass | `start_recorder.sh:88–92` |
| Incomplete (rc=1): kill by pattern, pgrep wait, fail loud, no PID tail | Pass | `start_recorder.sh:95–104` |
| Success (rc=0): print PID tail | Pass | `start_recorder.sh:106–112` |
| Timeout (rc=2): exit non-zero (no success tail) | Pass | `start_recorder.sh:114–125` |
| Kill via `pkill -f "recorder --config $CONFIG"` not `$RECORDER_PID` | Pass | `recorder_stop.sh:37` |
| Runbook Step 4b + manual grep + checklist updated | Pass | `0029_RUNBOOK.md:270–274, 308–311, 614` |
| `RULES.md` sentinel contract note | Pass | `RULES.md:2023` |
| `test_start_recorder_check.py` — classifier fixtures (incomplete, success, timeout, race guard) | Pass | `test_start_recorder_check.py:42–142` |
| `test_initial_rest_snapshot.py` — sentinel assertions (OK, zero-count, auth failure) | Pass | `test_initial_rest_snapshot.py:358–477` |
| Do not source `start_recorder.sh` in pytest | Pass | Tests source libs only |

---

## Code Quality Review

### Correctness

The original bug class is fixed end-to-end:

1. **Race on zero-count path:** `_write_initial_rest_snapshot` logs the human-readable INFO line before the WARNING and sentinel. The wait loop breaks only on `RECORDER_SNAPSHOT_OK|RECORDER_SNAPSHOT_INCOMPLETE`, not on `Initial REST snapshot:`.
2. **Ambiguous grep:** `_classify_recorder_snapshot` uses terminal sentinels; INCOMPLETE is checked first so a log that somehow contained both markers would still fail loud.
3. **Soft timeout:** rc=2 exits non-zero; no `Recorder PID:` tail; recorder is stopped so a retrying caller does not race a hung process onto the shared DB.

The success-path grep pattern `Initial REST snapshot:` does **not** false-match `Initial REST snapshot incomplete:` (no colon after `snapshot` on the WARNING line). On the incomplete path the classifier never reaches the OK branch because the INCOMPLETE sentinel is present.

The sentinel contract docstring now scopes the “every exit path” requirement to account-configured runs and documents the no-account exception explicitly — consistent with the `start()` gate at `recorder.py:153–154`.

No snake_case / camelCase or nested-object alignment issues — this feature is log-string and shell-contract only.

### Over-engineering / file size

Focused diff. The shared `recorder_stop.sh` helper deduplicates three identical kill blocks and is justified by new pytest coverage (`TestStopRecorderPattern`). Classifier lib stays minimal (~28 lines). No unnecessary abstractions in Python.

### Style

Matches existing Phase 4 shell patterns: `set -euo pipefail`, feature-referenced header comments, `shellcheck source=` hints, `uv run` in launcher. Python sentinel docstring in `_write_initial_rest_snapshot` documents the shell contract clearly. Test module follows 0052/0054 conventions (plan-referenced docstrings, subprocess bash sourcing).

---

## Test Review

| Criterion | Assessment |
|---|---|
| Classifier incomplete (production line order) | Covered — `test_incomplete_production_order` |
| Classifier auth-only sentinel path | Covered — `test_auth_failure_only_sentinel` |
| Classifier success | Covered — `test_success` |
| Classifier timeout / no sentinel | Covered — `test_no_sentinel` |
| Race guard (INFO without sentinel → rc=2) | Covered — `test_info_without_sentinel_is_timeout` |
| Recorder emits OK sentinel on success | Covered — `test_sentinel_ok_on_success` |
| Recorder emits INCOMPLETE on zero counts | Covered — `test_sentinel_incomplete_on_zero_counts` |
| Recorder emits INCOMPLETE on auth failure (no count INFO) | Covered — `test_sentinel_incomplete_on_auth_client_failure` |
| Kill helper (clean stop, stuck process, pkill rc=1) | Covered — `TestStopRecorderPattern` (beyond plan, valuable) |
| Launcher sources both libs | Covered — `TestStartRecorderLauncherIntegration` |
| Classifier never prints `Recorder PID:` | Asserted in incomplete + success tests |
| Isolation / mocking | Temp log files + bash function stubs; no live pkill in tests |
| Speed | 19 tests in 0.25s |

**Gap (acceptable per plan):** Full `start_recorder.sh` integration (live recorder + real kill) is manual runbook verification only — explicitly out of pytest scope in the plan.

---

## Verification

```bash
uv run pytest apps/recorder/tests/test_start_recorder_check.py \
             apps/recorder/tests/test_initial_rest_snapshot.py \
             -q --tb=short
# 19 passed in 0.25s
```

---

## Post-merge operator validation

1. **Happy path:** Run `scripts/phase4/start_recorder.sh` with valid credentials → exit 0, log contains `RECORDER_SNAPSHOT_OK`, PID tail printed.
2. **Bad credentials:** Temporarily unset or invalidate API keys → exit 1 within ~15s, log contains `RECORDER_SNAPSHOT_INCOMPLETE`, no PID tail, recorder process gone (`pgrep -f "recorder --config"` empty).
3. **Manual grep:** `grep -aE "RECORDER_SNAPSHOT_OK|RECORDER_SNAPSHOT_INCOMPLETE" /tmp/recorder.log` matches runbook Step 4b.
