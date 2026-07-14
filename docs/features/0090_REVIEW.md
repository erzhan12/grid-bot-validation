# Feature 0090 — Code Review Trail

Plan: `docs/features/0090_PLAN.md` | Branch: `feature/0090-exchange-tick-size` (uncommitted working tree, base = main `4323622`)

## Internal review (subagent-driven development)

- 4 tasks, each spec+quality reviewed → fixed → re-reviewed. Final whole-branch review (opus): **Ready to merge**, 0 Critical / 0 Important.
- Cross-task catch: full `make test` surfaced 2 broken `live_check` `_init_runner` callers (Phase-3 signature change fallout); fixed — `live_check` was the only missed external caller.
- `/review-fix-loop-staged` (iter 1/3, 0 CRITICAL): fixed valid WARNINGs — non-finite/unparseable tick guard in `from_bybit_response` (+10 tests), 3 stale docstrings, provider `ValueError` offline-hint.

## External review trail (`/ext-code-review`, codex + cursor)

### Round 1 — both engines: **NO P1/P2 FINDINGS**

Cursor produced a 16-row verification log, all PASS (fail-closed gridbot, warn-and-use-YAML replay/backtest, gridbot-not-using-provider, single `.get()`/run, determinism, gridcore zero-dep + camelCase parsing, REST unwrap to instrument dict, optional YAML + float coercion, provider move + re-export). Codex confirmed no P1/P2.

**P3 findings raised: 11 (codex 3, cursor 8). Triaged individually against the code.**

Accepted + fixed (6 — all cheap, clearly-right, accuracy/robustness; no behavior regression):

| # | Engine | File | Fix |
|---|--------|------|-----|
| 1 | cursor | `bybit_adapter/instrument_info.py` `resolve_tick_size` | On YAML==exchange, return `exchange_tick` (plan step 4) not `yaml_tick` — a matching `"0.10"` override no longer changes the tick's representation. +test `test_matching_yaml_returns_exchange_representation`. |
| 2 | cursor | `gridcore/instrument_info.py` `from_bybit_response` | `instrument.get("lotSizeFilter") or {}` (+priceFilter) — a present-but-null filter now yields clean `None`, not `AttributeError`. +test `test_null_filter_returns_none`. |
| 3 | codex | `bybit_adapter/instrument_info.py` `load_from_cache` | Added `InvalidOperation` to the except tuple — a corrupt cached decimal falls back to refetch, not a crash. +test `test_load_from_cache_bad_decimal_field`. |
| 4 | codex | `RULES.md:1097` | backtest Dependencies line updated: `gridcore, grid-db, bybit-adapter` (was "NO bybit_adapter"). |
| 5 | cursor | `gridcore/instrument_info.py:4` | Module docstring: provider lives in `bybit_adapter`, gridbot fetches via REST (was "app layers (backtest, gridbot)"). |
| 6 | cursor | `orchestrator.py:68` | `StartupReconciliationError` docstring broadened to cover the 0090 instrument-tick fail-closed trigger (exception reuse is plan-permitted). |

Rejected / recorded as accepted non-blocking gaps (5):

- **codex C3 / cursor #7** — provider test surface split (`require_live` tests in `bybit_adapter/tests`, full parse/retCode/empty suite still under `apps/backtest/tests` via the re-export path). Tests exist and pass; relocating the suite is a separate cleanup, out of 0090 scope.
- **cursor #5** — autouse instrument-info mock returns one BTCUSDT/`0.1` value for all symbols (ETH multi-strat tests drop YAML ticks). Test-design choice; behavior correct.
- **cursor #6** — `live_check/config.py:71` `tick_size` still required. Intentional: `live_check` has its own config model outside 0090 scope; deprecating it would be wrong (confirmed during implementation).
- **cursor #8** — `orchestrator.py` (~2k lines) grew another fail-closed path. No refactor warranted for 0090; pre-existing size hot spot.
- **codex C1 (partial)** — the corrupt-cache concern is now fixed (see accepted #3); the broader "bad cache field" surface beyond decimals is covered by the existing JSON/KeyError guards.

**Verification after fixes:** affected-package tests 387 passed; full `make test` green (0 failures); `make lint` clean.

## Trace

```
ext-code-review trace
  scope: 34 files
  engines: both (codex + cursor)
  iterations: 1/10
  findings: raised 11 (P1/P2: 0), accepted 6 (fixed 6), rejected/recorded 5, P3-ignored 0
  verification: full make test passed, lint clean
  result: SUCCESS (zero valid P1/P2)
```
