# 0065 — Backtest wallet seed: re-mark non-USDT collateral (totalEquity / liq_price parity)

Plan: docs/features/0065_PLAN.md  |  Branch: feature/0065-collateral-remark. TDD throughout (RED → GREEN per phase).

## Grounding (resolved from real DB `recorder_ltcusdt_phase4.db`)
- [x] SOL per-coin `raw_json`: `walletBalance`, `usdValue`, `collateralSwitch`(bool), `marginCollateral`(bool) confirmed
- [x] SOL only 3 rows (quiet-coin staleness real → ticker fallback needed)
- [x] Fixture: run_id `1aff13af-…`, account `9bdb9748-…`, SOLUSDT ticker present, traded sym LTCUSDT

## Phase 1 — Data layer
- [x] 1B repos: `WalletSnapshotRepository.get_all_coins_latest_before` (+2 tests)
- [x] 1B repos: `TickerSnapshotRepository.get_mark_at_or_before` (+2 tests)
- [x] 1B `WalletSeed`: +6 fields (`field(default_factory=...)`, frozen-safe)
- [x] 1B `load_collateral_seed(...)` loader (fresh `usdValue/bal` vs ticker fallback, `_strip_tz`) (+9 tests)
- [x] 1B `engine._load_seed` merge via `dataclasses.replace`; raise `SeedDataQualityError` (+2 tests)
- [x] 1C `SeedConfig`: 4 collateral fields (+3 tests)
- [x] 1A recorder: `RecorderConfig.collateral_symbols` + merged public sub set (+3 tests)

## Phase 2 — Backtest equity re-marking
- [x] 2A `BacktestSession`: collateral kwargs, `seed_contrib`, `collateral_marks`, `update_collateral_mark`, `collateral_drift_total`, `collateral_drift_by_coin` (+7 tests)
- [x] 2A re-mark DELTA in BOTH `update_equity` AND `refresh_balances`; `current_balance`/`equity_curve`/`final_balance` unchanged
- [x] 2B `CollateralMarkFeed` (per-coin ticker stream, `mark_at(coin, ts)`) (+4 tests)
- [x] 2B engine: build session w/ collateral kwargs; `update_collateral_mark` at TOP of tick loop (before `process_fills`)
- [x] 2B wind-down: `leave_open` default for #3a (documented; close_all out of scope)

## Phase 3 — Comparator attribution
- [x] 3 `ValidationMetrics`: `non_usdt_collateral_drift_total`, `collateral_drift_by_coin`, 3 list fields
- [x] 3 engine: plumb session drift + seed lists into metrics after finalize
- [x] 3 `reporter.export_metrics` rows + `print_summary` line (+3 tests)

## Verification
- [x] All new unit tests pass (loader, repos, session, recorder config, reporter, engine_seed)
- [x] Engine integration: SOL fixture, #3a (<$1), #3b, #4 USDT-only no-op (+3 tests)
- [x] Full repo suite green — 2439 passed, 3 skipped (+38 new)
- [x] `scripts/verify_0065_collateral.py` runs on real DB (SOL drift attribution)
- [x] Operator YAML examples (replay + recorder) documented
- [x] RULES.md entry 25 added
- [x] Adversarial review pass — 4-lens + verify (P0s = positive confirmations; applied 8 P1–P3 fixes: feed monotonicity guard, deterministic tie-break, _strip_tz DRY, close_all/#3a doc, collateral_coins validator, never-marked WARN, InvalidOperation log, session contract assert)
- [ ] User sign-off + commit (awaiting explicit instruction)

## What could break (revisit)
- Frozen dataclass mutable default → `field(default_factory=...)` ✓
- Static seed balances vs live balance drift (SOL doubled mid-window) — #3a WARN documented
- `_strip_tz` tz mismatch (naive SQLite ts vs aware config at_ts) — handled + tested
- Perp mark vs Bybit `usdValue` basis divergence on tight #3a — documented limitation
