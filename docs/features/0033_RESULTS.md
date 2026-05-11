# Feature 0033 — Parity smoke results

Run against `data/recorder_ltcusdt_phase4.db` (run_id
`b1f5b867-0c1c-4504-b48e-cc1d7a395eaf`, 138 closed executions,
2026-05-10 18:06:18Z → 2026-05-11 06:04:23Z).

## Smoke #1 — `mode: book_touch`

Config: `apps/replay/conf/replay_ltcusdt_phase4.yaml` (as committed).
Artefacts: `results/replay_ltcusdt_phase4.book_touch/`.

| Metric | Value | Target | Pass |
|---|---|---|---|
| match_rate | **0.9783** | ≥0.95 | ✅ |
| pnl_correlation | **0.9951** | ≥0.99 | ✅ |
| live_only_count | **3** | ≤4 | ✅ |
| backtest_only_count | **0** | ≤2 | ✅ |
| breaches_count | 1 | informational | — |
| meta.fill_mode | `book_touch` | emitted | ✅ |
| `summary.json` | present, run_id/start/end/symbol/fill_mode | required | ✅ |

## Smoke #2 — default (no `fill_simulator` block)

Config: `replay_ltcusdt_phase4.yaml` with the `fill_simulator` block
commented out. Exercises the default `FillMode.STRICT_CROSS` path —
must reproduce the pre-0033 baseline exactly.
Artefacts: `results/replay_ltcusdt_phase4.strict_cross_default/`.

| Metric | Value | Pre-0033 baseline | Match |
|---|---|---|---|
| match_rate | **0.9130** | 0.9130 | ✅ |
| pnl_correlation | **0.9838** | 0.9838 | ✅ |
| live_only_count | **12** | 12 | ✅ |
| backtest_only_count | **0** | 0 | ✅ |
| meta.fill_mode | `strict_cross` | n/a (new) | ✅ |

Backward compatibility holds: default replay output is metrically
identical to the pre-0033 strict-cross run, and the new
`meta.fill_mode` row records which simulator produced the run.

## Conclusion

Feature 0029 Phase 4 closed. The 91.3% → 97.8% lift comes entirely
from `book_touch` recovering at-limit fills that `strict_cross` was
discarding (last_price == limit_price → no fill, even when the live
order book absorbed the order at L1). Default mode remains
`strict_cross` so existing backtest consumers are unaffected.

The 3 residual `live_only` and PnL delta of −0.013 are most likely
matcher cascade artefacts in `(client_order_id, occurrence)` — small
timing/qty skews that desync the occurrence counters. Tracked as a
follow-up candidate (stable bipartite matching in comparator); not
required for 0033 acceptance.
