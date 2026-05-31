# Feature 0061 Code Review

**Plan:** `docs/features/0061_PLAN.md`  
**Scope:** Fix three stale failures in `test_risk_limit_info.py` after `cache_lock` / `cache_validation` module split (issue #143)  
**Implementation:** applied to the test file on `main` (commit `Fix stale risk limit info tests`)  
**Review pass:** 1

---

## Verdict

**Approve.** The merged commit matches the plan’s test-only design: no production edits, correct import targets for `_IN_PROCESS_LOCKS`, and log assertions aligned with `CacheSizeExceededError`. All 94 tests in `test_risk_limit_info.py` and 13 in `test_cache_lock.py` pass. The optional `RULES.md` pitfall (plan Step 4) is included in this PR.

---

## Plan Implementation Check

| Step | Status | Evidence |
|------|--------|----------|
| **1** — Lock-registry tests import `cache_lock` | Pass | `test_risk_limit_info.py:742`, `760` — `import backtest.cache_lock as cache_lock_module` |
| **1** — Assert on `cache_lock_module._IN_PROCESS_LOCKS` | Pass | Lines 749–756, 766–776 |
| **1** — Registry key `str(cache_path.resolve())` | Pass | Matches `acquire_in_process_lock(self.cache_path)` where `cache_path` is already `.resolve()`'d (`risk_limit_info.py:195`, `cache_lock.py:35`) |
| **1** — Keep integration tests in `test_risk_limit_info.py` | Pass | Not moved to `test_cache_lock.py`; unit registry tests remain in `test_cache_lock.py:31-71` |
| **2** — Oversized load log assertion | Pass | `test_load_from_cache_oversized_logs_size_error` lines 172–175: `"Cache file size"` and `"exceeds"` in caplog |
| **2** — Still assert `result is None` | Pass | Line 171 |
| **3** — Verification | Pass | `uv run pytest apps/backtest/tests/test_risk_limit_info.py -q` → **94 passed**; `test_cache_lock.py` → **13 passed** |
| **4** — RULES.md pitfall (optional) | Pass | Pitfall #16 added in this PR (`RULES.md:2232`) |
| **Out of scope** — No prod changes | Pass | Diff touches only `test_risk_limit_info.py` in `92b12b3` |

---

## Findings

### N1 — Optional RULES.md pitfall (Resolved in this PR)

Plan Step 4 adds pitfall #16 under the backtest cache section. The text is correct and matches implementation, and it is committed in this PR alongside `0061_PLAN.md`, so docs and the pitfall note land together.

### N2 — Unrelated working-tree change (context note — not part of this PR)

At review time the working tree also carried an unrelated `conf/gridbot_test.yaml` change (SOLUSDT `grid_step`). It is **not** part of feature 0061 and was deliberately excluded from this PR; it is mentioned here only so a future reader knows it was a separate concern, not an omission.

### N3 — Incidental diff in fix commit (Note — not a defect)

`92b12b3` also removes an unused `original_dump = json.dump` binding in `test_temp_file_cleaned_up_on_write_failure` (`~1222`). Harmless cleanup; not mentioned in the plan but improves the test file.

No blocking or non-blocking test or production defects identified.

### Notes (not defects)

- **Log assertion style (load vs save).** Load path uses `"Cache file size"` + `"exceeds"`; save-path tests use `"exceeds"` + `"byte limit"` (`test_save_rejects_oversized_cache:425`, `test_custom_max_cache_size_enforced:622`). Both match the same `CacheSizeExceededError` string from `cache_validation.py:77-79`; the split is intentional per plan and avoids brittle numeric coupling.
- **Registry integration vs unit tests.** `test_cache_lock.py` exercises acquire/release directly; the two fixed tests still validate `RiskLimitProvider` + `weakref.finalize` + `close()` lifecycle — appropriate layering, no duplication concern.
- **GC sensitivity.** `test_lock_registry_released_when_instances_deleted` still depends on `gc.collect()` after `del provider_*` — pre-existing pattern; acceptable for integration coverage of finalizers.
- **Data alignment.** No API shape or naming mismatches; failure mode was purely wrong module symbol and outdated log substring.
- **Production correctness.** Confirmed: `_load_cache_entry` logs `str(CacheSizeExceededError)` (`risk_limit_info.py:338-339`); registry lives only in `cache_lock.py`.

---

## Unit Test Review

| Area | Assessment |
|------|------------|
| Oversized cache on load | Covered — warning substrings + `None` return |
| Lock ref-count with two providers | Covered — `test_lock_registry_released_when_instances_deleted` |
| `close()` + new provider ref-count | Covered — `test_close_then_new_provider_keeps_lock_registry_consistent` (incl. closed-provider `RuntimeError`, concurrent writes) |
| Happy paths / other edge cases | Unchanged — 91 other tests in file still pass |
| Isolation | Good — `tmp_path`, no network; imports private registry only for integration assertions (same pattern as `test_cache_lock.py`) |
| Naming / style | Matches existing `TestConcurrentCacheAccess` and caplog patterns |
| Speed | Full file ~0.47s — no new heavy setup |

No new tests required: this issue restores coverage of existing behavior after refactor, not new functionality.

---

## Commands Run

```bash
uv run pytest apps/backtest/tests/test_risk_limit_info.py -q
# 94 passed in 0.47s

uv run pytest apps/backtest/tests/test_cache_lock.py -q
# 13 passed in 0.01s
```

---

## Recommendation

- **Merge status:** the fix is already applied to the test file on `main`; issue #143 is resolved on branches that include it.
- **Follow-up:** this PR commits `docs/features/0061_PLAN.md`, `docs/features/0061_REVIEW.md`, and `RULES.md` pitfall #16. The unrelated `conf/gridbot_test.yaml` change is intentionally not included.
