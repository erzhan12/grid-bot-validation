# 0013: Live Testing Plan

## Overview

Three-phase live testing strategy to validate the grid bot system against real Bybit market data, progressing from zero-risk passive recording to full shadow-mode bot operation.

All infrastructure is already built: recorder, replay engine, comparator, PnL checker, and gridbot with shadow mode. This plan focuses on configuration, execution, and validation criteria.

---

## Phase 1: Passive Data Recording (Zero Risk)

**Goal**: Capture live Bybit mainnet data for BTCUSDT without any trading activity.

### What It Does

- Recorder connects to Bybit mainnet WebSocket (public streams)
- Captures ticker snapshots and public trades to SQLite
- Optionally captures private account data (executions, orders, positions, wallet) with read-only API keys
- Runs for 3-7 days to build a representative dataset across market conditions

### What It Validates

- WebSocket connectivity and stability over multi-day runs
- Reconnection logic and gap detection/reconciliation
- Database write throughput and storage sizing
- Data completeness (no missing ticks, no duplicate trades)

### Prerequisites

- No API keys required for public-only recording
- Read-only Bybit API keys required for private stream capture (optional)
- Stable network connection for multi-day run

### Configuration

Create `apps/recorder/conf/recorder.yaml`:

```yaml
symbols:
  - "BTCUSDT"
database_url: "sqlite:///data/recorder_btcusdt.db"
testnet: false
batch_size: 100
flush_interval: 5.0
gap_threshold_seconds: 5.0
health_log_interval: 300

# Optional: add for private stream capture
# account:
#   api_key: "YOUR_READ_ONLY_API_KEY"
#   api_secret: "YOUR_READ_ONLY_API_SECRET"
```

### How to Run

```bash
# From project root
mkdir -p data
uv run python -m recorder.main --config apps/recorder/conf/recorder.yaml

# With debug logging for initial smoke test
uv run python -m recorder.main --config apps/recorder/conf/recorder.yaml --debug
```

### Success Criteria

| Metric | Target |
|--------|--------|
| Uptime | >99% over recording period |
| Gaps detected | <5 per day |
| Gap reconciliation | 100% backfilled via REST |
| Ticker snapshots | Continuous, no >10s gaps |
| Public trades | Complete, deduplicated by trade_id |
| DB size (estimate) | ~50-100 MB per day for BTCUSDT |

### Smoke Test (5-Minute Quick Check)

1. Start recorder with `--debug`
2. Verify log output: "connected", "subscribed", trade/ticker events flowing
3. Stop after 5 minutes (Ctrl+C)
4. Inspect DB:
   ```bash
   sqlite3 data/recorder_btcusdt.db "SELECT COUNT(*) FROM public_trades;"
   sqlite3 data/recorder_btcusdt.db "SELECT COUNT(*) FROM ticker_snapshots;"
   sqlite3 data/recorder_btcusdt.db "SELECT MIN(exchange_ts), MAX(exchange_ts) FROM ticker_snapshots;"
   ```
5. Expect: thousands of trades, hundreds of ticker snapshots, timestamps spanning ~5 minutes

---

## Phase 2: Shadow Replay Validation (Zero Risk)

**Goal**: Validate grid engine logic by replaying recorded data and comparing simulated trades against actual market behavior.

### What It Does

- Replay engine reads the recorded DB from Phase 1
- Feeds ticker snapshots chronologically through GridEngine + BacktestOrderManager
- Generates simulated trades as if the grid bot had been running
- Comparator matches simulated trades vs recorded executions (if private streams were captured)
- PnL checker validates calculation accuracy against Bybit REST API

### What It Validates

- Grid engine produces correct buy/sell decisions from real price sequences
- Position tracking and PnL calculations match expected values
- Risk multipliers behave correctly across real market volatility
- Liquidation price estimates are within acceptable range of Bybit's values
- Equity curve simulation is reasonable

### Prerequisites

