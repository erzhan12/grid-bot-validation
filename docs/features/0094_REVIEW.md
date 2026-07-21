# 0094 — External code review trail

Feature: shared-wallet multi-strategy replay + validation (plan `0094_PLAN.md`).
Reviewed: staged implementation on branch `feature/0094-shared-wallet-replay`.
Engines: OpenAI codex + Cursor agent (both, read-only). Triage + fixes: main session.

## Round 1

**codex** — 1 P1, 2 P3:
- P1 `multi_engine.py:721` `total_margin_balance` emitted as `total_equity`, `account_mm_rate` divided by it → "mm-rate gate systematically wrong". **REJECTED.** `reconcile_wallet_curve` (`shared_wallet.py:111-118`) diffs replayed values against the REAL recorded `point.total_margin_balance` / `point.account_mm_rate` fields, not `total_equity` — the documented cross-margin-UTA assumption (plan New-logic f: `marginBalance == total_equity`) is SELF-CHECKING; divergence surfaces as a margin/mm-rate delta the gate flags. Loose initial tolerances by design (O1). Not a silent bug.
- P3 signed `final_equity_delta` vs plan `|Δ|`. **ACCEPTED** → `abs()`.
- P3 test asserts `total_margin_balance == total_equity`. Covered by the reject rationale + new e2e test.

**cursor** — verification log (all C1–C7 / New-logic MATCH), 3 P2 + 5 P3:
- P2 startup mark-cache leaves `Decimal("0")` silently when no at-or-before mark. **ACCEPTED** → `logger.warning` in both branches of `_startup_mark_cache`.
- P2 missing e2e `MultiReplayEngine.run()` test (coupling + `account_curve` vs `equity_curve`). **ACCEPTED** → added `TestMultiReplayRunEndToEnd`.
- P2 missing C2 in-method `execute_place` interception regression. **PARTIAL** — the e2e run() test exercises the wired `coordinator.active` interception loop; the unit `test_refresh_balances_intercepts_sum_unrealized` covers the mechanism. A dedicated wallet_balance-capture assertion recorded as an accepted non-blocking gap.
- P3 shared gates `<=` vs per-strat strict `<`. **ACCEPTED** → strict `<`.
- P3 (×4) C5 mask-scenario test, C3 sample distinctness, 800-line module smell, monkey-patch style. **Accepted non-blocking gaps.**

Fixes verified: `uv run pytest apps/replay apps/live_check` = 251 passed; `make lint` clean.

## Round 2 (convergence check)

Both engines: **NO P1/P2 FINDINGS.** cursor re-verified all round-1 fixes MATCH and confirmed the rejected P1 is self-checking.

Shared P3 (both engines): no unique-`symbol` validator — duplicate `strategies[*].symbol` silently last-wins in the engine's per-symbol maps. **ACCEPTED** (cheap, real footgun) → `reject_duplicate_strategies` validator on `MultiReplayConfig` (symbol + strat_id) + 2 tests.
Other P3s (ts-normalize both sides in `reconcile_wallet_curve` — prod path is SQLite-naive/safe; `final_equity_delta` not separately gated — `max_equity_delta` over the window already includes the final point): accepted non-blocking gaps.

Final: `uv run pytest apps/replay apps/live_check` = 253 passed; `make lint` clean.

## Trace

- iterations: 2
- findings: raised 8+8, accepted 6 (fixed 6), rejected 1 (self-checking margin assumption), P3-ignored several
- result: SUCCESS (zero valid P1/P2, tests+lint green)
