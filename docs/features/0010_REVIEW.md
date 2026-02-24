# 0010 Code Review: PnL Checker

## 1. Plan Implementation Verification

### File Structure

Plan specified 7 source files + 3 test files. Implementation matches all 7 source files and **exceeds** test coverage with 6 test files (added `test_config.py`, `test_fetcher.py`, `test_main.py` beyond plan spec). Both `__init__.py` files present.

### New REST Client Methods

| Method | Plan | Implemented | Notes |
|--------|------|-------------|-------|
| `get_tickers(symbol)` | Yes | Yes | Returns single ticker dict with `lastPrice`, `markPrice`, `fundingRate` |
| `get_transaction_log(...)` | Yes | Yes | Returns `(list, cursor)` tuple |
| `get_transaction_log_all(...)` | Implied | Yes | Paginated convenience wrapper with `max_pages` safety limit; returns `(list, truncated_flag)` |

All three methods are tested in `packages/bybit_adapter/tests/test_rest_client.py` (56 tests passing).

### Algorithm Steps

| Step | Plan | Implemented | Correct |
|------|------|-------------|---------|
| 1. Fetch positions | Yes | `fetcher._fetch_positions()` | `size <= 0` filter, `liqPrice` empty-string guard (`or "0"`) |
| 1. Fetch tickers | Yes | `fetcher._fetch_ticker()` | Returns `lastPrice`, `markPrice`, `fundingRate` |
| 1. Fetch wallet | Yes | `fetcher._fetch_wallet()` | Navigates `result.list[0]`, finds USDT coin |
| 1. Fetch funding | Yes | `fetcher._fetch_funding()` | Sums `funding` field, handles errors/truncation |
| 2. Unrealized PnL (abs) | Yes | `_calc_unrealised_pnl()` | Long/short formulas correct |
| 2. Unrealized PnL % (bbu2) | Yes | `_calc_unrealised_pnl_pct_bbu2()` | Reciprocal formula matches `backtest/position_tracker.py` |
| 2. Unrealized PnL % (Bybit) | Yes | In `calculate()` | `unrealised_mark / position_im * 100` with `MIN_POSITION_IM` guard |
| 2. Liq ratio | Yes | In `calculate()` | `liq_price / last_price` |
| 2. Risk multipliers | Yes | `_calc_risk_multipliers()` | Uses `Position.create_linked_pair()`, resets before calc |
| 2. Funding snapshot | Yes | In `calculate()` | `size * mark * rate` |
| 3. Compare | Yes | `comparator.compare()` | Per-field delta + tolerance check |
| 4. Report (console) | Yes | `reporter.print_console()` | Rich tables with colored pass/fail |
| 4. Report (JSON) | Yes | `reporter.save_json()` | Structured JSON with credential redaction |

### Workspace Integration

| Item | Plan | Done |
|------|------|------|
| `pyproject.toml` with dependencies | Yes | Yes (gridcore, bybit-adapter, pyyaml, pydantic, rich) |
| Root `pyproject.toml` pythonpath | Yes | Yes (`apps/pnl_checker/src`) |
| Root `pyproject.toml` testpaths | Yes | Yes (`apps/pnl_checker/tests`) |
| Makefile `make test` target | Yes | Yes (`uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-append -q`) |

### CLI Interface

All plan-specified flags implemented: `--config/-c`, `--tolerance/-t`, `--output/-o`, `--debug`. `PNL_CHECKER_CONFIG_PATH` env var supported with config search fallback.

### Mark Price Source

Calculator correctly uses `pos.mark_price` (from position endpoint) for the primary unrealized PnL comparison against Bybit's `unrealisedPnl`. This matches the RULES.md guideline: "Use `pos.mark_price` (from position endpoint) NOT `ticker.mark_price`". Ticker's `last_price` is used only for the informational last-price PnL.

---

## 2. Findings

### MEDIUM - Environment variable leakage in test fixtures

`test_reporter.py` and `test_main.py` construct `PnlCheckerConfig` objects without isolating `BYBIT_API_KEY`/`BYBIT_API_SECRET` environment variables. The `AccountConfig.apply_env_overrides` validator reads these env vars on every construction. If a CI/CD environment or developer machine has these set, the config object will silently pick up the env values instead of the test fixture values.

- `test_config.py` correctly uses `monkeypatch.delenv()` to isolate; `test_reporter.py` and `test_main.py` do not.
- **Risk**: Test data contamination in environments where these env vars are set.
- **Fix**: Add `monkeypatch` (or `autouse` fixture) to clear `BYBIT_API_KEY`/`BYBIT_API_SECRET` in `test_reporter.py` and `test_main.py`, or add a shared `conftest.py` fixture.