- Completed Phase 1 recording (minimum 24 hours recommended, 3+ days ideal)
- Grid strategy parameters decided (grid_count, grid_step, amount)
- For PnL checker: read-only Bybit API keys

### Configuration

Create `apps/replay/conf/replay.yaml`:

```yaml
database_url: "sqlite:///data/recorder_btcusdt.db"
run_id: null                          # Auto-discovers latest recording run
symbol: "BTCUSDT"
start_ts: null                        # Uses full recording range
end_ts: null

strategy:
  tick_size: 0.1
  grid_count: 50
  grid_step: 0.2
  amount: "x0.001"                    # 0.1% of wallet per order
  commission_rate: 0.0002             # 0.02% maker fee

initial_balance: 10000
enable_funding: true
funding_rate: 0.0001
wind_down_mode: "leave_open"

output_dir: "results/replay"
price_tolerance: 0
qty_tolerance: 0.001
```

Create `apps/pnl_checker/conf/pnl_checker.yaml`:

```yaml
account:
  api_key: "YOUR_READ_ONLY_API_KEY"
  api_secret: "YOUR_READ_ONLY_API_SECRET"

symbols:
  - symbol: "BTCUSDT"
    tick_size: "0.1"

risk_params:
  min_liq_ratio: 0.8
  max_liq_ratio: 1.2
  max_margin: 8.0
  min_total_margin: 0.15

tolerance: 0.01
```

### How to Run

```bash
# Replay recorded data through grid engine
uv run python -m replay.main --config apps/replay/conf/replay.yaml

# PnL checker (requires live API connection)
uv run python -m pnl_checker.main --config apps/pnl_checker/conf/pnl_checker.yaml
```

### Success Criteria

| Metric | Target |
|--------|--------|
| Replay completes | No crashes, processes all ticks |
| Trade match rate | >95% (if private streams were recorded) |
| Price delta (mean) | <0.1% of trade price |
| Qty delta (mean) | <0.1% of trade qty |
| PnL correlation | >0.95 (Pearson) |
| Equity curve divergence | <2% mean divergence |
| PnL checker fields | All within tolerance |
| Tolerance breaches | <5% of matched trades |

### Analysis Checklist

- [ ] Review `results/replay/matched_trades.csv` — check price/qty deltas
- [ ] Review `results/replay/unmatched_trades.csv` — investigate live-only and backtest-only trades
- [ ] Review `results/replay/metrics.csv` — confirm all metrics within targets
- [ ] Review equity curve — visual sanity check for drift
- [ ] Run PnL checker — confirm calculation accuracy vs Bybit
- [ ] Check for systematic bias (e.g., always over/under-estimating PnL)

---

## Phase 3: Shadow Mode Live Bot (Near-Zero Risk)

**Goal**: Run the full gridbot in shadow mode — connected to live Bybit, processing real-time events, generating decisions — but never executing orders.

### What It Does

- Gridbot connects to Bybit WebSocket (ticker + private streams)
- GridEngine processes real-time ticks and generates PlaceLimitIntent / CancelIntent
- IntentExecutor logs intents but skips API calls (`shadow_mode: true`)
- Recorder runs in parallel to capture ground truth market data
- After multi-day run, compare shadow bot decisions against what actually happened

### What It Validates

- Full orchestrator lifecycle: startup, WS routing, background loops, graceful shutdown
- Real-time event handling latency and throughput
- Order reconciliation loop behavior with live exchange state
- Position check loop and wallet caching under real conditions
- Reconnection resilience during market volatility
- Same-order error detection with real order sequences
- Memory stability and resource usage over multi-day runs

### Prerequisites

- Completed Phase 2 with acceptable metrics
- Bybit API keys (read-only sufficient for shadow mode)
- Decision on testnet vs mainnet:
  - **Testnet**: safer, but different liquidity/behavior than mainnet
  - **Mainnet with read-only keys**: real data, shadow_mode prevents execution

### Configuration

Create `apps/gridbot/conf/gridbot.yaml`:

