---
paths:
  - "apps/live_check/**"
---

## live_check — Replay-vs-Live Reconciliation (feature 0088)

**Path**: `apps/live_check/` — CLI `uv run live-check` (`--once` default, `--watch <interval>`, `--per-fill`, `--curve`; window `--last 4h` / `--lag 2m`).

Wraps `ReplayEngine` (seeded `event_follower`, never `last_cross`) per strat over a rolling window and compares against RECORDED ground truth only (never live Bybit REST). Verdict (five checks since 0100): `live_only==[] AND backtest_only==[]`, **qty exact-equality on every matched pair** (`qty_mismatch_count==0`), |Δrealized|<0.01, |Δcommission|<0.01, |Δunrealised|<0.50 (net per pair). Exit codes: 0 all-PASS, 1 FAIL/config error, 2 SKIP/no-data (zero-data window is NEVER a PASS).

### Key Rules

- **Read-only DB open**: `DatabaseSettings(read_only=True)` rewrites file SQLite URLs to `mode=ro&uri=true` (`grid_db/database.py`). `mode=ro` ONLY — `immutable=1` freezes the snapshot and `--watch` would miss new recorder rows. Ground-truth reads use `get_readonly_session()` (no auto-commit).
- **`ReplayEngine(..., emit_backtest_snapshots=False)`** wires a no-op position-snapshot writer so `.run()` never inserts `source='backtest'` rows into the read-only live DB. Default True keeps all other callers unchanged.
- **Symbol scoping is mandatory**: both strats share one run_id; `get_by_run_range` has NO symbol filter — ground-truth sums use direct `func.coalesce(func.sum(...), 0)` queries filtered by run_id + symbol + window.
- **Matched gate ≠ raw exec count**: partial fills aggregate several `private_executions` rows into one `NormalizedTrade`; `live_exec_count` is display-only.
- **Pre-0080 guard (two floors)**: `window.start >= run.start_ts` AND `>= 2026-06-17T23:07:00Z` (`window.POST_0080_CUTOFF`) — pre-0080 data collapses link_id matching (954→44).
- **Freshness gate (ALL modes since 0100, was watch-only)**: probes `MAX(TickerSnapshot.exchange_ts)` per symbol (no run_id column; `PrivateExecution` would false-trip on quiet periods); threshold `max(2*lag, 5m)`; `None` ticker ts → SKIP line, never crash. Seed miss (`SeedDataQualityError`) → SKIP line, watch loop continues. `--once`/`--shared` skip BEFORE any replay runs — a stopped recorder's self-consistent prefix must never PASS.
- **Qty gate (0100)**: identity matching alone is blind to quantity — the qty-excess cap pro-rates fee/pnl and emits under the SAME matching key, and trailing partials left undrained after the window's last ticker tick shrink the replay rollup. `_count_qty_mismatches` uses exact equality (no tolerance): event_follower applies recorded `exec_qty` as-is, so any mismatch is real divergence.
- **`enable_funding=False` in BOTH `build_replay_config` and `build_multi_replay_config` (0100)** — the inherited replay default (True, canned 0.0001 rate) is not Bybit's recorded rate; simulated funding drifts `current_balance`/sizing away from live. Never re-enable in a reconcile run.
- **`account_id` must be pre-queried** from the `Run` row before building `ReplayConfig` — `SeedConfig` requires it at construction time.
- All query/comparison datetimes normalized to naive UTC (`window.to_naive_utc`) — SQLite stores tz-stripped; aware-vs-naive math raises `TypeError`.
- Replay unrealised source: `ReplayResult.session.metrics.total_unrealized_pnl` (finalized `BacktestMetrics`) — NOT `ReplayResult.metrics` (comparator `ValidationMetrics`, no such field).
- **Seed-time U0 correction (feature 0101)**: seeded replay now subtracts seed-time `U0` from the UPL-bearing baselines (single-strat: `initial_balance`+`initial_equity`; `--shared`: `initial_balance` only — see `replay.md`). The single-strat verdict formula compares realized/commission/unrealised sums + qty (NO balances) so it is UNCHANGED; the `--shared` `equity_ok` gate (`verdict.py:207`) reads the multi `total_equity` axis, which is `coin_balance` cash and was never overstated, so it is also unchanged. BUT the corrected `current_balance` feeds wallet-fraction qty sizing / margin gating inside the replay, so post-0101 seeded runs can place slightly different orders — trade-level verdicts must be re-confirmed empirically on fixed code, not assumed from pre-0101 PASS history.

---

