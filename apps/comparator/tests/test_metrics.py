"""Tests for comparator.metrics module."""

from datetime import timedelta
from decimal import Decimal

import pytest

from comparator.matcher import MatchedTrade, MatchResult
from comparator.metrics import (
    ABS_THRESHOLD,
    REL_K,
    calculate_metrics,
    _compute_trade_delta,
    _pearson_correlation,
    _decimal_median,
    _spike_stats,
)


class TestComputeTradeDelta:
    """Tests for _compute_trade_delta."""

    def test_identical_trades(self, make_trade, ts):
        """Identical trades produce zero deltas."""
        live = make_trade(source="live", timestamp=ts)
        bt = make_trade(source="backtest", timestamp=ts)
        pair = MatchedTrade(live=live, backtest=bt)

        delta = _compute_trade_delta(pair)

        assert delta.price_delta == Decimal("0")
        assert delta.qty_delta == Decimal("0")
        assert delta.fee_delta == Decimal("0")
        assert delta.pnl_delta == Decimal("0")
        assert delta.time_delta == timedelta(0)

    def test_positive_deltas(self, make_trade, ts):
        """Backtest higher than live produces positive deltas."""
        live = make_trade(price=Decimal("100"), qty=Decimal("1"), fee=Decimal("0.1"),
                          realized_pnl=Decimal("10"), source="live", timestamp=ts)
        bt = make_trade(price=Decimal("101"), qty=Decimal("1.1"), fee=Decimal("0.12"),
                        realized_pnl=Decimal("11"), source="backtest",
                        timestamp=ts + timedelta(minutes=5))
        pair = MatchedTrade(live=live, backtest=bt)

        delta = _compute_trade_delta(pair)

        assert delta.price_delta == Decimal("1")
        assert delta.qty_delta == Decimal("0.1")
        assert delta.fee_delta == Decimal("0.02")
        assert delta.pnl_delta == Decimal("1")
        assert delta.time_delta == timedelta(minutes=5)


