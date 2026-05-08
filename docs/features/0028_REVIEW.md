# 0028_REVIEW — bbu2 alignment audit implementation

## Findings

### P1 — Missing audit report/sign-off artifact

`docs/features/0028_PLAN.md` makes `docs/features/0028_AUDIT_REPORT.md` the sign-off deliverable, including the full audit matrix, carve-outs, evidence, and residual-risk statement. The implementation currently has only `0028_PLAN.md`; there is no `0028_AUDIT_REPORT.md`. That means the key decisions made in code — especially removing legacy `b...` amount mode and deferring `long_koef` — are not tied to an audit row with category, severity, decision impact, and evidence. Without that report, the implementation cannot satisfy Phase 6 even if the code changes are correct.

Evidence: `docs/features/0028_PLAN.md:331-344`, `docs/features/0028_PLAN.md:455-458`; current workspace has only `docs/features/0028_PLAN.md` for 0028.

### P1 — `long_koef` divergence is deferred instead of resolved or classified

The plan explicitly calls `long_koef` application the second priority target and says silent dropping would be a Behavioral Bug disguised as a config match. The implementation records this as a TODO but does not add a divergence row/report or implement the bbu2 behavior. A user setting `long_koef != 1.0` still gets no effect in qty computation, while bbu2 multiplies amount under `1.1 < position_ratio < 10` and both liquidation prices equal zero. This violates the plan's sign-off rule that unresolved behavioral divergences must not remain hidden.

Evidence: `docs/features/0028_PLAN.md:433-438`, `TODO.md:13`; `apps/gridbot/src/gridbot/runner.py:_resolve_qty` still only applies `amount_multiplier`.

### P2 — `b...` mode removal is documented in RULES, but not reconciled with the plan's oracle requirement

The implementation removes legacy `b...` amount mode from `qty.py` and marks it as a RULES carve-out for inverse/non-USDT contracts. That may be a reasonable product decision, but the active 0028 plan still requires the qty oracle to enumerate `b...` and assert output matches bbu2 `__get_amount`. The final audit artifact should reclassify this as an `Intentional Improvement` / `Architecture Delta` with evidence and update the plan/report accordingly; otherwise future reviewers will see a direct mismatch between the plan and the implementation.

Evidence: `docs/features/0028_PLAN.md:274-277`, `packages/gridcore/src/gridcore/qty.py:87-95`, `RULES.md:136-149`.

## Verification

- `uv run pytest -q packages/gridcore/tests/test_qty.py apps/gridbot/tests/test_runner.py` → 134 passed.
- `uv run ruff check packages/gridcore/src/gridcore/qty.py packages/gridcore/tests/test_qty.py apps/gridbot/src/gridbot/config.py apps/backtest/src/backtest/config.py apps/replay/src/replay/config.py apps/gridbot/tests/test_runner.py` → passed.

