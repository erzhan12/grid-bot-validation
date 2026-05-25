# 0051 Code Review: `last_cross` fill mode

**Reviewer:** automated review (2026-05-25, pass 2)  
**Plan:** `docs/features/0051_PLAN.md`  
**Scope:** `fill_simulator.py`, `order_manager.py`, replay config, tests, `README.md`, `RULES.md`, runbook

**Reviewed at:** `feature/0051-last-cross-fill-mode` working tree (uncommitted).  
**HEAD:** `53769bb` (`feat(0049): surgical recorder wipe for shared Phase 4 DB (#114)`)

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

Pass-1 items are resolved:

| Pass-1 ID | Resolution |
|---|---|
| N1 — README fill-mode list | Fixed — `README.md:53` now lists ``last_cross`` |
| N2 — Class docstring on `advance_market` | Fixed — `fill_simulator.py:73-77` documents unconditional call + deferred mode-gating |
| N3 — Mixed-input warm-state test | Fixed — `test_bare_decimal_does_not_mutate_warm_state` at `test_fill_simulator.py:592-612` |

### Note — Strict inequality at limit (intentional, per plan)

Tests #13 and #14 pin `prev_last == limit_price` as **not** fillable. This matches issue #117 and the plan's "Open behavior choice." Operators evaluating v7 match-rate should be aware: at-limit sticky prints that cross on the same snapshot as `prev_last == limit` will not fire until a later transition. Flipping to non-strict (`>=` / `<=` on `prev_last`) is a one-line change per side if replay data suggests it.

---

## Verdict

**Approve.** The implementation matches the plan. All pass-1 findings are addressed. Targeted tests pass (106/106).

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---:|---|
| Add `FillMode.LAST_CROSS = "last_cross"` | Pass | `fill_simulator.py:23` |
| Two-slot state + idempotency token on simulator | Pass | `_prev_last_price`, `_tick_prev_last`, `_tick_token` at `fill_simulator.py:91-93`; stash-before-commit in `advance_market` at `:145-147` |
| Extend `MarketSnapshot` with optional `symbol` | Pass | `fill_simulator.py:41`; populated in `_to_snapshot` at `:151-157` |
| Implement `_should_fill_last_cross` (strict inequality, read-only) | Pass | `fill_simulator.py:254-281` |
| `advance_market` hook in `check_fills` (TickerEvent branch only, before order loop) | Pass | `order_manager.py:274-281` |
| Legacy bare-`Decimal` path skips `advance_market` | Pass | `order_manager.py:284-290` else-branch has no call |
| Extend replay `FillSimulatorConfig.mode` Literal + description | Pass | `config.py:165-183`; default remains `book_touch` |
| No engine/main changes required | Pass | enum pipeline unchanged |
| Matrix test `LAST_CROSS: False` rows | Pass | `test_fill_simulator.py:230-311` |
| Config round-trip for `"last_cross"` | Pass | `test_config.py:94` |
| Dedicated tests #1–#15 | Pass | `TestLastCrossFillMode` + `TestLastCrossOrderManagerIntegration` |
| Mixed-input bare-`Decimal` no-mutation contract | Pass | `test_legacy_bare_decimal_returns_false` (#11) + `test_bare_decimal_does_not_mutate_warm_state` |
| `_ticker` / `_ticker_for` `tick_index` discipline | Pass | `test_fill_simulator.py:34-82` |
| `RULES.md` fill-mode + pitfall docs | Pass | `RULES.md:1693-1695` |
| Runbook / config docs sweep | Pass | Runbook comment at `0029_RUNBOOK.md:442-443`; README at `:53` |
| Do not change replay default | Pass | `FillSimulatorConfig` default `"book_touch"` |
| Mode-gating optimization | Deferred | Per plan — acceptable |

---

## Code Quality Review

### Correctness

The two-slot staging model is implemented correctly:

1. `advance_market` stashes the committed prior tick into `_tick_prev_last` **before** overwriting `_prev_last_price`.
2. `_should_fill_last_cross` reads only `_tick_prev_last`, never the committed slot.
3. Token equality on `(symbol, exchange_ts, local_ts)` prevents N× advance when `check_fill` runs per order.
4. Invalid ticks (`curr_last <= 0`, `symbol is None`) are no-ops for state — verified by test #10.
5. Side validation runs before the `prev_last is None` short-circuit (`test_invalid_side_raises_in_cold_state`), matching `book_touch` behavior.

Production replay path is wired: `runner.py` and replay engine both call `order_manager.check_fills(market=event)` with `TickerEvent`, which triggers `advance_market` before the per-order loop.

No snake_case / camelCase or nested-object alignment issues were found — all paths use internal `TickerEvent` / `Decimal` types consistently.

### Over-engineering / file size

Scope is proportional. `fill_simulator.py` gains ~91 lines; tests are thorough but focused. The `_drive_two_ticks` helper and duplicated `_ticker` in `test_order_manager.py` follow existing test patterns. No refactor needed.

### Style

Matches surrounding code: `StrEnum` dispatch via `match`, `SideType` comparisons, frozen dataclasses, concise docstrings. Class and `_should_fill` docstrings now document all four modes including `last_cross`.

---

## Test Review

| Criterion | Assessment |
|---|---|
| Happy paths (BUY/SELL cross, gap-up/down) | Covered (#3–#6) |
| Edge cases (no prior tick, sticky price, zero last, per-symbol isolation, strict-at-limit) | Covered (#1, #2, #9, #10, #13, #14) |
| Order-manager integration (multi-order, idempotency, orderless ticks) | Covered (#8, #8b, #15) |
| Legacy bare-`Decimal` contract (cold + warm) | Covered (#11 + `test_bare_decimal_does_not_mutate_warm_state`) |
| Invalid side | Covered (#12 + cold-state variant) |
| Config plumbing | Covered (`test_fill_simulator_explicit_modes`) |
| Isolation / mocking | Pure unit tests; no external I/O — appropriate |
| Naming / patterns | Clear `test_*` names, `# Test #N` comments map to plan |
| Speed | 106 tests in 0.04s |

`test_non_positive_last_price_never_fills` parametrizes over `list(FillMode)` and therefore includes `LAST_CROSS` automatically — zero `last_price` never fills.

---

## Verification

```bash
uv run pytest apps/backtest/tests/test_fill_simulator.py \
             apps/backtest/tests/test_order_manager.py \
             apps/replay/tests/test_config.py -q --tb=short
# 106 passed in 0.04s
```

---

## Notes

- v7 dataset re-validation (`mode: last_cross`, ≥95% match-rate) is operator-driven per plan and not part of this code review.
- `advance_market` mode-gating (skip dict writes when not `LAST_CROSS`) remains a deferred optimization; no functional impact today.
- Commit the working-tree changes (including this review file and `0051_PLAN.md` if desired) before opening the PR.
