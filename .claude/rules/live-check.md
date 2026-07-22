---
paths:
  - "apps/live_check/**"
---

## live_check — Replay-vs-Live Reconciliation (feature 0088)

**Path**: `apps/live_check/` — CLI `uv run live-check` (`--once` default, `--watch <interval>`, `--per-fill`, `--curve`; window `--last 4h` / `--lag 2m`).

Wraps `ReplayEngine` (seeded `event_follower`, never `last_cross`) per strat over a rolling window and compares against RECORDED ground truth only (never live Bybit REST). Verdict: `live_only==[] AND backtest_only==[]`, |Δrealized|<0.01, |Δcommission|<0.01, |Δunrealised|<0.50 (net per pair). Exit codes: 0 all-PASS, 1 FAIL/config error, 2 SKIP/no-data (zero-data window is NEVER a PASS).

### Key Rules

- **Read-only DB open**: `DatabaseSettings(read_only=True)` rewrites file SQLite URLs to `mode=ro&uri=true` (`grid_db/database.py`). `mode=ro` ONLY — `immutable=1` freezes the snapshot and `--watch` would miss new recorder rows. Ground-truth reads use `get_readonly_session()` (no auto-commit).
- **`ReplayEngine(..., emit_backtest_snapshots=False)`** wires a no-op position-snapshot writer so `.run()` never inserts `source='backtest'` rows into the read-only live DB. Default True keeps all other callers unchanged.
- **Symbol scoping is mandatory**: both strats share one run_id; `get_by_run_range` has NO symbol filter — ground-truth sums use direct `func.coalesce(func.sum(...), 0)` queries filtered by run_id + symbol + window.
- **Matched gate ≠ raw exec count**: partial fills aggregate several `private_executions` rows into one `NormalizedTrade`; `live_exec_count` is display-only.
- **Pre-0080 guard (two floors)**: `window.start >= run.start_ts` AND `>= 2026-06-17T23:07:00Z` (`window.POST_0080_CUTOFF`) — pre-0080 data collapses link_id matching (954→44).
- **Freshness gate (`--watch`)**: probes `MAX(TickerSnapshot.exchange_ts)` per symbol (no run_id column; `PrivateExecution` would false-trip on quiet periods); threshold `max(2*lag, 5m)`; `None` ticker ts → SKIP line, never crash. Seed miss (`SeedDataQualityError`) → SKIP line, watch loop continues.
- **`account_id` must be pre-queried** from the `Run` row before building `ReplayConfig` — `SeedConfig` requires it at construction time.
- All query/comparison datetimes normalized to naive UTC (`window.to_naive_utc`) — SQLite stores tz-stripped; aware-vs-naive math raises `TypeError`.
- Replay unrealised source: `ReplayResult.session.metrics.total_unrealized_pnl` (finalized `BacktestMetrics`) — NOT `ReplayResult.metrics` (comparator `ValidationMetrics`, no such field).

---

