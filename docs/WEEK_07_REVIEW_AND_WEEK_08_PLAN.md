# Week 07 Review & Week 08 Plan

## Week 07 Review (Feb 10–16, 2026)

### Summary Stats

- **32 commits** across 3 merged PRs + 1 in-progress branch
- **83 files changed** — +11,542 / -93 lines
- **3 PRs merged**: #18, #19, #20
- **Current branch**: `feature/phase-h-testing` (15 uncommitted/untracked files)

---

### Phase F: Backtest Engine (Feb 10–11) — PRs #18, #19

Completed the full backtest engine from scratch:

- **Data provider** with cursor-based pagination
- **Fill simulator** and **order manager** with simulated order book
- **InstrumentInfoProvider** — refactored to OOP with 24h cache TTL
- **Position tracker** with commission/funding support and input validation
- **Session** with equity tracking, drawdown, and Sharpe ratio (resampled to fixed intervals)
- **Reporter** with CSV export
- **Runner** with two-phase tick processing
- **CLI** with `--strict` flag and differentiated exit codes (1 = config, 2 = runtime)

Key commits:

| Commit | Description |
|--------|-------------|
| `71d80d4` | Add backtest data provider, executor, fill simulator and instrument info |
| `fccbd31` | Refactor instrument_info to OOP with cache TTL and validation |
| `1da4046` | Add backtest order manager with simulated order book and tests |
| `db07344` | Clean up backtest: remove dead code, fix encapsulation, use cursor pagination |
| `e3f2e12` | Add position tracker with full state assertions in close tests |
| `e22f455` | Add backtest reporter with CSV export and tests |
| `35885b3` | Add backtest runner with two-phase tick processing and tests |
| `c3ff006` | Add backtest session with equity tracking, drawdown, and metrics |
| `087304d` | Add backtest CLI entry point and database fixtures to conftest |
| `ba20768` | Resample equity curve to fixed intervals for Sharpe ratio calculation |
| `df53887` | Add --strict flag and differentiate exit codes in backtest CLI |
| `45a67fe` | Add input validation to position tracker |

---

### Phase G: Comparator (Feb 14) — PR #20

Built the backtest-vs-live validation comparator:

- **Trade loader** — LiveTradeLoader (VWAP partial-fill aggregation) + BacktestTradeLoader
- **Trade matcher** — set-based join on `client_order_id + occurrence`
- **Validation metrics** — coverage rates, price/qty/fee/PnL accuracy, timing deltas, tolerance breach detection, PnL correlation
- **Equity comparison** module
- **Reporter** — CSV exports + formatted console summary
- **CLI entrypoint** + config model
- **Shared DB** additions: `WalletSnapshotRepository`, enums
- **105 tests, 96% coverage**

Key commits:

| Commit | Description |
|--------|-------------|
| `d963b43` | Add trade loader, matcher with tests and feature docs |
| `58a2a2c` | Add validation metrics with per-trade deltas and tolerance checks |
| `08d2542` | Add comparator package for backtest vs live validation |
| `ddb79db` | Add comparator reporter for CSV export and console summary |
| `25c19fa` | Add CLI entrypoint, config, test fixtures, and workspace integration |

---

### Phase H: Testing & Validation — Started (Feb 16)

Gridbot hardening + integration tests begun:

- **Order reconciliation loop** — periodic fetch of exchange open orders, reconciles with in-memory state, logs discrepancies
- **Wallet balance caching** — configurable `wallet_cache_interval` (default 300s) to reduce REST API calls
- **Integration tests started** — `test_engine_to_executor.py`, `test_runner_lifecycle.py`

Key commits:

| Commit | Description |
|--------|-------------|
| `8dd5bca` | Refactor test_engine_to_executor imports to module level |
| `78bfb1f` | Implement periodic order reconciliation loop and enhance configuration |
| `5b6f4fd` | Add wallet balance caching to Orchestrator |
| `a6450de` | Update grid validation in TestRunnerLifecycle tests |

Uncommitted Phase H work (15 files):

| File | Purpose |
|------|---------|
| `packages/bybit_adapter/tests/test_rest_client.py` | REST client unit tests |
| `apps/event_saver/tests/test_main.py` | EventSaver orchestration tests |
| `apps/event_saver/tests/test_public_collector.py` | Public collector tests |
| `apps/event_saver/tests/test_private_collector.py` | Private collector tests |
| `apps/gridbot/tests/test_main.py` | Gridbot main entry tests |
| `packages/bybit_adapter/tests/test_ws_client.py` | WS client edge case tests (modified) |
| `tests/integration/conftest.py` | Shared integration fixtures |
| `tests/integration/test_backtest_to_comparator.py` | Backtest → Comparator pipeline |
| `tests/integration/test_eventsaver_db.py` | EventSaver → Database pipeline |
| `tests/integration/test_shadow_validation.py` | Shadow-mode validation pipeline |
| `Makefile` | Add `test-integration` target |
| `pyproject.toml` | Integration test path updates |

---

### Known Issues (from 0007_REVIEW.md)

