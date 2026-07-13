"""Tests for live_check.verdict — the four threshold checks."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from live_check.config import VerdictThresholds
from live_check.ground_truth import GroundTruth
from live_check.verdict import evaluate

_ZERO = Decimal("0")


def _truth(realized="0", commission="0", unrealised="0", count=3):
    return GroundTruth(
        sum_realized=Decimal(realized),
        sum_commission=Decimal(commission),
        net_unrealised=Decimal(unrealised),
        live_exec_count=count,
    )


def _rr(realized="0", commission="0", unrealised="0",
        matched=3, live_only=0, backtest_only=0, metrics_none=False):
    """Synthetic ReplayResult stub.

    ``replay_result.metrics`` (the comparator ValidationMetrics) deliberately
    has NO total_unrealized_pnl attribute — evaluate must never read it.
    ``session`` likewise has no top-level total_unrealized_pnl.
    """
    session_metrics = None if metrics_none else SimpleNamespace(
        total_realized_pnl=Decimal(realized),
        total_commission=Decimal(commission),
        total_unrealized_pnl=Decimal(unrealised),
    )
    return SimpleNamespace(
        session=SimpleNamespace(metrics=session_metrics),
        metrics=SimpleNamespace(),  # ValidationMetrics stand-in, no pnl fields
        match_result=SimpleNamespace(
            matched=[object()] * matched,
            live_only=[object()] * live_only,
            backtest_only=[object()] * backtest_only,
        ),
    )


class TestVerdictThresholds:
    def test_all_pass(self):
        """Exact parity on all four checks → PASS."""
        v = evaluate(_rr(), _truth(), VerdictThresholds())
        assert v.passed
        assert v.matched_ok and v.realized_ok and v.commission_ok
        assert v.unrealised_ok

    @pytest.mark.parametrize("delta,ok", [("0.009", True), ("0.01", False)])
    def test_realized_boundary(self, delta, ok):
        """|Δrealized| strictly < 0.01 passes; == 0.01 fails."""
        v = evaluate(_rr(realized=delta), _truth(), VerdictThresholds())
        assert v.realized_ok is ok
        assert v.passed is ok

    @pytest.mark.parametrize("delta,ok", [("-0.009", True), ("-0.01", False)])
    def test_commission_boundary(self, delta, ok):
        """|Δcommission| strictly < 0.01 passes (abs of negative delta)."""
        v = evaluate(_rr(commission=delta), _truth(), VerdictThresholds())
        assert v.commission_ok is ok

    @pytest.mark.parametrize("delta,ok", [("0.49", True), ("0.50", False)])
    def test_unrealised_boundary(self, delta, ok):
        """|Δunrealised| strictly < 0.50 passes."""
        v = evaluate(_rr(unrealised=delta), _truth(), VerdictThresholds())
        assert v.unrealised_ok is ok

    def test_live_only_fails_matched_check(self):
        """Any live_only trade fails the matched verdict."""
        v = evaluate(_rr(live_only=1), _truth(), VerdictThresholds())
        assert not v.matched_ok
        assert not v.passed
        assert v.live_only_count == 1

    def test_backtest_only_fails_matched_check(self):
        """Any backtest_only trade fails the matched verdict."""
        v = evaluate(_rr(backtest_only=2), _truth(), VerdictThresholds())
        assert not v.matched_ok
        assert v.backtest_only_count == 2


class TestMatchedGrain:
    def test_partial_fill_aggregation_still_passes(self):
        """Raw live_exec_count > matched_count must NOT fail a correct run.

        Partial fills aggregate several raw execs into one NormalizedTrade,
        so the gate is live_only/backtest_only == [], never
        matched_count == live_exec_count.
        """
        v = evaluate(
            _rr(matched=3, live_only=0, backtest_only=0),
            _truth(count=5),
            VerdictThresholds(),
        )
        assert v.live_exec_count == 5
        assert v.matched_count == 3
        assert v.passed

    def test_live_exec_count_is_display_only(self):
        """live_exec_count is carried on the Verdict but never gates it."""
        v = evaluate(_rr(matched=1), _truth(count=100), VerdictThresholds())
        assert v.passed


class TestUnrealisedSource:
    def test_reads_session_metrics_total_unrealized(self):
        """d_unrealised comes from session.metrics.total_unrealized_pnl.

        The stub's ReplayResult.metrics has no such field and session has no
        top-level attribute — an implementation reading either would crash.
        """
        rr = _rr(unrealised="0.2")
        v = evaluate(rr, _truth(unrealised="0.1"), VerdictThresholds())
        assert v.d_unrealised == Decimal("0.1")

    def test_metrics_none_raises_explicit_guard(self):
        """session.metrics None → explicit ValueError, not AttributeError."""
        with pytest.raises(ValueError, match="not.*finalized|finalize"):
            evaluate(_rr(metrics_none=True), _truth(), VerdictThresholds())
