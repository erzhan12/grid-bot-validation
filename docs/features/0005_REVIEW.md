# 0005 Backtest Rewrite — Code Review (Updated)

## Findings (ordered by severity)

1. Medium — Plan-level persistence is still missing. The plan specifies `Run` records and a `BacktestExecution` model with DB writes, but there is no model or persistence path in the engine. Files: `shared/db/src/grid_db/models.py`, `apps/backtest/src/backtest/engine.py`.

2. Low — CLI export still bypasses the new reporter and only writes trades. `backtest.main` uses the legacy `export_results()` helper, so equity curve and metrics exports (now supported by `BacktestReporter`) are not accessible from the CLI. Files: `apps/backtest/src/backtest/main.py`, `apps/backtest/src/backtest/reporter.py`.

3. Low — Test gaps remain for DB-backed `HistoricalDataProvider` and for `close_all` with guaranteed open positions. The plan’s integration/comparison tests are also still absent. Files: `apps/backtest/tests/`, `apps/backtest/tests/test_engine.py`.

4. Low — Unused config fields/imports remain (`long_koef`, `min_liq_ratio`, `max_liq_ratio`, `min_total_margin`, unused repository imports). Files: `apps/backtest/src/backtest/config.py`, `apps/backtest/src/backtest/data_provider.py`.

## Questions / assumptions

- Should the CLI `--export` option be upgraded to use `BacktestReporter` and emit trades + equity + metrics, or is a trades-only CSV still the desired output?
- Is DB persistence for backtests intentionally deferred, or should it be implemented now per the plan?
- Do you want explicit integration/comparison tests added now, or keep the current unit‑level coverage?

## Change summary

- Updated findings to reflect remaining gaps after the latest fixes (equity timing, close_all, metrics/reporter, and tests).
