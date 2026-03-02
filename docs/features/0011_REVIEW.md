# 0011 Review — Backtest Risk Multipliers

Plan: `docs/features/0011_PLAN.md`  
Rubric: `commands/code_review.md`

## Findings (ordered by severity)

### [P0] Risk hook overrides base sizing callback, so risk-enabled runs place zero-qty orders

`BacktestEngine` builds an executor with the amount-based sizing callback (`apps/backtest/src/backtest/engine.py:255-260`).  
`BacktestRunner` then unconditionally overwrites that callback when risk is enabled (`apps/backtest/src/backtest/runner.py:121-122`).

In production flow, `GridEngine` emits `PlaceLimitIntent` with `qty=0` by design (`packages/gridcore/src/gridcore/engine.py:354`).  
The new risk callback multiplies `intent.qty` directly (`apps/backtest/src/backtest/runner.py:399-400`), so result qty stays `0`, and executor rejects placement (`qty <= 0`).

Impact:
- Default config now has `enable_risk_multipliers=True`, so this affects normal backtest runs.
- Orders are not placed, making results invalid.

Runtime repro (from `apps/backtest`):
- `enable_risk True orders 0`
- `enable_risk False orders 100`

### [P1] Risk recalculation uses fill price, not current market price

`process_fills()` stores ticker `last_price` (`apps/backtest/src/backtest/runner.py:185`), but `_process_fill()` calls `_update_risk_multipliers(float(event.price))` (`apps/backtest/src/backtest/runner.py:284`).

In this backtest model, fill price is always the order limit price, not the current ticker price (`apps/backtest/src/backtest/fill_simulator.py:31-36`).

Impact:
- Liquidation ratio checks use the wrong denominator (`liq_price / last_close`).
- Risk multipliers can be materially off during fast moves (exactly when risk logic matters most).

Runtime repro:
- `stored_last_price 95000`
- `update_called_with 100000.0`

### [P2] New tests miss real execution path; regression escaped

Coverage exists, but key assertions are too weak for this feature:
- `test_process_tick_fills_order` is conditional and can pass with no placed orders (`apps/backtest/tests/test_runner.py:71-89`).
- Risk qty tests use synthetic intents with non-zero `intent.qty` (`apps/backtest/tests/test_runner.py:290-320`), but real engine intents have `qty=0` (`packages/gridcore/src/gridcore/engine.py:354`).
- No integration test verifies that risk-enabled runner still places non-zero orders using configured amount patterns and rounding.

## Plan Implementation Coverage

Implemented:
- Config fields added: `leverage`, `maintenance_margin_rate`, `enable_risk_multipliers`.
- Runner wiring added: linked `Position` pair, position-state builder, liquidation estimator, multiplier updater, multiplier getter.
- Qty hook added for risk multiplier application.
- Unit tests for new helpers/config fields added.

Not correctly implemented:
- Qty multiplier integration does not compose with existing amount/rounding calculator (P0).
- Recalculation input price is fill price instead of ticker last price (P1).

## Test/Lint Evidence

- `uv run pytest apps/backtest/tests/test_runner.py -q` → `20 passed`
- `uv run pytest apps/backtest/tests/test_engine.py -q` → `15 passed`
- `uv run ruff check apps/backtest/src/backtest/config.py apps/backtest/src/backtest/runner.py apps/backtest/tests/test_runner.py apps/backtest/tests/test_engine.py` → fails on existing unused imports in test files
