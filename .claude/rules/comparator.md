---
paths:
  - "apps/comparator/**"
---

## comparator — Backtest vs Live Validation

**Path**: `apps/comparator/`

### Key Concepts

- **Trade matching**: Joins on `(client_order_id, occurrence)` composite key (handles deterministic ID reuse)
- **Occurrence**: nth time same client_order_id appears chronologically
- **Live partial fills**: Aggregated by `(order_link_id, order_id)` using VWAP price
- **Direction inference** (live): `closed_pnl != 0` → closing trade. Limitation: break-even closes misclassified
- **For matched pairs**: Prefer backtest direction (always correct) over inferred live direction
- **Tolerance**: `tolerance=0` means exact match (any non-zero delta flagged)

### NormalizedTrade

Fields: `client_order_id`, `symbol`, `side`, `price`, `qty`, `fee`, `realized_pnl`, `timestamp`, `source`, `direction`, `occurrence`. Uses `SideType`/`DirectionType` enums.

### Key Pitfalls

- SQLite strips timezone — compare with `.replace(tzinfo=None)` in tests
- Direction != Side (a Sell can close a long position)
- Use `zip(matched, trade_deltas)` not dict keyed by client_order_id (fails on reuse)
- `breaches` stores `(client_order_id, occurrence)` tuples
- All timestamps normalized via `_normalize_ts()` to naive UTC
- `--symbol` required with `--backtest-config` mode
- `run()` filters backtest_trades by symbol before matching (symmetric filtering)

### Robust spike-vs-drift stats (feature 0070, issue #156)

Each per-snapshot abs-delta family in `position_metrics.py` AND the trade-level `pnl_*` family carry six robust stats alongside the existing `mean`/`max`, so an operator rule can tell a **sharp spike** (real divergence — few snapshots towering over baseline) from **sustained drift** (benign accumulation — many snapshots persistently above baseline). Today's flat `max > $0.30` gate conflates them (C108: `cur_realised_usdt_max_abs_delta = $0.307` tripped purely from drift).

- **Six fields per family**: `<f>_median_abs_delta`, `<f>_p95_abs_delta`, `<f>_std_abs_delta`, `<f>_spike_intensity` (Decimal) + `<f>_spike_count_30c`, `<f>_spike_count_relative_3` (int). Helper: `comparator.metrics._spike_stats(abs_deltas) -> RobustStats` (single shared impl; `position_metrics._fold_family` and `metrics.calculate_metrics` both call it — do NOT duplicate). Position families folded in `fold_metrics_into._fold_family` over the same `matched` lists as mean/max (so they inherit the 0044 state-diverged exclusion). `pnl_*` folded in `calculate_metrics` over per-trade `pnl_delta`.
- **Families instrumented (8 position + 1 trade)**: `cur_realised_usdt`, `pos_value_usdt`, `cum_realised_usdt`, `upnl_usdt`, `unrealised_pnl`, `liq_price`, `position_im`/`position_mm` (optional — issue #155 noise, folded for symmetry), and `pnl`. Price/qty keep mean/median/max only (out of scope).
- **Definitions**: `median = deltas[len//2]` (**upper-mid** index — NOT the averaging `_decimal_median`; the spike rules are calibrated to this exact index, keep the two helpers distinct); `p95 = deltas[int(len*0.95)]` clamped to `len-1` (defensive — the truncating index is already always ≤ len-1, so it never actually fires; prevents IndexError reasoning on tiny lists); `std = statistics.pstdev` (Decimal in → Decimal out, `0` for one element); `spike_intensity = max - median`; `spike_count_30c = #(|delta| > ABS_THRESHOLD)`; `spike_count_relative_3 = #(|delta| > REL_K*median)` **only when `median > 0`** (the `median==0` guard is mandatory — the relative test is otherwise trivially true for every positive delta → returns 0).
- **Comparator constants** (declarative, in `metrics.py`): `ABS_THRESHOLD = Decimal("0.30")`, `REL_K = Decimal("3")`. **Operator Layer 1–4 thresholds live HERE / in the external monitoring prompt, NOT in code** (comparator only emits metrics; operator applies the rule):
  | Layer | Rule (substitute the family prefix) | Meaning |
  |---|---|---|
  | 1. Spike (real) | `spike_intensity > $0.20` AND `spike_count_30c ≤ 3` | sharp peak, few snapshots |
  | 2. Drift (known) | `median > $0.10` AND `spike_count_30c > 10` | persistent gap — SHOW, don't flag if `bt_only > 0` |
  | 3. Heavy tail | `p95 > $0.50` AND `median < $0.10` | quiet baseline, hot tail — investigate |
  | 4. Volume floor | `position_pairs_compared < 20` | skip Layers 1–3 — too few samples for percentiles |

  For `cur_realised_usdt` Layer-1 replaces the old `max > $0.30` rule; for `pos_value_usdt` use `spike_intensity > $0.30` (cleaner than the old `> $0.50`).
- **Layer-shorthand → emitted-key**: the helper's internal `spike_count_abs`/`spike_count_rel` are exported as `<family>_spike_count_30c` / `<family>_spike_count_relative_3`; `median`→`<family>_median_abs_delta`, `spike_intensity`→`<family>_spike_intensity`. Never grep for a bare `spike_count_abs` key — it does not exist in `validation_metrics.csv`. Layer-2's `bt_only > 0` suppression qualifier maps to **`position_pairs_unmatched_bt`** (per-snapshot unmatched-backtest rows — the queue-priority accumulation the C108 example cites), NOT the trade-level `backtest_only_count`.
- **CSV/console**: `export_metrics` appends the six rows contiguous per family (after each `_mean/_max` pair; `pnl_*` after `cumulative_pnl_delta`, before `pnl_correlation` — no pnl mean/max anchor) via `_robust_stat_rows`. `print_summary` adds a grouped `POSITION ROBUST STATS` block (median/p95/max side-by-side) + a `PnL robust:` line. Additive only — no existing row removed/reordered; per-snapshot `position_comparison.csv` unchanged.

---