### LOW - `setup_logging()` missing `handlers.clear()`

`main.py:setup_logging()` adds a handler to the root logger but does not call `root_logger.handlers.clear()` first. The recorder app fixed this exact pattern (see RULES.md: "setup_logging Handler Guard: `root_logger.handlers.clear()` before `addHandler()`"). If `main()` is called multiple times (e.g., in test scenarios), handlers accumulate and produce duplicate log lines.

### LOW - Double `datetime.now(UTC)` in `save_json()`

`reporter.py:save_json()` calls `datetime.now(UTC)` twice (line 174 for filename, line 179 for JSON body). The timestamps could differ by milliseconds. Should capture once and reuse.

---

## 3. Style & Consistency Notes

| Item | Status | Notes |
|------|--------|-------|
| camelCase→snake_case mapping | Correct | All 13 position fields, 7 wallet fields, 3 ticker fields correctly mapped |
| `liqPrice` empty string | Handled | `Decimal(pos.get("liqPrice", "0") or "0")` — `or "0"` catches empty string |
| Decimal usage | Consistent | All monetary values use `Decimal` throughout the pipeline |
| Division guards | Present | `MIN_POSITION_IM` and `MIN_LEVERAGE` in `calculator.py` prevent divide-by-zero |
| Credential redaction | Present | `_redact_config()` in `reporter.py` replaces credentials with `[REDACTED]` |
| Logging style | f-strings | Consistent with recorder pattern (acceptable for one-shot CLI tool) |
| Config pattern | YAML + Pydantic | Matches gridbot/recorder pattern |
| Example config | Present | `apps/pnl_checker/conf/pnl_checker.yaml.example` (same pattern as other apps) |
| `AccountConfig` uses `str` not `SecretStr` | Inconsistent | Recorder uses `SecretStr` for credentials; pnl_checker uses plain `str`. Lower risk for a CLI tool, but inconsistent. |

---

## 4. Test Review

### Coverage Summary

| Module | Stmts | Miss | Cover | Notable Gaps |
|--------|-------|------|-------|--------------|
| `__init__.py` | 0 | 0 | 100% | — |
| `calculator.py` | 108 | 2 | 98% | Low-leverage/low-IM warning branches |
| `comparator.py` | 114 | 1 | 99% | `is_numeric` property |
| `config.py` | 71 | 7 | 90% | Symbol format validator, config search fallback paths |
| `fetcher.py` | 133 | 6 | 95% | Negative position size, network-specific error branches |
| `main.py` | 77 | 19 | 75% | Config error path, network/value error paths, CLI `__main__` block |
| `reporter.py` | 88 | 17 | 81% | Account table rendering, fail-style delta, `_format_value` for non-Decimal |
| **TOTAL** | **591** | **52** | **91%** | |

### Test Quality Assessment

- **Isolation**: External dependencies (BybitRestClient, file I/O) properly mocked with `unittest.mock`.
- **Edge cases**: Empty positions, funding errors, truncation, missing calculations, zero prices, breakeven PnL.
- **Naming**: Descriptive test names that explain the scenario.
- **Pattern consistency**: Follows existing test patterns (class-based grouping, `_make_*` helpers).
- **Fast**: 71 tests in 0.79s with no unnecessary I/O or setup.

---

## 5. Design Observations (Non-Blocking)

**Funding error/truncation as FAIL**: `_build_funding_fields()` marks funding API errors and data truncation with `passed=False`, which causes the overall verdict to show FAIL. The plan specifies funding as "informational only — not compared numerically." This conflates data quality warnings with calculation accuracy. A position with perfect PnL/margin alignment will still show FAIL if the funding transaction log has >20 pages. This is intentional (tests verify this behavior) but could confuse users who see FAIL for a non-calculation issue.

**Leverage truncation to int**: `calculator.py` converts `Decimal` leverage to `int` for `PositionState.leverage` via `int(long_pos.leverage)`. This is correct for the `PositionState` type constraint (`leverage: int = 1`). Bybit linear perps use integer leverage, so no data loss in practice.

---

## 6. Validation Performed

- `uv run pytest apps/pnl_checker/tests -q` → **71 passed**
- `uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -q` → **91% total coverage**
- `uv run ruff check apps/pnl_checker/src apps/pnl_checker/tests` → **all checks passed**
- `uv run pytest packages/bybit_adapter/tests/test_rest_client.py -q` → **56 passed**
