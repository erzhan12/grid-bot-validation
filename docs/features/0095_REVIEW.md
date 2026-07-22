# 0095 — External code review trail

Feature: shared-wallet reconcile — futures-only equity basis (plan `0095_PLAN.md`).
Reviewed: staged implementation on branch `feature/0095-futures-equity-reconcile`.
Engines: OpenAI codex + Cursor agent (both, read-only). Triage + fixes: main session.

## Plan pipeline (before code)

plan-debate (3 iters, SUCCESS — 3 findings incl. the `coin[]`-list misread and the mm-rate
numerator-comparability precondition) → ext-plan-review (codex+cursor, 3 iters, SUCCESS —
8 findings, 0 rejected, incl. cursor's correction of the diagnosis to an **unrealized
double-count** (not spot) and the margin-basis false-FAIL ($5 vs $305)).

## Code review

**Baseline:** `make lint` clean; `uv run pytest apps/replay apps/live_check` = 261 passed;
the real acceptance gate `live-check --shared` vs run 580ca395 PASSES (max Δequity +0.54
< 1.00, final +0.036 — the ~$8.5 systematic offset collapsed, verified against the recorder DB).

**Round 1 — both engines NO P1/P2 FINDINGS.** cursor produced a 14-point verification log,
all MATCH (seed from coin_balance, futures-equity parse never falls back to the account
column, NULL-safe, total_margin_balance=futures_equity, Fallback gates on equity only,
Decimal throughout, tests present). P3s:
- codex/cursor: no test for malformed JSON string / non-numeric `unrealisedPnl`. **ACCEPTED**
  → added `test_load_wallet_curve_skips_malformed_raw_json_without_crash`. **This test
  surfaced a latent bug:** `Decimal(str("NaN"))` parses to `Decimal('NaN')` WITHOUT raising
  (NaN/Infinity are valid Decimal specials), so `wallet_balance + NaN = NaN` flowed through
  the try/except and poisoned the reconcile max-diff. **Fixed** `_futures_equity` /
  `_futures_mm_rate` to reject non-finite results (`result.is_finite() else None`).
- codex: stale `total_margin_balance` / `account_mm_rate` threshold descriptions in
  `config.py`. **ACCEPTED** → rewrote to note the 0095 futures-basis / informational change.
- cursor: Fallback INFO hardcoded in render vs verdict.passed (policy split); `_futures_equity`
  /`_futures_mm_rate` `row` untyped. **P3, accepted non-blocking gaps** (churn > value).

Re-verified after fixes: `uv run pytest apps/replay apps/live_check` = **262 passed**;
`make lint` clean.

## Trace

- engines: both (codex + cursor)
- iterations: 1
- findings: raised 5 (all P3), accepted 2 (fixed), rejected 0, P3-ignored 2; the accepted
  malformed-json test surfaced + fixed a real latent NaN-propagation bug
- verification: 262 passed, lint clean
- result: SUCCESS