```yaml
accounts:
  - name: "main"
    api_key: "YOUR_API_KEY"
    api_secret: "YOUR_API_SECRET"
    testnet: false                    # true for testnet, false for mainnet

strategies:
  - strat_id: "btc_grid_shadow"
    account: "main"
    symbol: "BTCUSDT"
    tick_size: 0.1
    grid_count: 50
    grid_step: 0.2
    amount: "x0.001"
    shadow_mode: true                 # CRITICAL: no orders executed
    max_margin: 8.0
    long_koef: 1.0
    min_liq_ratio: 0.8
    max_liq_ratio: 1.2
    min_total_margin: 0.15

database_url: "sqlite:///data/gridbot_shadow.db"
position_check_interval: 63.0
order_sync_interval: 61.0
wallet_cache_interval: 300.0
```

### How to Run

```bash
# Terminal 1: Start recorder (captures ground truth)
uv run python -m recorder.main --config apps/recorder/conf/recorder.yaml

# Terminal 2: Start shadow gridbot
uv run python -m gridbot.main --config apps/gridbot/conf/gridbot.yaml

# Optional: periodic PnL checks
uv run python -m pnl_checker.main --config apps/pnl_checker/conf/pnl_checker.yaml
```

### Success Criteria

| Metric | Target |
|--------|--------|
| Uptime | >99% over 3+ day run |
| Shadow intents logged | Continuous, matching tick rate |
| WS reconnections | Handled gracefully, no missed events |
| Memory usage | Stable (no leaks over multi-day run) |
| Background loops | All running (position check, order sync, health) |
| Graceful shutdown | Clean exit on SIGINT/SIGTERM |
| Log errors | Zero unexpected errors |

### Monitoring Checklist

- [ ] Check health logs every few hours (logged at `health_log_interval`)
- [ ] Monitor DB file size growth
- [ ] Watch for reconnection events in logs
- [ ] Verify shadow intents are being generated (not silent)
- [ ] Check memory usage (`ps aux | grep gridbot`)
- [ ] After run: replay + compare shadow decisions vs recorded market data

---

## Timeline

| Week | Activity |
|------|----------|
| Week 1, Days 1-2 | Phase 1 setup + smoke test + start recording |
| Week 1, Days 3-7 | Phase 1 multi-day recording |
| Week 2, Days 1-2 | Phase 2 replay + analysis |
| Week 2, Days 3-4 | Fix any issues found in Phase 2 |
| Week 2, Days 5-7 | Phase 3 shadow mode + monitoring |
| Week 3 | Review Phase 3 results, decide on live trading |

---

## Risk Mitigation

| Risk | Phase | Mitigation |
|------|-------|------------|
| WS disconnection during recording | 1 | Gap detection + REST reconciliation (built-in) |
| DB corruption from ungraceful shutdown | 1, 3 | SQLite WAL mode, periodic flush, SIGTERM handler |
| Stale data in replay (market regime change) | 2 | Record during representative conditions (weekday + weekend) |
| Shadow mode accidentally disabled | 3 | Verify `shadow_mode: true` in config before launch; IntentExecutor logs skip |
| API key exposure | 1, 2, 3 | Keep YAML configs in `.gitignore`, use read-only keys where possible |
| Memory leak in multi-day run | 1, 3 | Monitor with `ps`, check deque sizes are bounded |

---

## Files Reference

| Component | Config Example | Entry Point |
|-----------|---------------|-------------|
| Recorder | `apps/recorder/conf/recorder.yaml.example` | `python -m recorder.main` |
| Replay | `apps/replay/conf/replay.yaml.example` | `python -m replay.main` |
| Comparator | CLI args | `python -m comparator.main` |
| PnL Checker | `apps/pnl_checker/conf/pnl_checker.yaml.example` | `python -m pnl_checker.main` |
| Gridbot | `apps/gridbot/conf/gridbot.yaml.example` | `python -m gridbot.main` |
