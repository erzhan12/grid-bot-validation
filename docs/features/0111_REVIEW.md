# 0111 review trail

## PR-a — `feature/0111-leg-side-move` (pure move of `leg_side` to `bybit_adapter.position_utils`)

### Local staged review (2026-10-04)
Five category passes (quality, security, performance, testing, documentation): no findings. Function body identical to the HEAD version; both import paths (`bybit_adapter.leg_side`, `event_saver.writers.leg_side`) resolve to the same object; `position_utils.py` has no imports, so no circular-import edge; recorder consumer covered by the existing `test_flat_hedge_leg_with_empty_side_keeps_its_row` / `test_unresolved_flat_row_is_logged_at_info`.

### External review trail (2026-10-04)
Engines: codex `gpt-6-astra` (high, read-only), cursor `agent --mode ask`. One iteration.

- codex: `NO P1/P2 FINDINGS`, no P3s.
- cursor: verification log (scope, moved body vs HEAD, camelCase row keys after envelope unwrap at `position_writer.py:208-228` / `rest_client.py:426`, re-export + dependency direction, test coverage) then `NO P1/P2 FINDINGS` and two P3s:
  1. `test_position_utils.py` never passes `side: None` — **accepted**, added `test_null_side_resolves_from_position_idx` and a `{"side": None}` case to the missing-fields test.
  2. Module docstring and `.claude/rules/bybit-adapter.md` named gridbot as a current caller while PR-a adds no gridbot import — **accepted**, reworded both to say gridbot adopts it in PR-b.

Rejected: none.

Verification after fixes: `uv run pytest packages/bybit_adapter/tests apps/event_saver/tests apps/recorder/tests` 720 passed; `make lint` clean; `make test` (pre-P3-fix run) exit 0, total coverage 92%.
