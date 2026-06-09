"""Validation metrics for backtest-vs-live comparison.

Computes per-trade deltas and aggregate accuracy metrics from matched trade pairs.
"""

import logging
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import NamedTuple

from gridcore.position import DirectionType

from comparator.matcher import MatchResult, MatchedTrade

logger = logging.getLogger(__name__)


@dataclass
class TradeDelta:
    """Per-trade difference between backtest and live."""

    client_order_id: str
    price_delta: Decimal
    qty_delta: Decimal
    fee_delta: Decimal
    pnl_delta: Decimal
    time_delta: timedelta


@dataclass
class ValidationMetrics:
    """Aggregate validation metrics."""

    # Coverage
    total_live_trades: int = 0
    total_backtest_trades: int = 0
    matched_count: int = 0
    live_only_count: int = 0
    backtest_only_count: int = 0
    match_rate: float = 0.0
    phantom_rate: float = 0.0

    # Price accuracy (across matched pairs)
    price_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    price_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    price_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))

    # Quantity accuracy
    qty_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    qty_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    qty_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))

    # Fee accuracy
    total_live_fees: Decimal = field(default_factory=lambda: Decimal("0"))
    total_backtest_fees: Decimal = field(default_factory=lambda: Decimal("0"))
    fee_delta: Decimal = field(default_factory=lambda: Decimal("0"))

    # PnL accuracy
    total_live_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    total_backtest_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    cumulative_pnl_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pnl_correlation: float = 0.0

    # 0070: robust spike-vs-drift stats for the trade-level PnL family
    # (issue #156, "same treatment for symmetry"). Folds over the per-trade
    # pnl_delta list in calculate_metrics via _spike_stats. spike_count_* are
    # integer counts; median/p95/std/spike_intensity are Decimal. Price/qty
    # keep their existing mean/median/max only — full robust stats there are
    # out of minimum scope. See docs/features/0070_PLAN.md.
    pnl_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pnl_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pnl_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pnl_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    pnl_spike_count_30c: int = 0
    pnl_spike_count_relative_3: int = 0

    # Volume
    total_live_volume: Decimal = field(default_factory=lambda: Decimal("0"))
    total_backtest_volume: Decimal = field(default_factory=lambda: Decimal("0"))

    # Direction breakdown
    long_match_count: int = 0
    short_match_count: int = 0
    long_pnl_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    short_pnl_delta: Decimal = field(default_factory=lambda: Decimal("0"))

    # Timing
    mean_time_delta_seconds: float = 0.0

    # Tolerance breaches
    breaches_count: int = 0
    breaches: list[tuple[str, int]] = field(default_factory=list)  # (client_order_id, occurrence) pairs exceeding tolerance

    # Equity curve comparison
    equity_max_divergence: Decimal = field(default_factory=lambda: Decimal("0"))
    equity_mean_divergence: Decimal = field(default_factory=lambda: Decimal("0"))
    equity_correlation: float = 0.0

    # Per-trade deltas (for detailed export)
    trade_deltas: list[TradeDelta] = field(default_factory=list)

    # 0034: position telemetry parity (live vs backtest position snapshots).
    # Sign convention: backtest minus live. Positive = backtest over-estimates.
    # NULL telemetry fields are skipped per-field, never treated as zero.
    position_im_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_im_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_mm_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_mm_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    liq_price_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    liq_price_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealised_pnl_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealised_pnl_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cum_realised_pnl_final_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    # 0056: cycle-scoped realized PnL parity. Sum of the last per-side delta
    # across matched pairs — captures either the in-progress cycle or the
    # just-closed cycle total at session end (Bybit holds the closed total
    # until the next opening fill).
    cur_realised_pnl_final_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    # 0059: per-snapshot parity for the four USDT values the 0058 log line
    # emits (upnl_usdt, cur/cum realised, pos_value). Unlike the *_final_delta
    # fields above (last per-side value only), these sum |delta| across ALL
    # matched pairs so intermediate drift that cancels out at session end is
    # still visible. upnl_usdt_* compares the STORED unrealised_pnl 1:1 (not
    # the recompute-vs-mark unrealised_pnl_* family). cur/cum_realised_usdt_*
    # are the per-snapshot complements of the cur/cum_realised_pnl_final_delta
    # scalars. pos_value_final_delta mirrors the cur/cum final-delta scalars.
    upnl_usdt_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    upnl_usdt_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cur_realised_usdt_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cur_realised_usdt_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cum_realised_usdt_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cum_realised_usdt_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pos_value_usdt_mean_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pos_value_usdt_max_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pos_value_final_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_pairs_compared: int = 0
    position_pairs_unmatched_bt: int = 0
    position_pairs_missing_telemetry: int = 0
    # 0044: pairs matched by exchange_ts but where backtest state has drifted
    # from live (size or entry beyond tolerance). Excluded from delta
    # aggregates so operator-induced artefacts (e.g. manual fills not in the
    # grid replay) don't pollute the headline metrics. See
    # PositionComparator.state_size_tolerance / state_entry_rel_tolerance and
    # docs/features/0044_PLAN.md.
    position_pairs_state_diverged: int = 0

    # 0070: robust spike-vs-drift stats per per-snapshot abs-delta family
    # (issue #156). Six stats each — median / p95 / std (Decimal),
    # spike_intensity (= max - median, Decimal), spike_count_30c (|delta| >
    # ABS_THRESHOLD, int) and spike_count_relative_3 (|delta| > REL_K x median,
    # int). Folded in PositionComparator.fold_metrics_into via _spike_stats over
    # the SAME matched-pair lists the mean/max aggregates use, so they inherit
    # the 0044 state-diverged exclusion. position_im_* / position_mm_* are
    # OPTIONAL (issue #155: already noisy) but instrumented for symmetry.
    cur_realised_usdt_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cur_realised_usdt_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cur_realised_usdt_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cur_realised_usdt_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    cur_realised_usdt_spike_count_30c: int = 0
    cur_realised_usdt_spike_count_relative_3: int = 0
    pos_value_usdt_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pos_value_usdt_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pos_value_usdt_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    pos_value_usdt_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    pos_value_usdt_spike_count_30c: int = 0
    pos_value_usdt_spike_count_relative_3: int = 0
    cum_realised_usdt_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cum_realised_usdt_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cum_realised_usdt_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    cum_realised_usdt_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    cum_realised_usdt_spike_count_30c: int = 0
    cum_realised_usdt_spike_count_relative_3: int = 0
    upnl_usdt_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    upnl_usdt_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    upnl_usdt_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    upnl_usdt_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    upnl_usdt_spike_count_30c: int = 0
    upnl_usdt_spike_count_relative_3: int = 0
    unrealised_pnl_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealised_pnl_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealised_pnl_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealised_pnl_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealised_pnl_spike_count_30c: int = 0
    unrealised_pnl_spike_count_relative_3: int = 0
    liq_price_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    liq_price_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    liq_price_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    liq_price_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    liq_price_spike_count_30c: int = 0
    liq_price_spike_count_relative_3: int = 0
    # Optional (issue #155 noise) — folded for symmetric coverage.
    position_im_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_im_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_im_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_im_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    position_im_spike_count_30c: int = 0
    position_im_spike_count_relative_3: int = 0
    position_mm_median_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_mm_p95_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_mm_std_abs_delta: Decimal = field(default_factory=lambda: Decimal("0"))
    position_mm_spike_intensity: Decimal = field(default_factory=lambda: Decimal("0"))
    position_mm_spike_count_30c: int = 0
    position_mm_spike_count_relative_3: int = 0

    # 0065: non-USDT collateral re-mark attribution. Computed replay-side from
    # BacktestSession + WalletSeed (NOT in PositionComparator.fold_metrics_into).
    # non_usdt_collateral_drift_total is the modelled re-mark delta
    # (collateral_now - seed_contrib) at the last processed tick — one additive
    # component already folded into backtest total_equity (Phase 2A), NOT the
    # acceptance-#3a gap. collateral_drift_by_coin breaks it down per coin. The
    # three list fields surface under-coverage (coins live includes but the
    # backtest could not model / re-marked metadata).
    non_usdt_collateral_drift_total: Decimal = field(
        default_factory=lambda: Decimal("0")
    )
    collateral_drift_by_coin: dict[str, Decimal] = field(default_factory=dict)
    collateral_excluded_coins: list[str] = field(default_factory=list)
    collateral_missing_mark_coins: list[str] = field(default_factory=list)
    collateral_switch_off_coins: list[str] = field(default_factory=list)


