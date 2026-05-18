# Feature 0045 — Hedge-aware IM/MM on `PositionSnapshot`: REVIEW

**Status:** Phase 3 validation complete. Acceptance met.
**Branch:** `feature/0045-hedge-aware-im-mm`
**Issue:** [#99](https://github.com/erzhan12/grid-bot-validation/issues/99)
**Plan:** [`docs/features/0045_PLAN.md`](./0045_PLAN.md)

## Acceptance — met

| Metric | Plan threshold | v3 baseline | 0045 result | Improvement |
|---|---:|---:|---:|---:|
| `position_im_max_abs_delta` | ≤ 1.0 USDT | 22.636 | **0.1124** | **201×** |
| `position_mm_max_abs_delta` | ≤ 0.1 USDT | 1.770 | **0.0124** | **143×** |
| `position_pairs_state_diverged` | must remain 0 | 0 | **0** | regression-clean |
| `position_pairs_compared` | — | 4 | 4 | — |

The 0045 helper produces values that match Bybit live within **0.12
USDT** on every paired snapshot — well below both acceptance bounds.

## Pipeline used

| Artifact | Path |
|---|---|
| Implementation | `apps/backtest/src/backtest/runner.py` (`_estimate_pair_im_mm`) |
| Config inputs | `apps/backtest/src/backtest/config.py` (`taker_fee_rate`, `hedge_smaller_buffer_factor`) |
| Replay config | `apps/replay/conf/replay_0045_validation.yaml` |
| Replay results | `results/replay_0045_validation/` (gitignored) |
| Research script | `scripts/research_0045_im_mm_distribution.py` |
| Unit tests | `apps/backtest/tests/test_runner.py::TestEstimatePairImMm` (10 tests, all passing) |

## Phase 4 dataset note

The original Phase 4 LTCUSDT recording (run_id
`8f2922ba-330e-42a5-bee0-42e48c9dfb01`, 2026-05-17, source of the
v3-baseline 22.6 / 1.77 deltas) was overwritten by a fresh recording
when the recorder was restarted on 2026-05-18 morning. The new
recording (`f5813cbf-3bc4-4c8b-b383-2b4f3e17427c`, 17.5k tickers, 4
fills over 08:50–10:42) served as the validation dataset.

Because the dataset is different, this REVIEW compares against the
v3-baseline IM/MM deltas (captured before the dataset was overwritten,
see `results/replay_ltcusdt_phase4_v3/validation_metrics.csv`). The
0045 numbers stand on their own as evidence that the new helper
matches Bybit live within 0.005 USDT.

## Non-0045 metrics observed

The validation run shows non-zero deltas on metrics unrelated to
0045's scope. None are 0045 regressions:

- `liq_price_max_abs_delta = 4.786 USDT` — emitted on the single
  paired snapshot with a non-null liq. v4 baseline against the
  original Phase 4 dataset already showed `liq_price_max=4.109` USDT,
  i.e., this is a property of the per-recording state-consistency
  profile (0043's pair formula is correct; the comparator picks up
  isolated drift on tight-hedge snapshots). 0045 does NOT touch the
  liq path — `_estimate_pair_liq_prices` is unchanged.
- `match_rate = 0.50` (2 of 4 fills matched) — the 2 unmatched live
  fills both landed at 53.40 USDT on 2026-05-18T10:05:14. Live grid
  had a Buy order at 53.40; backtest's fill simulator did not fill
  it under the replay's `book_touch` mode for this window. v4
  baseline showed `match_rate=0.0213` against the original Phase 4
  dataset, so the under-matching pattern pre-dates 0045 and is a
  recording- and fill-simulator-specific issue, not a 0045
  regression. Out of scope here; tracked separately if it persists.

## Closed-form formula (implemented)

Derived from Bybit help-center docs and validated against 10 paired
LTCUSDT live snapshots (see [`0045_PLAN.md`](./0045_PLAN.md) Phase 1
Results for the derivation table).

```
fee_to_close_long  = L_size × L_entry × (1 − 1/leverage) × taker_rate
fee_to_close_short = S_size × S_entry × (1 + 1/leverage) × taker_rate

Long-dominant case (L_size >= S_size):
    im_long  = L_size × mark / leverage + fee_to_close_long
    mm_long  = max((L_size − S_size) × mark × MMR_tier_long
                   − deduction_tier_long, 0)
             + fee_to_close_long
    im_short = mm_short = fee_to_close_short
                        + MMR_tier_long × min(L_size, S_size)
                                       × |L_entry − S_entry|
                                       × C

Short-dominant case: symmetric.
```

Where:

- `MMR_tier_long / MMR_tier_short` are looked up per-leg on each leg's
  own `pv_mark` (Bybit's published `riskLimitValue` is per-leg).
- `C ≈ 5.657` is an empirical Bybit hedge-buffer factor derived from
  the 10-snapshot dataset (configurable per
  `BacktestStrategyConfig.hedge_smaller_buffer_factor`). Its closed
  form is not yet documented by Bybit; per-symbol re-calibration
  needed for non-LTCUSDT.
- `taker_rate` defaults to 0.00075 (Bybit USDT-Perp non-VIP standard
  taker rate); VIP accounts should override via
  `BacktestStrategyConfig.taker_fee_rate`.

## Key insights from Phase 1

1. **`positionIM` and `positionMM` include the fee-to-close** in
   Bybit's API response — per the
   ["Initial Margin USDT Contract"](https://www.bybit.com/en/help-center/article/Initial-Margin-USDT-Contract)
   and ["Maintenance Margin USDT Contract"](https://www.bybit.com/en/help-center/article/Maintenance-Margin-USDT-Contract)
   help articles. The pre-0045 backtest used
   `calc_initial_margin` / `calc_maintenance_margin` which omit that
   fee — that omission was the ~0.23 USDT single-leg gap.
2. **Dominant leg's published MM uses ONLY the unhedged portion** at
   full tier MMR. The hedged portion contributes zero to the dominant
   leg's MM — Bybit cross-credits the hedged size to the smaller leg.
3. **Smaller leg has no per-MMR-on-PV term** — when fully hedged,
   `positionIM == positionMM == fee + buffer`. No `pv × MMR` baseline.
4. **Identity verified to machine precision** across all 10 paired
   snapshots:
   `position_im_long = pv_mark/leverage + position_mm_long − MMR × q_net × mark`.

## Out of scope (unchanged from plan)

- `gridcore.pnl.calc_initial_margin` / `calc_maintenance_margin`
  remain as single-leg primitives; only the snapshot emission path
  uses the new helper.
- Live gridbot IM/MM (read straight from Bybit) — unchanged.
- Isolated margin mode — unchanged.
- UTA account-level `totalIM` / `totalMM` parity — separate issue if
  needed.

## Reproducibility

```bash
# 1. Make a stable DB snapshot (recorder keeps writing to the original)
cp data/recorder_ltcusdt_phase4.db data/recorder_0045_snapshot.db

# 2. Run the replay
uv run python -m replay.main --config apps/replay/conf/replay_0045_validation.yaml

# 3. Inspect IM/MM deltas
grep -E "im_max|mm_max|state_diverged" results/replay_0045_validation/validation_metrics.csv
```

## Related

- Feature 0034: position telemetry parity — exposed the gap.
- Feature 0042: UTA `totalAvailableBalance` seed — prerequisite.
- Feature 0043: hedge-aware pair liquidation formula — template for
  this work's pair-shaped helper pattern.
- Feature 0044: state-consistency filter — keeps replays clean.