class TestPearsonCorrelation:
    """Tests for _pearson_correlation."""

    def test_perfect_correlation(self):
        assert _pearson_correlation([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self):
        assert _pearson_correlation([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)

    def test_insufficient_data(self):
        assert _pearson_correlation([1], [2]) == 0.0
        assert _pearson_correlation([], []) == 0.0

    def test_zero_variance(self):
        assert _pearson_correlation([1, 1, 1], [2, 4, 6]) == 0.0


class TestDecimalMedian:
    """Tests for _decimal_median."""

    def test_odd_count(self):
        assert _decimal_median([Decimal("1"), Decimal("3"), Decimal("5")]) == Decimal("3")

    def test_even_count(self):
        assert _decimal_median([Decimal("1"), Decimal("3")]) == Decimal("2")

    def test_single(self):
        assert _decimal_median([Decimal("7")]) == Decimal("7")

    def test_empty(self):
        assert _decimal_median([]) == Decimal("0")


class TestCalculateMetrics:
    """Tests for calculate_metrics."""

    def test_coverage_counts(self, sample_match_result):
        """Verifies trade count and match/phantom rates."""
        m = calculate_metrics(sample_match_result)

        assert m.total_live_trades == 3  # 2 matched + 1 live-only
        assert m.total_backtest_trades == 3  # 2 matched + 1 backtest-only
        assert m.matched_count == 2
        assert m.live_only_count == 1
        assert m.backtest_only_count == 1
        assert m.match_rate == pytest.approx(2 / 3)
        assert m.phantom_rate == pytest.approx(1 / 3)

    def test_price_accuracy(self, sample_match_result):
        """Price deltas computed correctly for matched pairs."""
        m = calculate_metrics(sample_match_result)

        # order_1: same price → delta=0
        # order_2: same price → delta=0
        assert m.price_mean_abs_delta == Decimal("0")
        assert m.price_max_abs_delta == Decimal("0")

    def test_qty_accuracy(self, sample_match_result):
        """Qty deltas computed from matched pairs."""
        m = calculate_metrics(sample_match_result)

        # order_1: same qty → delta=0
        # order_2: 0.0011 - 0.001 = 0.0001
        assert m.qty_max_abs_delta == Decimal("0.0001")

    def test_fee_totals(self, sample_match_result):
        """Fee totals summed from matched pairs."""
        m = calculate_metrics(sample_match_result)

        # Live: 0.02 + 0.02 = 0.04
        assert m.total_live_fees == Decimal("0.04")
        # Backtest: 0.02 + 0.022 = 0.042
        assert m.total_backtest_fees == Decimal("0.042")
        assert m.fee_delta == Decimal("0.002")

    def test_pnl_totals(self, sample_match_result):
        """PnL totals from matched pairs."""
        m = calculate_metrics(sample_match_result)

        # Live: 0 + 0.2 = 0.2
        assert m.total_live_pnl == Decimal("0.2")
        # Backtest: 0 + 0.22 = 0.22
        assert m.total_backtest_pnl == Decimal("0.22")
        assert m.cumulative_pnl_delta == Decimal("0.02")

    def test_volume_includes_unmatched(self, sample_match_result):
        """Volume totals include matched + unmatched trades."""
        m = calculate_metrics(sample_match_result)

        # Live: 0.001 + 0.001 (matched) + 0.002 (live-only) = 0.004
        assert m.total_live_volume == Decimal("0.004")
        # Backtest: 0.001 + 0.0011 (matched) + 0.001 (bt-only) = 0.0031
        assert m.total_backtest_volume == Decimal("0.0031")

    def test_direction_breakdown(self, sample_match_result):
        """Long/short match counts from matched pairs."""
        m = calculate_metrics(sample_match_result)

        # order_1 is long (opening buy), order_2 is long (closing sell)
        assert m.long_match_count == 2
        assert m.short_match_count == 0

    def test_no_matched_pairs(self, make_trade):
        """Zero matched pairs doesn't crash."""
        result = MatchResult(
            matched=[],
            live_only=[make_trade(source="live")],
            backtest_only=[],
        )
        m = calculate_metrics(result)

        assert m.matched_count == 0
        assert m.match_rate == 0.0
        assert m.total_live_trades == 1
        assert m.price_mean_abs_delta == Decimal("0")

    def test_volume_with_no_matches(self, make_trade):
        """Volume includes unmatched trades even when no pairs match."""
        result = MatchResult(
            matched=[],
            live_only=[make_trade(source="live", qty=Decimal("0.005"))],
            backtest_only=[make_trade(source="backtest", qty=Decimal("0.003"))],
        )
        m = calculate_metrics(result)
        assert m.total_live_volume == Decimal("0.005")
        assert m.total_backtest_volume == Decimal("0.003")

    def test_all_empty(self):
        """Completely empty result doesn't crash."""
        result = MatchResult(matched=[], live_only=[], backtest_only=[])
        m = calculate_metrics(result)

        assert m.total_live_trades == 0
        assert m.match_rate == 0.0
        assert m.phantom_rate == 0.0

    def test_trade_deltas_populated(self, sample_match_result):
        """Trade deltas list is populated with one entry per matched pair."""
        m = calculate_metrics(sample_match_result)
        assert len(m.trade_deltas) == 2

    def test_pnl_correlation(self, make_trade, ts):
        """PnL correlation is computed for matched pairs."""
        # Create perfectly correlated PnL sequences
        live_trades = [
            make_trade(client_order_id="a", realized_pnl=Decimal("1"),
                       timestamp=ts, source="live"),
            make_trade(client_order_id="b", realized_pnl=Decimal("2"),
                       timestamp=ts + timedelta(hours=1), source="live"),
            make_trade(client_order_id="c", realized_pnl=Decimal("3"),
                       timestamp=ts + timedelta(hours=2), source="live"),
        ]
        bt_trades = [
            make_trade(client_order_id="a", realized_pnl=Decimal("2"),
                       timestamp=ts, source="backtest"),
            make_trade(client_order_id="b", realized_pnl=Decimal("4"),
                       timestamp=ts + timedelta(hours=1), source="backtest"),
            make_trade(client_order_id="c", realized_pnl=Decimal("6"),
                       timestamp=ts + timedelta(hours=2), source="backtest"),
        ]

        matched = [
            MatchedTrade(live=live_trades[i], backtest=bt_trades[i])
            for i in range(3)
        ]
        result = MatchResult(matched=matched, live_only=[], backtest_only=[])
        m = calculate_metrics(result)

        # Cumulative: live=[1,3,6], bt=[2,6,12] → perfectly correlated
        assert m.pnl_correlation == pytest.approx(1.0, abs=1e-6)

    def test_tolerance_breaches(self, make_trade, ts):
        """Trades exceeding tolerance are flagged."""
        live = make_trade(client_order_id="x", price=Decimal("100"), qty=Decimal("1"),
                          source="live", timestamp=ts)
        bt = make_trade(client_order_id="x", price=Decimal("100.5"), qty=Decimal("1.01"),
                        source="backtest", timestamp=ts)
        result = MatchResult(
            matched=[MatchedTrade(live=live, backtest=bt)],
            live_only=[], backtest_only=[],
        )

        # With tight tolerance, trade is flagged
        m = calculate_metrics(result, price_tolerance=Decimal("0.1"), qty_tolerance=Decimal("0.001"))
        assert m.breaches_count == 1
        assert ("x", 0) in m.breaches

        # With wide tolerance, no breaches
        m2 = calculate_metrics(result, price_tolerance=Decimal("1.0"), qty_tolerance=Decimal("0.1"))
        assert m2.breaches_count == 0

    def test_default_tolerances_exact_match_enforced(self, sample_match_result):
        """Default price_tolerance=0 enforces exact match, qty_tolerance=0.001 allows small delta."""
        m = calculate_metrics(sample_match_result)
        # price_tolerance=0 means any non-zero price delta is flagged
        # Both order_1 and order_2 have price_delta=0, so no price breaches
        # qty_tolerance=0.001 and order_2 has qty_delta=0.0001 which is < 0.001
        assert m.breaches_count == 0

    def test_direction_prefers_backtest_over_live(self, make_trade, ts):
        """Backtest direction is preferred for direction breakdown."""
        # Live trade has wrong direction (break-even close misclassified)
        live = make_trade(client_order_id="x", source="live", timestamp=ts,
                          direction="long")  # wrong: actually short
        bt = make_trade(client_order_id="x", source="backtest", timestamp=ts,
                        direction="short")  # correct
        result = MatchResult(
            matched=[MatchedTrade(live=live, backtest=bt)],
            live_only=[], backtest_only=[],
        )
        m = calculate_metrics(result)

        # Backtest direction ("short") should take priority
        assert m.short_match_count == 1
        assert m.long_match_count == 0

    def test_zero_tolerance_flags_nonzero_delta(self, make_trade, ts):
        """price_tolerance=0 flags any trade with non-zero price delta."""
        live = make_trade(client_order_id="x", price=Decimal("100"), qty=Decimal("1"),
                          source="live", timestamp=ts)
        bt = make_trade(client_order_id="x", price=Decimal("100.01"), qty=Decimal("1"),
                        source="backtest", timestamp=ts)
        result = MatchResult(
            matched=[MatchedTrade(live=live, backtest=bt)],
            live_only=[], backtest_only=[],
        )

        # price_tolerance=0 → exact match → 0.01 delta is flagged
        m = calculate_metrics(result, price_tolerance=Decimal("0"), qty_tolerance=Decimal("1"))
        assert m.breaches_count == 1
        assert ("x", 0) in m.breaches


class TestSpikeStats:
    """0070: shared robust-statistics helper (issue #156)."""

    def test_spike_stats_empty(self):
        """Empty list → all six zero (no-data / all-flat case)."""
        s = _spike_stats([])
        assert s.median == Decimal("0")
        assert s.p95 == Decimal("0")
        assert s.std == Decimal("0")
        assert s.spike_intensity == Decimal("0")
        assert s.spike_count_abs == 0
        assert s.spike_count_rel == 0

    def test_spike_stats_all_zero(self):
        """All-zero deltas → median=p95=std=spike_intensity=0, counts 0."""
        s = _spike_stats([Decimal("0")] * 4)
        assert s.median == Decimal("0")
        assert s.p95 == Decimal("0")
        assert s.std == Decimal("0")
        assert s.spike_intensity == Decimal("0")
        assert s.spike_count_abs == 0
        assert s.spike_count_rel == 0

    def test_spike_stats_median_index(self):
        """median = deltas[len//2] (upper-mid, NOT averaged); p95 = deltas[int(len*0.95)]."""
        # Odd length: 5 values → index 2.
        odd = _spike_stats(
            [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5")]
        )
        assert odd.median == Decimal("3")
        assert odd.p95 == Decimal("5")  # int(5*0.95)=4 → deltas[4]
        # Even length: [1,2,3,4] → index 2 picks 3 (upper-mid), NOT (2+3)/2.
        even = _spike_stats(
            [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        )
        assert even.median == Decimal("3")
        # Contrast with the averaging _decimal_median helper.
        assert _decimal_median(
            [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        ) == Decimal("2.5")
        assert even.p95 == Decimal("4")  # int(4*0.95)=3 → deltas[3]

    def test_spike_stats_p95_small_list_no_indexerror(self):
        """Lists of length 1, 2, 19 do not raise; p95 clamps to the last element."""
        assert _spike_stats([Decimal("0.7")]).p95 == Decimal("0.7")
        assert _spike_stats([Decimal("0.1"), Decimal("0.9")]).p95 == Decimal("0.9")
        long19 = [Decimal("0.1")] * 18 + [Decimal("0.9")]
        assert _spike_stats(long19).p95 == Decimal("0.9")

    def test_spike_stats_spike_intensity(self):
        """spike_intensity = max - median, exact."""
        s = _spike_stats([Decimal("0.1"), Decimal("0.2"), Decimal("0.9")])
        assert s.median == Decimal("0.2")
        assert s.spike_intensity == Decimal("0.7")  # 0.9 - 0.2

    def test_spike_stats_spike_count_abs_threshold(self):
        """Counts only deltas > 0.30 (boundary 0.30 excluded; 0.31 included)."""
        s = _spike_stats([Decimal("0.29"), Decimal("0.30"), Decimal("0.31")])
        assert s.spike_count_abs == 1
        assert ABS_THRESHOLD == Decimal("0.30")

    def test_spike_stats_spike_count_rel_median_zero_guard(self):
        """median == 0 with a positive tail → spike_count_rel == 0 (no false count)."""
        s = _spike_stats([Decimal("0"), Decimal("0"), Decimal("0.5")])
        assert s.median == Decimal("0")
        assert s.spike_count_rel == 0
        # spike_count_abs still fires on the > 0.30 tail.
        assert s.spike_count_abs == 1

    def test_spike_stats_spike_count_rel_positive(self):
        """median > 0 → counts deltas > 3 x median."""
        s = _spike_stats(
            [Decimal("0.1"), Decimal("0.1"), Decimal("0.1"), Decimal("0.4")]
        )
        assert s.median == Decimal("0.1")  # deltas[2]
        assert REL_K == Decimal("3")
        # 3 x 0.1 = 0.3; only 0.4 exceeds it.
        assert s.spike_count_rel == 1

    def test_spike_stats_std_single_element(self):
        """pstdev([x]) == 0, no raise; result is Decimal."""
        s = _spike_stats([Decimal("5")])
        assert s.std == Decimal("0")
        assert isinstance(s.std, Decimal)


class TestPnlRobustStats:
    """0070: trade-level pnl_* robust stats wired through calculate_metrics."""

    def test_pnl_robust_stats_populated(self, make_trade, ts):
        """Single injected PnL outlier populates pnl_spike_* symmetrically."""
        # live pnl = 0 throughout; bt pnl = baseline 0.05 x4 + one 0.60 spike.
        bt_pnls = [Decimal("0.05")] * 4 + [Decimal("0.60")]
        matched = []
        for i, p in enumerate(bt_pnls):
            live = make_trade(
                client_order_id=f"o{i}", realized_pnl=Decimal("0"),
                timestamp=ts + timedelta(hours=i), source="live",
            )
            bt = make_trade(
                client_order_id=f"o{i}", realized_pnl=p,
                timestamp=ts + timedelta(hours=i), source="backtest",
            )
            matched.append(MatchedTrade(live=live, backtest=bt))
        result = MatchResult(matched=matched, live_only=[], backtest_only=[])
        m = calculate_metrics(result)

        assert m.pnl_median_abs_delta == Decimal("0.05")
        assert m.pnl_p95_abs_delta == Decimal("0.60")  # n=5, int(4.75)=4
        assert m.pnl_spike_intensity == Decimal("0.55")  # 0.60 - 0.05
        assert m.pnl_spike_count_30c == 1
        assert m.pnl_spike_count_relative_3 == 1  # 0.60 > 3 x 0.05

    def test_pnl_robust_stats_empty_match_result(self, make_trade):
        """No matched pairs → pnl_* robust fields keep their defaults."""
        result = MatchResult(
            matched=[], live_only=[make_trade(source="live")], backtest_only=[],
        )
        m = calculate_metrics(result)
        assert m.pnl_median_abs_delta == Decimal("0")
        assert m.pnl_p95_abs_delta == Decimal("0")
        assert m.pnl_std_abs_delta == Decimal("0")
        assert m.pnl_spike_intensity == Decimal("0")
        assert m.pnl_spike_count_30c == 0
        assert m.pnl_spike_count_relative_3 == 0