def _compute_trade_delta(pair: MatchedTrade) -> TradeDelta:
    """Compute delta between a matched pair."""
    return TradeDelta(
        client_order_id=pair.live.client_order_id,
        price_delta=pair.backtest.price - pair.live.price,
        qty_delta=pair.backtest.qty - pair.live.qty,
        fee_delta=pair.backtest.fee - pair.live.fee,
        pnl_delta=pair.backtest.realized_pnl - pair.live.realized_pnl,
        time_delta=pair.backtest.timestamp - pair.live.timestamp,
    )


def _pearson_correlation(xs: list[float], ys: list[float]) -> float:
    """Compute Pearson correlation coefficient.

    Returns 0.0 if insufficient data or zero variance.
    """
    n = len(xs)
    if n < 2:
        return 0.0

    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)

    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)

    denom = (var_x * var_y) ** 0.5
    if denom == 0:
        return 0.0

    return cov / denom


def _decimal_median(values: list[Decimal]) -> Decimal:
    """Compute median of Decimal list (averages the two middle on even counts).

    NOTE: deliberately distinct from the 0070 ``_spike_stats`` median, which
    uses the bare upper-mid index ``deltas[len // 2]`` (no even-count
    averaging) because the spike-vs-drift rules are calibrated to that exact
    index. Keep the two helpers separate.
    """
    if not values:
        return Decimal("0")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 1:
        return sorted_vals[n // 2]
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2


# ---------------------------------------------------------------------------
# 0070: robust spike-vs-drift statistics (issue #156).
#
# These two constants are the comparator-side declarative defaults for the
# exported spike COUNTS. The Layer 1-4 OPERATOR thresholds
# ($0.20 / $0.10 / $0.50, counts 3/10, matched<20 volume floor) live in
# RULES.md and the external monitoring prompt, NOT here — the comparator only
# emits the metrics; the operator applies the layered rule. See
# docs/features/0070_PLAN.md.
# ---------------------------------------------------------------------------
ABS_THRESHOLD = Decimal("0.30")
"""Fixed absolute spike threshold for ``spike_count_30c`` (the legacy >$0.30 gate)."""

REL_K = Decimal("3")
"""Relative-outlier multiplier for ``spike_count_relative_3`` (> REL_K x median)."""


class RobustStats(NamedTuple):
    """Six robust order statistics over a list of abs-deltas (feature 0070).

    Spike-vs-drift primitive: ``spike_intensity`` (= max - median) is large
    only when a few snapshots tower over the baseline (a real divergence
    event); sustained drift instead lifts ``median`` while keeping
    ``spike_intensity`` near zero. ``spike_count_abs`` is exported as
    ``<family>_spike_count_30c`` and ``spike_count_rel`` as
    ``<family>_spike_count_relative_3`` (see RULES.md mapping table).
    """

    median: Decimal
    p95: Decimal
    std: Decimal
    spike_intensity: Decimal
    spike_count_abs: int
    spike_count_rel: int


def _spike_stats(
    abs_deltas: list[Decimal],
    abs_threshold: Decimal = ABS_THRESHOLD,
    rel_k: Decimal = REL_K,
) -> RobustStats:
    """Compute the six robust stats over already-abs, None-filtered deltas.

    Mirrors the issue #156 pseudocode with project-correct Decimal handling:

    * empty list -> all six zero (the all-flat / no-data case; "no anomaly").
    * ``median = deltas[len // 2]`` -- the **upper-mid** element on even counts
      (NOT the averaged ``_decimal_median``); the spike rules are calibrated to
      this exact index.
    * ``p95 = deltas[int(len * 0.95)]`` -- clamped to the last index so lists of
      length 1-20 never raise ``IndexError``. (The truncating index is already
      always <= len-1, so the clamp is defensive / never actually fires.)
    * ``std = statistics.pstdev`` -- population stdev; ``Decimal`` in, ``Decimal``
      out; returns ``Decimal("0")`` for a single element (no raise).
    * ``spike_intensity = max - median`` -- the silhouette of the peak.
    * ``spike_count_abs`` counts ``d > abs_threshold`` (fixed $0.30 gate;
      boundary 0.30 excluded).
    * ``spike_count_rel`` counts ``d > rel_k * median`` ONLY when ``median > 0``
      (the median==0 guard: the relative test is undefined / trivially true for
      every positive delta otherwise, so it returns 0).
    """
    if not abs_deltas:
        return RobustStats(
            Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 0, 0,
        )
    deltas = sorted(abs_deltas)
    n = len(deltas)
    median = deltas[n // 2]
    p95 = deltas[min(int(n * 0.95), n - 1)]
    std = statistics.pstdev(deltas)
    spike_intensity = deltas[-1] - median
    spike_count_abs = sum(1 for d in deltas if d > abs_threshold)
    spike_count_rel = (
        sum(1 for d in deltas if d > rel_k * median) if median > 0 else 0
    )
    return RobustStats(
        median, p95, std, spike_intensity, spike_count_abs, spike_count_rel,
    )


def calculate_metrics(
    match_result: MatchResult,
    price_tolerance: Decimal = Decimal("0"),
    qty_tolerance: Decimal = Decimal("0.001"),
) -> ValidationMetrics:
    """Calculate validation metrics from match result.

    Args:
        match_result: Output from TradeMatcher.match().
        price_tolerance: Flag trades with price delta exceeding this.
        qty_tolerance: Flag trades with qty delta exceeding this.

    Returns:
        ValidationMetrics with all aggregate and per-trade metrics.
    """
    metrics = ValidationMetrics()

    total_live = len(match_result.matched) + len(match_result.live_only)
    total_bt = len(match_result.matched) + len(match_result.backtest_only)

    metrics.total_live_trades = total_live
    metrics.total_backtest_trades = total_bt
    metrics.matched_count = len(match_result.matched)
    metrics.live_only_count = len(match_result.live_only)
    metrics.backtest_only_count = len(match_result.backtest_only)
    metrics.match_rate = metrics.matched_count / total_live if total_live else 0.0
    metrics.phantom_rate = metrics.backtest_only_count / total_bt if total_bt else 0.0

    # Volume from unmatched trades (always computed, even with no matched pairs)
    for t in match_result.live_only:
        metrics.total_live_volume += t.qty
    for t in match_result.backtest_only:
        metrics.total_backtest_volume += t.qty

    if not match_result.matched:
        return metrics

    # Compute per-trade deltas
    deltas = [_compute_trade_delta(pair) for pair in match_result.matched]
    metrics.trade_deltas = deltas

    # Price accuracy
    abs_price_deltas = [abs(d.price_delta) for d in deltas]
    metrics.price_mean_abs_delta = Decimal(str(statistics.mean(abs_price_deltas)))
    metrics.price_median_abs_delta = _decimal_median(abs_price_deltas)
    metrics.price_max_abs_delta = max(abs_price_deltas)

    # Qty accuracy
    abs_qty_deltas = [abs(d.qty_delta) for d in deltas]
    metrics.qty_mean_abs_delta = Decimal(str(statistics.mean(abs_qty_deltas)))
    metrics.qty_median_abs_delta = _decimal_median(abs_qty_deltas)
    metrics.qty_max_abs_delta = max(abs_qty_deltas)

    # 0070: robust spike-vs-drift stats over the per-trade PnL deltas, symmetric
    # to the per-snapshot families (issue #156). Reached only when matched is
    # non-empty (guarded above), so empty runs keep the Decimal("0") / 0
    # defaults. Price/qty intentionally keep mean/median/max only.
    abs_pnl_deltas = [abs(d.pnl_delta) for d in deltas]
    pnl_stats = _spike_stats(abs_pnl_deltas)
    metrics.pnl_median_abs_delta = pnl_stats.median
    metrics.pnl_p95_abs_delta = pnl_stats.p95
    metrics.pnl_std_abs_delta = pnl_stats.std
    metrics.pnl_spike_intensity = pnl_stats.spike_intensity
    metrics.pnl_spike_count_30c = pnl_stats.spike_count_abs
    metrics.pnl_spike_count_relative_3 = pnl_stats.spike_count_rel

    # Tolerance breaches
    # tolerance=0 means exact match required (flag any non-zero delta)
    for pair, delta in zip(match_result.matched, deltas):
        exceeded = False
        if abs(delta.price_delta) > price_tolerance:
            exceeded = True
        if abs(delta.qty_delta) > qty_tolerance:
            exceeded = True
        if exceeded:
            metrics.breaches.append((delta.client_order_id, pair.live.occurrence))
    metrics.breaches_count = len(metrics.breaches)

    # Fee totals
    for pair in match_result.matched:
        metrics.total_live_fees += pair.live.fee
        metrics.total_backtest_fees += pair.backtest.fee
    metrics.fee_delta = metrics.total_backtest_fees - metrics.total_live_fees

    # PnL totals and correlation
    live_cum_pnl: list[float] = []
    bt_cum_pnl: list[float] = []
    live_running = Decimal("0")
    bt_running = Decimal("0")

    # Sort matched pairs by live timestamp for cumulative PnL curve
    sorted_pairs = sorted(match_result.matched, key=lambda p: p.live.timestamp)
    for pair in sorted_pairs:
        live_running += pair.live.realized_pnl
        bt_running += pair.backtest.realized_pnl
        live_cum_pnl.append(float(live_running))
        bt_cum_pnl.append(float(bt_running))

    metrics.total_live_pnl = live_running
    metrics.total_backtest_pnl = bt_running
    metrics.cumulative_pnl_delta = bt_running - live_running
    metrics.pnl_correlation = _pearson_correlation(live_cum_pnl, bt_cum_pnl)

    # Volume from matched pairs
    for pair in match_result.matched:
        metrics.total_live_volume += pair.live.qty
        metrics.total_backtest_volume += pair.backtest.qty

    # Direction breakdown — prefer backtest direction (always correct from
    # BacktestTrade.direction) over inferred live direction (fragile for
    # break-even closes where closed_pnl==0 is misclassified as opening).
    for pair, delta in zip(match_result.matched, deltas):
        direction = pair.backtest.direction or pair.live.direction
        if direction == DirectionType.LONG:
            metrics.long_match_count += 1
            metrics.long_pnl_delta += delta.pnl_delta
        else:
            metrics.short_match_count += 1
            metrics.short_pnl_delta += delta.pnl_delta

    # Timing
    time_deltas_sec = [abs(d.time_delta.total_seconds()) for d in deltas]
    metrics.mean_time_delta_seconds = statistics.mean(time_deltas_sec)

    return metrics
