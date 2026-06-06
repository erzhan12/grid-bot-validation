# 0068 Code Review — `qty_zero` ERROR category + 110017/110072 event_coverage

**Date:** 2026-06-06  
**Plan:** `docs/features/0068_PLAN.md`  
**Scope reviewed:** `.claude/skills/gridbot-health/analyze.py`, `.claude/skills/gridbot-health/SKILL.md`, `.claude/skills/gridbot-health/test_analyze.py`

## Findings

No blocking or non-blocking findings.

## Plan Compliance

- **Change C implemented:** `_categorize_error` adds `qty_zero` after `grid_state_writer` and before the final `other` fallback, matching either `"110017"` or `"orderQty will be truncated to zero"`.
- **No `dup_link_id` category added:** 110072 still returns `other` from `_categorize_error`, as required.
- **Closed event key set updated:** `_EVENT_COVERAGE_KEYS` includes bare `qty_zero` and `dup_link_id` after `error_other` and before `ws_disconnect`.
- **Table 13 labels present:** `_EVENT_COVERAGE_LABELS` has entries for both new keys, avoiding render-time `KeyError`.
- **Event coverage behavior matches the plan:** `qty_zero` is populated through ERROR category mapping and is carved out of `error_other`; `dup_link_id` is populated by a level-agnostic line scan and intentionally overlaps `error_other` for ERROR-level 110072 lines.
- **Docs updated:** `SKILL.md` Table 1 and Table 13 descriptions include `qty_zero`, `dup_link_id`, the bare-key naming note, and the intentional `dup_link_id`/`error_other` overlap.

## Test Review

Feature 0068 coverage is present and aligned with the acceptance criteria:

| Plan item | Test | Verdict |
|-----------|------|---------|
| `_categorize_error` classification | `test_categorize_error_qty_zero` | Covers production-shaped 110017, bare phrase, legacy buckets, and 110072 staying `other` |
| Synthetic replay criterion | `test_synthetic_window_qty_zero_270_not_other` | Pins exact `qty_zero == 270` and confirms those lines do not inflate `other` |
| Event coverage population | `test_build_event_coverage_qty_zero_and_dup_link_id` | Covers `qty_zero`, `dup_link_id`, `qty_zero` carve-out, and ERROR-level 110072 dual-population |
| Closed key/label guard | `test_event_coverage_new_keys_registered_with_labels` | Guards key registration and Table 13 labels |
| Additive merge | `test_merge_event_coverage_new_keys_additive` | Pins count accumulation plus `first_seen`/`last_seen` behavior for both new keys |
| WARNING-level 110072 edge | `test_build_event_coverage_dup_link_id_warning_level_not_error_other` | Confirms level-agnostic `dup_link_id` scan without `error_other` pollution for non-ERROR logs |

The tests are isolated, fast, and follow the existing helper style (`mk`, `_ts`, synthetic `LogLine` lists). No external dependency mocking is needed for this pure classification and aggregation logic.

## Data Alignment

- The intentionally bare `qty_zero` / `dup_link_id` event keys are documented, so the mixed `error_*` + bare-key Table 13 shape is explicit.
- `qty_zero` is ERROR-only because it flows through `_categorize_error`; `dup_link_id` is level-agnostic because it mirrors the existing `err_dupe` counter. This aligns Table 1 and Table 13 behavior.
- The production-shaped fixtures match the expected executor and retry-queue log shapes, including the subtle ERROR vs WARNING distinction for 110072.

## Style / Scope

The change is localized to the skill implementation, skill docs, and tests. No over-engineering, unrelated refactor, or file-size-driven refactor need was found.

## Verification

```bash
uv run pytest .claude/skills/gridbot-health/test_analyze.py
# 49 passed in 0.04s
```