| Severity | Issue |
|----------|-------|
| P3 | Unused `Order` import in `tests/integration/test_eventsaver_db.py:19` |
| P3 | Root `pytest` invocation fails with `ImportPathMismatchError` due to conftest collisions |

---

## Week 08 Plan (Feb 17–23, 2026)

### Goals

1. Finish and merge Phase H (testing & validation)
2. Build a standalone data recorder for Bybit mainnet
3. Build a replay engine for shadow-mode validation
4. Start first multi-day recording session

### Success Criteria

- Phase H merged to master with all critical path tests passing
- Data recorder running and capturing mainnet data for 1+ symbols
- Replay engine can consume recorded DB and produce trades
- Comparator validates replay results within <1% PnL deviation (on synthetic data first)
- Pipeline is end-to-end testable: `record → replay → compare → report`

---

### Days 1–2 (Mon–Tue): Finish & Merge Phase H

| Task | Details |
|------|---------|
| Commit Phase H work | Commit all 15 uncommitted/untracked files on `feature/phase-h-testing`, create PR |
| Critical path coverage | Ensure all three critical flows are well-tested: GridEngine→Executor, EventSaver pipeline, Backtest→Comparator |
| ImportPathMismatchError | Investigate root `pytest` collision — fix if it's a quick `conftest.py` or `pyproject.toml` config change, defer if it requires restructuring |
| Merge Phase H | PR review, fix any CI issues, merge to master |

**Critical paths to validate:**

1. **GridEngine → Executor flow** — ticker → grid decisions → order placement
2. **EventSaver pipeline** — WS events → normalization → DB persistence
3. **Backtest → Comparator pipeline** — backtest trades → comparator → validation metrics

---

### Days 3–4 (Wed–Thu): Build Data Recorder

| Task | Details |
|------|---------|
| New package: `apps/recorder` | Standalone CLI tool for recording Bybit mainnet data |
| Data streams | All streams EventSaver captures: public trades, ticker, executions, orders, positions, wallet |
| Storage | Same `grid_db` SQLite schema — comparator and backtest can read directly |
| Config | YAML-based, configurable symbols (start with BTCUSDT) |
| Reconnection | Leverage existing WS client reconnection + add gap detection logging (log when connection drops, record timestamps of gaps) |
| Multi-day support | Designed for 3–7 day runs |
| Tests | Unit tests for recorder logic, mock WS data |

**Architecture:**

```
apps/recorder/
├── conf/
│   └── recorder.yaml.example
├── src/recorder/
│   ├── __init__.py
│   ├── main.py          # CLI entry point
│   ├── config.py        # YAML config model
│   ├── recorder.py      # Core recording logic
│   └── gap_detector.py  # Connection gap tracking
├── tests/
│   ├── conftest.py
│   └── test_recorder.py
└── pyproject.toml
```

**Data streams to capture:**

| Stream | Source | Storage Table |
|--------|--------|---------------|
| Public trades | WS `publicTrade` | `public_trades` |
| Ticker | WS `tickers` | `ticker_snapshots` |
| Executions | WS `execution` | `private_executions` |
| Orders | WS `order` | `orders` |
| Positions | WS `position` | `positions` |
| Wallet | WS `wallet` | `wallet_snapshots` |

---

### Days 5–6 (Fri–Sat): Replay Engine + Shadow Pipeline

| Task | Details |
|------|---------|
| Replay engine | Reads recorded data from SQLite DB, feeds through GridEngine + BacktestOrderManager in chronological order |
| Trade normalization | Both replay and recorded execution trades output as `NormalizedTrade` |
| Comparator integration | Run `TradeMatcher` + `calculate_metrics()` on replay-vs-recorded results |
| Tolerance config | <1% PnL deviation threshold, configurable per-metric |
| CLI | `make shadow-validate --db path/to/recorded.db --config gridbot.yaml` |

**Replay flow:**

```
Recorded DB (SQLite)
       │
       ▼
  ReplayDataProvider
  (reads trades + ticker chronologically)
       │
       ▼
  GridEngine.on_event()
  + BacktestOrderManager
       │
       ▼
  Simulated Trades
  (NormalizedTrade format)
       │
       ▼
  TradeMatcher.match()
  (simulated vs recorded executions)
       │
       ▼
  calculate_metrics()
  (validate <1% PnL deviation)
       │
       ▼
  ComparatorReporter
  (CSV + console summary)
```

---

### Day 7 (Sun): Start Recording + Documentation

| Task | Details |
|------|---------|
| Start first recording | Launch recorder against Bybit mainnet with BTCUSDT, targeting 3–7 day capture |
| 0008_PLAN.md | Document the shadow mode architecture and validation pipeline |
| RULES.md update | Add recorder patterns, replay engine conventions |

---

### Risk & Mitigation

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| WS disconnections during multi-day recording | Medium | Gap detection logging, auto-reconnection, alerting on long gaps |
| Recorded data format incompatible with replay engine | Low | Using same `grid_db` schema ensures compatibility |
| Phase H takes longer than 2 days | Medium | Focus on critical paths only, defer non-essential coverage gaps |
| Replay engine produces non-deterministic results | Medium | Use deterministic tick ordering, seed random if needed |