"""Smoke tests for the four live_check render modes."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from live_check.ground_truth import ExecRow
from live_check.render import (
    render_curve,
    render_once,
    render_per_fill,
    render_shared_wallet,
    render_watch_line,
)
from live_check.shared_wallet import SharedWalletDiff
from live_check.verdict import SharedWalletVerdict
from live_check.verdict import Verdict


def _verdict(passed=True):
    ok = passed
    return Verdict(
        live_only_count=0 if ok else 1,
        backtest_only_count=0,
        matched_count=2,
        live_exec_count=3,
        d_realized=Decimal("0.001"),
        d_commission=Decimal("-0.002"),
        d_unrealised=Decimal("0.1"),
        matched_ok=ok,
        realized_ok=True,
        commission_ok=True,
        unrealised_ok=True,
        passed=ok,
    )


def _strat():
    return SimpleNamespace(strat_id="ltcusdt_test", symbol="LTCUSDT")


def _trade(link, price="80", qty="0.2", pnl="0.5", ts=None, order_id="O1"):
    return SimpleNamespace(
        client_order_id=link,
        order_id=order_id,
        price=Decimal(price),
        qty=Decimal(qty),
        realized_pnl=Decimal(pnl),
        timestamp=ts or datetime(2026, 7, 1, 12, 0, 0),
    )


def _matched_pair(link, live_pnl, bt_pnl, ts):
    return SimpleNamespace(
        live=SimpleNamespace(
            client_order_id=link,
            realized_pnl=Decimal(live_pnl),
            timestamp=ts,
        ),
        backtest=SimpleNamespace(realized_pnl=Decimal(bt_pnl)),
    )


def _result(trades=(), matched=()):
    return SimpleNamespace(
        session=SimpleNamespace(trades=list(trades)),
        match_result=SimpleNamespace(matched=list(matched)),
    )


class TestRenderOnce:
    def test_pass_block(self):
        """Summary block carries strat, verdict, and all three deltas."""
        out = render_once([(_strat(), _verdict(True), _result())])
        assert "ltcusdt_test" in out
        assert "PASS" in out
        assert "Δrealized" in out
        assert "Δcommission" in out
        assert "Δunrealised" in out

    def test_fail_block(self):
        """FAIL verdict labelled FAIL."""
        out = render_once([(_strat(), _verdict(False), _result())])
        assert "FAIL" in out

    def test_shared_wallet_margin_lines_are_informational(self):
        """0095 fallback render labels demoted margin gates as INFO."""
        diff = SharedWalletDiff(
            max_equity_delta=Decimal("0"),
            final_equity_delta=Decimal("0"),
            max_margin_balance_delta=Decimal("500"),
            max_account_mm_rate_delta=Decimal("0.5"),
            equity_points=1,
            margin_balance_points=1,
            account_mm_rate_points=1,
        )
        verdict = SharedWalletVerdict(
            per_strat={"ltcusdt_test": _verdict(True)},
            wallet_diff=diff,
            equity_ok=True,
            total_margin_balance_ok=False,
            account_mm_rate_ok=False,
            passed=True,
        )
        out = render_shared_wallet([(_strat(), _verdict(True), _result())], verdict)
        assert "max Δmargin_balance" in out
        assert "max Δaccount_mm_rate" in out
        assert out.count("INFO") == 2


class TestRenderWatchLine:
    def test_one_line_per_strat(self):
        """Traffic-light line has matched count and the three deltas."""
        out = render_watch_line([(_strat(), _verdict(True), _result())])
        assert out.count("\n") == 0
        assert "matched=2" in out
        assert "Δr=" in out and "Δc=" in out and "Δu=" in out


class TestRenderPerFill:
    def test_partial_fills_pair_against_one_rollup_trade(self):
        """Two partial execs pair with ONE aggregated bt trade and flag ✓.

        event_follower flushes one BacktestTrade per order lifecycle (VWAP
        price, summed qty/pnl) keyed (link prefix, order_id); the live
        orderLinkId carries a -{millis} suffix that must be stripped for
        the keys to meet. Sequential raw-link pairing would false-✗ here.
        """
        ts = datetime(2026, 7, 1, 12, 0, 0)
        execs = [
            ExecRow("exec-1", ts, "Buy", Decimal("80"), Decimal("0.1"),
                    Decimal("0"), "L1-1751371200000", "O1"),
            ExecRow("exec-2", ts, "Buy", Decimal("80"), Decimal("0.1"),
                    Decimal("0"), "L1-1751371200000", "O1"),
        ]
        trades = [_trade("L1", qty="0.2", pnl="0", order_id="O1")]
        out = render_per_fill(
            [(_strat(), _verdict(True), _result(trades=trades), execs)]
        )
        assert "exec_id" in out  # header
        assert "exec-1" in out and "exec-2" in out
        assert "✗" not in out  # aggregate matches → every row ✓

    def test_aggregate_mismatch_flags_group(self):
        """Group qty sum ≠ bt trade qty → the group's rows flag ✗."""
        ts = datetime(2026, 7, 1, 12, 0, 0)
        execs = [
            ExecRow("exec-1", ts, "Buy", Decimal("80"), Decimal("0.1"),
                    Decimal("0"), "L1-1751371200000", "O1"),
        ]
        trades = [_trade("L1", qty="0.2", pnl="0", order_id="O1")]
        out = render_per_fill(
            [(_strat(), _verdict(False), _result(trades=trades), execs)]
        )
        assert "✗" in out

    def test_multi_price_partials_match_vwap(self):
        """Partials at DIFFERENT prices pair via the rollup's VWAP price."""
        ts = datetime(2026, 7, 1, 12, 0, 0)
        execs = [
            ExecRow("exec-1", ts, "Buy", Decimal("80"), Decimal("0.1"),
                    Decimal("0"), "L1-1751371200000", "O1"),
            ExecRow("exec-2", ts, "Buy", Decimal("81"), Decimal("0.1"),
                    Decimal("0"), "L1-1751371200000", "O1"),
        ]
        # rollup VWAP = (80*0.1 + 81*0.1) / 0.2 = 80.5
        trades = [_trade("L1", price="80.5", qty="0.2", pnl="0", order_id="O1")]
        out = render_per_fill(
            [(_strat(), _verdict(True), _result(trades=trades), execs)]
        )
        assert "✗" not in out

    def test_null_link_falls_back_to_order_id(self):
        """NULL order_link_id pairs via order_id (no '' key collision)."""
        ts = datetime(2026, 7, 1, 12, 0, 0)
        execs = [
            ExecRow("exec-1", ts, "Buy", Decimal("80"), Decimal("0.2"),
                    Decimal("0"), None, "O9"),
        ]
        trades = [_trade("O9", qty="0.2", pnl="0", order_id="O9")]
        out = render_per_fill(
            [(_strat(), _verdict(True), _result(trades=trades), execs)]
        )
        assert "✗" not in out

    def test_unmatched_exec_renders_dash_row(self):
        """A live exec with no bt fill renders a placeholder row, no crash."""
        ts = datetime(2026, 7, 1, 12, 0, 0)
        execs = [ExecRow("exec-1", ts, "Sell", Decimal("81"), Decimal("0.2"),
                         None, "orphan-123", "O2")]
        out = render_per_fill(
            [(_strat(), _verdict(False), _result(), execs)]
        )
        assert "exec-1" in out
        assert "✗" in out


class TestRenderCurve:
    def test_matched_pair_grain_and_csv(self, tmp_path):
        """Both series walk the same matched pairs; CSV exported per strat."""
        t0 = datetime(2026, 7, 1, 12, 0, 0)
        pairs = [
            _matched_pair("L1", "0.5", "0.5", t0),
            _matched_pair("L2", "-0.2", "-0.2", t0.replace(minute=5)),
        ]
        out = render_curve(
            [(_strat(), _verdict(True), _result(matched=pairs))],
            csv_dir=str(tmp_path),
        )
        assert "live" in out and "replay" in out
        assert "2 matched pairs" in out
        csv_file = tmp_path / "curve_ltcusdt_test.csv"
        assert csv_file.exists()
        content = csv_file.read_text()
        assert "live_cum" in content and "replay_cum" in content

    def test_empty_matched_no_crash(self):
        """Zero matched pairs renders an empty-series marker."""
        out = render_curve([(_strat(), _verdict(True), _result())])
        assert "no data" in out
