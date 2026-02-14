# 0006 Comparator (Phase G) — Code Review (Re-run)

Plan: `docs/features/0006_PLAN.md`  
Review rubric: `commands/code_review.md`

## Findings (ordered by severity)

1. Low — Occurrence assignment can still drift when reused IDs share identical sort keys.
   - Occurrence indices are assigned after sorting by `(timestamp, client_order_id, side)`.
   - If two reused-lifecycle trades share this exact tuple in both sources but arrive in different relative order, occurrence pairing can mismatch.
   - This is explicitly documented in code as a known limitation and is likely rare, but it remains a correctness edge case.
   - Files: `apps/comparator/src/comparator/loader.py:55`, `apps/comparator/src/comparator/loader.py:126`, `apps/comparator/src/comparator/matcher.py:48`.

2. Low — Direct `_load_backtest_from_config()` runtime path is still largely untested.
   - `main.py` coverage remains 83%, with most uncovered lines in the real config-driven backtest execution branch (`backtest.config` load, `BacktestEngine.run`, session/equity extraction).
   - Current tests mock this path (good unit isolation), but a thin integration test around the real call chain would better protect against wiring regressions.
   - Files: `apps/comparator/src/comparator/main.py:157`, `apps/comparator/src/comparator/main.py:168`, `apps/comparator/src/comparator/main.py:175`.

No higher-severity functional regressions were found in this re-run.

## Resolved Since Prior Review

- CSV mode symbol filtering is now symmetric: `run()` filters backtest trades by `config.symbol` before matching.
- Mixed naive/aware timezone crashes are fixed via timestamp normalization (`_normalize_ts`) across trade and equity loaders.
- Config mode now explicitly requires `--symbol`, and plan docs were updated to match (`symbol` optional in CSV mode, required in config mode).
- Reused `client_order_id` matching now uses `(client_order_id, occurrence)`.
- `matched_trades.csv` is correctly aligned for reused IDs (pair-order zip with `trade_deltas`), and includes `occurrence`.
- Unmatched CSV now includes `occurrence`.
- Tolerance breaches now retain occurrence context (`list[tuple[str, int]]`).
- Main datetime parsing now normalizes aware datetimes to UTC.
- CLI/runtime coverage in `main.py` improved substantially.

## Plan Compliance Snapshot

Implemented and working:
- Comparator package structure and CLI entrypoints are in place (`config`, `loader`, `matcher`, `metrics`, `equity`, `reporter`, `main`).
- Live partial-fill aggregation is implemented with `(order_link_id, order_id)` grouping.
- Reuse-aware occurrence indexing is implemented and propagated to matching/reporting.
- CSV reports and optional equity export are integrated into `export_all`.
- Workspace and DB integration changes are present (`Makefile`, `RULES.md`, `shared/db` repository additions/tests).

Outstanding correctness gaps:
- Deterministic tie-break for occurrence assignment is still absent in the exact-equal-key reuse edge case.
- Real config-mode backtest execution path has limited non-mocked test coverage.

## Test / Lint Evidence

Executed during this re-review:
- `uv run ruff check apps/comparator`
  - Result: `All checks passed!`
- `uv run pytest apps/comparator/tests --cov=comparator --cov-report=term-missing -q`
  - Result: `105 passed`
  - Coverage: `96%` total; `apps/comparator/src/comparator/main.py` is `83%`.
- `uv run pytest shared/db/tests -q`
  - Result: `98 passed`
