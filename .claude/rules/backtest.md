---
paths:
  - "apps/backtest/**"
---

## backtest — Backtest Engine

**Path**: `apps/backtest/` | **Dependencies**: gridcore, grid-db, bybit-adapter (0090: `InstrumentInfoProvider` home)

### Architecture

- Reuses `GridEngine` directly, no modifications
- In-memory order book, trade-through fill model, position tracking with PnL
- Funding simulation (8-hour intervals)
- **Strict cross fill**: BUY fills when `price < limit` (not `<=`), SELL when `price > limit`

### Key Patterns

- **Order format for GridEngine**: camelCase keys (`orderId`, `orderLinkId`, `price` as string)
- **`BacktestPositionTracker`** tracks PnL; **`gridcore.Position`** handles risk multipliers (different purposes)
- **Quantity**: same amount format as gridbot (`"100"`, `"x0.001"`); rounding uses `math.ceil`. Legacy `"b..."` removed in 0028.
- **Two-phase tick**: `process_fills()` → equity update → `execute_tick()` (fills reflected before sizing)
- **Equity update**: Engine level, not runner level (aggregates all runners' unrealized PnL)
- **`WindDownMode` StrEnum**: `LEAVE_OPEN`, `CLOSE_ALL`
- **InstrumentInfoProvider** (`packages/bybit_adapter/src/bybit_adapter/instrument_info.py`): Fetches from Bybit API, 24h cache, fallback cascade: fresh cache → API → stale cache → defaults. `tick_size` is sourced from the exchange (0090); YAML `tick_size` is optional — gridbot uses it as a fail-closed cross-check (mismatch aborts startup), replay/backtest use it as a what-if override (mismatch warns, YAML wins).

### Risk Multiplier Composition (CRITICAL)

- GridEngine emits `qty=0` — risk callback must COMPOSE with base `qty_calculator`, not replace it
- **WRONG**: `executor.qty_calculator = risk_callback` (overwrites; `0 * multiplier = 0`)
- **RIGHT**: Save base calculator, compose: `base_qty = base_calc(intent, balance); return base_qty * multiplier`
- Risk recalculation uses `last_price` (market), NOT fill price
- Tests with synthetic `qty=Decimal("0.001")` hide the zero-qty bug — always test with `qty=0`
- Conditional assertions (`if limit_orders["long"]:`) silently pass — use unconditional `assert len(...) > 0`
- Defensive guard: check `self._long_position is not None` before calling `.reset_amount_multiplier()`
- Division-by-zero: when `position_value > 0` but `wallet_balance == 0`, raise `ValueError`
- ALL test fixtures creating `BacktestExecutor` MUST include a `qty_calculator`

### CLI

```bash
uv run python -m backtest.main --config conf/backtest.yaml
uv run python -m backtest.main --config conf/backtest.yaml --start "2025-01-01" --end "2025-01-31"
uv run python -m backtest.main --config conf/backtest.yaml --export results.csv
uv run python -m backtest.main --config conf/backtest.yaml --strict
```

Exit codes: `0` = success, `1` = config error, `2` = execution error

### Metrics & Reporting

- `BacktestMetrics`: trades, PnL, risk (max drawdown, Sharpe), balance, volume, direction breakdown
- Sharpe ratio: equity resampled to fixed intervals (default 1h), annualized 365.25 days
- `BacktestReporter`: CSV exports (trades, equity curve, metrics, all)

---

