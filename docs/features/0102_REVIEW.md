# Feature 0102 — Review Trail

Account-wide liquidation halt for shared-wallet multi-strategy replay (issue #246, resolves 0094 O1).
Plan: `docs/features/0102_PLAN.md` (authored + debated over 4 rounds via codex-plan-debate-ext).

## Local staged review (review-fix-loop-staged)

5 parallel category subagents over the staged diff.

- **Correctness** — no issues. Mutation-tested: removing the pre-fill `refresh_balances` fails `test_mtm_only_breach_halts_before_process_fills`; removing the post-fill observer fails `test_fill_driven_breach_halts_at_post_fill_sample`. All debated design points verified against code.
- **Code quality** — 1 INFO (the runner-iteration `price <= 0` skip is now duplicated across `total_unrealized`/`total_im_mm`/`total_position_value`; plan mandates mirroring the skip, 3 lines each). Non-blocking, left as-is.
- **CRITICAL (fixed)** — the plan's Phase-3 `event_follower` two-symbol breach *integration* test was missing; existing halt tests used a stub `_LoopRunner` + MagicMock engine, so the production reactive-placement path (`runner.py:522-539`) and the reduce-only-survives-`_should_place_close` guarantee were never exercised end-to-end. **Fix:** added `TestAccountHaltEventFollowerIntegration::test_breach_halts_idle_engine_blocks_reactive_opens_and_keeps_closes` — real `MultiReplayEngine.run` in `EVENT_FOLLOWER` mode, two real `GridEngine`s, organic breach (seeded long size=20 @100, mark 100→1, balance=100), recorded `PrivateExecution` driving the real synthetic-ticker reactive dispatch. Asserts: both engines halted incl idle LTC; all post-halt `execute_place` are reduce-only (call-order, non-vacuous); a reduce-only close succeeds at `fill_ts` (proves the reactive path, not `execute_tick`).
- **WARNING (fixed)** — no-breach e2e now spies `execute_place` to assert non-reduce-only opens still execute (an always-on freeze would fail), and asserts `min_account_pool != Decimal("Infinity")` (was vacuously satisfied by the sentinel).

Production code was NOT changed by the CRITICAL/WARNING fixes (test-only).

## External review (ext-code-review — codex gpt-5.6-sol + cursor)

Iteration 1 — **both engines: NO P1/P2 FINDINGS.** Cursor's verification log MATCH on every load-bearing claim (dual observe, latch gating, min-before-return invariant, gross `total_position_value`, `on_event` reduce-only-exempt filter on both paths, `Decimal("0")` call site, result fields + Infinity sentinel).

P3s triaged:
- ACCEPTED (fixed, trivially cheap + clearly right): unused `_pre_total_im` → `_` unpack; `observe_account_risk` bare-`0` compares → `Decimal("0")` (spec surface); stale plan Context line 13 still showing `refresh_balances(coordinator.total_unrealized())` → `Decimal("0")`.
- ACCEPTED non-blocking gaps (not fixed — optional test strengthenings / cosmetic): pin exact no-breach `min_account_pool`; `pool == 0` boundary latch case; all-flat `Infinity` through `run()` vs helper; assert `min_account_pool` in the event-follower e2e; a dedicated top-of-tick sizing-parity spy (AC3 invariant already code-verified in local correctness pass); new gridcore halt test uses double quotes vs the file's single (ruff-check does not enforce quote style).

## Verification (uv monorepo)

- `uv run pytest apps/replay/tests -q` → 195 passed
- `uv run pytest packages/gridcore/tests/test_engine.py -q` → 75 passed
- `uv run pytest apps/live_check/tests/test_shared_wallet.py -q` → 7 passed
- `make test` → all packages green, TOTAL coverage 91% (≥88 gate)
- `make lint` → clean
