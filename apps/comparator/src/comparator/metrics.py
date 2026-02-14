"""Validation metrics for backtest-vs-live comparison.

Computes per-trade deltas and aggregate accuracy metrics from matched trade pairs.
"""

import logging
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

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
    """Compute median of Decimal list."""
    if not values:
        return Decimal("0")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 1:
        return sorted_vals[n // 2]
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2


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
