"""Verdict evaluation for live_check — the four threshold checks.

Pure functions over already-loaded data; no DB access.
"""

from dataclasses import dataclass
from decimal import Decimal

from live_check.config import VerdictThresholds
from live_check.ground_truth import GroundTruth


@dataclass(frozen=True)
class Verdict:
    """Outcome of the four reconcile checks for one strat/window."""

    live_only_count: int
    backtest_only_count: int
    matched_count: int
    live_exec_count: int  # informational display only (raw exec rows)
    d_realized: Decimal
    d_commission: Decimal
    d_unrealised: Decimal
    matched_ok: bool
    realized_ok: bool
    commission_ok: bool
    unrealised_ok: bool
    passed: bool


def evaluate(
    replay_result,
    ground_truth: GroundTruth,
    thresholds: VerdictThresholds,
) -> Verdict:
    """Apply the four verdict checks to one strat's replay vs ground truth.

    The matched check is structural: ``match_result.live_only == [] AND
    match_result.backtest_only == []``. It is NOT ``matched_count ==
    live_exec_count`` — ``matched`` counts aggregated NormalizedTrades while
    ``live_exec_count`` counts RAW execution rows; partial fills make them
    legitimately differ on a correct run.

    Replay unrealised is read from
    ``replay_result.session.metrics.total_unrealized_pnl`` (the finalized
    ``BacktestMetrics``) — ``ReplayResult.metrics`` is a comparator
    ``ValidationMetrics`` with no such field, and ``session`` has no
    top-level ``total_unrealized_pnl`` attribute.

    Args:
        replay_result: ``ReplayResult`` from a finalized engine run.
        ground_truth: Recorded sums for the same strat/window.
        thresholds: Pass/fail deltas.

    Returns:
        Verdict with per-check flags and the overall pass.

    Raises:
        ValueError: When ``replay_result.session.metrics`` is None (result
            built without ``finalize()``).
    """
    session_metrics = replay_result.session.metrics
    if session_metrics is None:
        raise ValueError(
            "replay_result.session.metrics is None — the session was not "
            "finalized; ReplayEngine.run() calls finalize() before returning, "
            "so this result did not come from a completed run."
        )

    match_result = replay_result.match_result
    live_only_count = len(match_result.live_only)
    backtest_only_count = len(match_result.backtest_only)
    matched_ok = live_only_count == 0 and backtest_only_count == 0

    d_realized = session_metrics.total_realized_pnl - ground_truth.sum_realized
    d_commission = session_metrics.total_commission - ground_truth.sum_commission
    d_unrealised = (
        session_metrics.total_unrealized_pnl - ground_truth.net_unrealised
    )

    realized_ok = abs(d_realized) < thresholds.realized
    commission_ok = abs(d_commission) < thresholds.commission
    unrealised_ok = abs(d_unrealised) < thresholds.unrealised

    return Verdict(
        live_only_count=live_only_count,
        backtest_only_count=backtest_only_count,
        matched_count=len(match_result.matched),
        live_exec_count=ground_truth.live_exec_count,
        d_realized=d_realized,
        d_commission=d_commission,
        d_unrealised=d_unrealised,
        matched_ok=matched_ok,
        realized_ok=realized_ok,
        commission_ok=commission_ok,
        unrealised_ok=unrealised_ok,
        passed=matched_ok and realized_ok and commission_ok and unrealised_ok,
    )
