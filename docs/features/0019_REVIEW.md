# 0019 Review

Scope reviewed: implementation of `docs/features/0019_PLAN.md` in
`apps/gridbot/src/gridbot/orchestrator.py`,
`apps/gridbot/src/gridbot/position_fetcher.py`,
`apps/gridbot/tests/test_orchestrator.py`, and
`apps/gridbot/tests/test_position_fetcher.py`, with focus on plan
fidelity, runtime safety, data-shape assumptions, style consistency, and
unit-test quality.

## Findings

### [P2] Backward-compatible `StartupTimeoutError` import path from `gridbot.orchestrator` was dropped

- The updated plan explicitly requires re-importing
  `StartupTimeoutError` in orchestrator to preserve the historical import
  path (`from gridbot.orchestrator import StartupTimeoutError`), while the
  class itself moves to `position_fetcher.py`
  (`docs/features/0019_PLAN.md`, "Files to change" / Imports section).
- Current orchestrator imports only:
  `from gridbot.position_fetcher import PositionFetcher, _POSITION_TICK_BASE`
  (`apps/gridbot/src/gridbot/orchestrator.py:39`).
- Compatibility path is now broken:
  `ImportError: cannot import name 'StartupTimeoutError' from 'gridbot.orchestrator'`.
  (Verified via project venv Python import probe.)
- Tests do not catch this because they now import
  `StartupTimeoutError` directly from `gridbot.position_fetcher`
  (`apps/gridbot/tests/test_orchestrator.py`, startup hard-cap test).

Impact: this is a behavior/compatibility regression vs plan intent and can
break external/internal code still importing `StartupTimeoutError` from
`gridbot.orchestrator`.

## Plan Fidelity

Aside from the compatibility export above, the extraction is implemented as planned:

- `PositionFetcher` exists in a new module with injected dependencies,
  moved fetch/cache state, moved constants, and moved startup/rotation/fetch
  helpers.
- `Orchestrator` now routes startup and periodic position checks via
  `self._position_fetcher.fetch_and_update(...)`.
- WS position callback is wired to
  `self._position_fetcher.on_position_message(...)`.
- Position-fetch fields/methods were removed from orchestrator and test
  access was updated to `_position_fetcher`.
- Tests were updated to preserve injected-dict identity (`.clear()` instead
  of rebinding) and patch moved monotonic calls in
  `gridbot.position_fetcher`.
- Isolated `test_position_fetcher.py` exists and is comprehensive (now 20 tests).

## Verification

Executed checks:

- `uv run pytest apps/gridbot/tests/test_orchestrator.py -q`
  - Result: `92 passed`
- `uv run pytest apps/gridbot/tests/test_position_fetcher.py -q`
  - Result: `20 passed`
- `uv run pytest apps/gridbot/tests -q`
  - Result: `325 passed`
- `git diff --name-only -- packages/gridcore apps/backtest apps/comparator apps/gridbot/src/gridbot/executor.py apps/event_saver`
  - Result: empty (no cross-boundary edits)
- `uv run ruff check apps/gridbot/src/gridbot/orchestrator.py apps/gridbot/src/gridbot/position_fetcher.py apps/gridbot/tests/test_orchestrator.py apps/gridbot/tests/test_position_fetcher.py`
  - Result: 2 F401 warnings (`ExecutionEvent`, `OrderUpdateEvent`) in
    `orchestrator.py`; these are pre-existing and unrelated to 0019.
- Compatibility probe:
  `./.venv/bin/python` import test for
  `from gridbot.orchestrator import StartupTimeoutError`
  - Result: `ImportError` (fails today).
