# Feature 0058 Code Review

**Plan:** `docs/features/0058_PLAN.md`  
**Scope:** `packages/gridcore/src/gridcore/position.py`, `apps/gridbot/src/gridbot/runner.py`, and their unit tests  
**Review pass:** 2 (post-fix)

---

## Verdict

**Approve.** The implementation matches the updated plan. Pass-1 semantic finding is resolved; no blocking or non-blocking defects remain.

---

## Resolved From Prior Review

| Pass-1 ID | Resolution |
|---|---|
| P3 — `realized_usdt` mapped to `cumRealisedPnl` instead of UI “Realized” | Fixed — `realized_usdt` now logs `cur_realized_pnl` (`curRealisedPnl`); lifetime value is separate as `cum_realized_usdt` from `cum_realized_pnl`. Distinct test values (5.50 vs 123.45) catch a cur/cum swap. |

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---|---|
| Add `cum_realized_pnl` and `cur_realized_pnl` to `PositionState` with 0056 semantics comments | Pass | `position.py:48-54` |
| Extend DEBUG log with `upnl_usdt`, `realized_usdt` (cur), `cum_realized_usdt`, `pos_value_usdt` after `unrealized_pnl=%` | Pass | `position.py:278-288` |
| `realized_usdt` sourced from `cur_realized_pnl`; `cum_realized_usdt` from `cum_realized_pnl` | Pass | `position.py:286-287` |
| Keep existing log fields unchanged (name, order, formatting) | Pass | `margin`, `liq_ratio`, `unrealized_pnl=%`, `multiplier`, `position_ratio`, `total_margin` unchanged |
| Do **not** change `unrealized_pnl_pct` computation or risk rules | Pass | pct logic at `position.py:224-226` untouched |
| Parse `cumRealisedPnl` and `curRealisedPnl` in live `_build_position_state` with `(x, 0) or 0` idiom | Pass | `runner.py:842-843`, `859-860` |
| Comment on cum (lifetime) vs cur (UI) semantics | Pass | `runner.py:839-841`, `position.py:48-53` |
| caplog test: all four USDT fields; cur/cum distinct; negative upnl sign | Pass | `test_position.py:166-247` |
| `_build_position_state` parse test: positive, negative, absent, empty string for both fields | Pass | `test_runner.py:887-915` |
| Backtest keeps default `cum_realized_pnl` / `cur_realized_pnl` = 0 (non-breaking) | Pass | `backtest/runner.py:739-747` omits fields; defaults apply |
| Out of scope items not implemented | Pass | No gridbot-health edits, no backtest log parity |

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

### Notes (not defects)

- **Explicit `None` values untested.** Runner tests cover absent keys and empty strings for both `cumRealisedPnl` and `curRealisedPnl`. An explicit `None` in the dict would also default to zero via the existing `(x, 0) or 0` idiom (same as `unrealisedPnl`). Optional one-liner test; not required for merge.
- **`pos_value_usdt` uses computed notional.** Sourced from `calc_position_value(size, avgPrice)`, not Bybit’s `positionValue` REST field. Pre-existing behavior; plan defers `avgPrice` divergence investigation.
- **Naming avoids comparator collision.** `PositionState.cum_realized_pnl` / `cur_realized_pnl` are distinct from the per-trade `realized_pnl` symbols in comparator/backtest, as noted in the plan.

---

## Code Quality Review

### Correctness & data alignment

- **camelCase:** Bybit keys `cumRealisedPnl` and `curRealisedPnl` read correctly; aligns with recorder/event-saver parsing at `recorder.py:521-530`.
- **Semantic mapping:** `realized_usdt` → UI “Realized”; `cum_realized_usdt` → lifetime cumulative. Comments in dataclass, runner, and caplog test docstring are consistent with `0056_PLAN.md`.
- **Sign preservation:** Negative `unrealisedPnl` and negative realized values tested.
- **Flat positions:** `size == 0` → `_build_position_state` returns `None`; no per-tick log (acceptable per plan).

### Over-engineering / style

- Diff remains focused: 4 files, +150 lines, no new abstractions.
- Test structure matches existing caplog and `_build_position_state` patterns.
- No file-size or refactoring concerns.

### Unit tests

| Area | Assessment |
|---|---|
| Happy path | Covered — four USDT log fields; both realized fields parsed with distinct values |
| Edge cases | Covered — absent keys, empty strings, negative signs |
| cur/cum swap guard | Covered — `realized_usdt=5.5000` vs `cum_realized_usdt=123.4500` in caplog test |
| Isolation | Good — no exchange I/O |
| Regression | `251` tests in `test_position.py` + `test_runner.py` pass; backtest `build_position_state` tests pass |

---

## Commands Run

```bash
uv run pytest packages/gridcore/tests/test_position.py apps/gridbot/tests/test_runner.py -q
```

Result: `251 passed in 0.23s`

```bash
uv run pytest packages/gridcore/tests/ apps/backtest/tests/test_runner.py -q -k "build_position_state"
```

Result: `4 passed`

```bash
uv run ruff check packages/gridcore/src/gridcore/position.py apps/gridbot/src/gridbot/runner.py \
  packages/gridcore/tests/test_position.py apps/gridbot/tests/test_runner.py
```

Result: `All checks passed!`
