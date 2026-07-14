# Feature 0090 — Source tick_size from exchange instead of hand-maintained YAML

Plan: docs/features/0090_PLAN.md  |  Branch: feature/0090-exchange-tick-size

- [x] Task 1 / Phase 1: move InstrumentInfoProvider → bybit_adapter, require_live flag, backtest re-export + pyproject dep, gridcore from_bybit_response missing-key fix + tests
- [x] Task 2 / Phase 2: gridbot — optional YAML tick_size, fail-closed _fetch_instrument_info w/ retries, mismatch cross-check (raise), runner tick resolution fallback + tests
- [x] Task 3 / Phase 3: replay + backtest — optional YAML tick_size, provider-sourced tick w/ dynamic require_live, warn-and-use-YAML mismatch + tests
- [x] Task 4: conf YAMLs mark tick_size deprecated; RULES.md updates (lines 175, 1115)
- [x] Full suite green (post-fix: all packages 0 failures) + lint clean
- [x] Final whole-branch review — READY TO MERGE (0 Critical/Important); 2 test-hygiene nits folded in

## Not committed — awaiting user review/approval before any commit.
