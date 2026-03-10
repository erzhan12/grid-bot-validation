# 0014 Review - `--save-events` Flag for Gridbot Live Data Capture (Re-review)

Plan: `docs/features/0014_PLAN.md`  
Rubric: `commands/code_review.md`

## Findings (ordered by severity)

### [P2] Multi-strategy accounts still cannot persist executions/orders

In `_start_event_saver()`, accounts with more than one strategy are forced to `run_id=None` (`apps/gridbot/src/gridbot/orchestrator.py:803-813`).  
For EventSaver, `run_id=None` means executions/orders are not persisted (`apps/event_saver/src/event_saver/main.py:98-102`, `apps/event_saver/src/event_saver/writers/execution_writer.py:186`, `apps/event_saver/src/event_saver/writers/order_writer.py:102-108`).

Impact: for multi-strategy-per-account setups, replay/comparison data is still incomplete for private execution/order streams.

## Resolved From Previous Review

- `_create_run_records()` is now implemented and populates `_run_ids`; `_update_run_records_stopped()` now marks runs completed (`apps/gridbot/src/gridbot/orchestrator.py:833-931`).
- EventSaver startup ordering now matches the plan (started before gridbot WebSocket connect) (`apps/gridbot/src/gridbot/orchestrator.py:170-179`).
- Empty-account guard and no-strategy account guard are present in EventSaver setup (`apps/gridbot/src/gridbot/orchestrator.py:759-790`).
- CLI tests updated for new `main(..., save_events=...)` signature; `--save-events` CLI test added (`apps/gridbot/tests/test_main.py:222-276`).
- EventSaver integration tests expanded for config/context wiring and run-id behavior (`apps/gridbot/tests/test_orchestrator.py:1371-1734`).
- Prior `ruff` unused-import findings in touched files are fixed.

## Plan Implementation Coverage

Implemented:
- `enable_event_saver` config field.
- `--save-events` CLI plumbing and config override.
- Embedded EventSaver lifecycle (`start`/`stop`) in orchestrator.
- Run record creation/update logic to support `run_id` linkage.
- Substantially expanded tests for lifecycle, data wiring, and DB behavior.

Remaining gap:
- Multi-strategy-account `run_id` semantics are still unresolved, so execution/order persistence is intentionally disabled in that case.

## Test/Lint Evidence

- `uv run pytest apps/gridbot/tests -q` -> `209 passed`.
- `uv run ruff check apps/gridbot/src/gridbot/config.py apps/gridbot/src/gridbot/main.py apps/gridbot/src/gridbot/orchestrator.py apps/gridbot/tests/test_main.py apps/gridbot/tests/test_orchestrator.py` -> `All checks passed!`
