"""Tests for comparator.matcher module."""

from decimal import Decimal

from comparator.matcher import TradeMatcher


class TestTradeMatcher:
    """Tests for TradeMatcher."""

    def test_all_matched(self, make_trade):
        """All trades match when client_order_ids are identical."""
        live = [
            make_trade(client_order_id="a", source="live"),
            make_trade(client_order_id="b", source="live"),
        ]
        backtest = [
            make_trade(client_order_id="a", source="backtest"),
            make_trade(client_order_id="b", source="backtest"),
        ]

        matcher = TradeMatcher()
        result = matcher.match(live, backtest)

        assert len(result.matched) == 2
        assert len(result.live_only) == 0
        assert len(result.backtest_only) == 0

    def test_live_only(self, make_trade):
        """Trades in live but not backtest appear in live_only."""
        live = [
            make_trade(client_order_id="a", source="live"),
            make_trade(client_order_id="b", source="live"),
        ]
        backtest = [
            make_trade(client_order_id="a", source="backtest"),
        ]

        matcher = TradeMatcher()
        result = matcher.match(live, backtest)

        assert len(result.matched) == 1
        assert len(result.live_only) == 1
        assert result.live_only[0].client_order_id == "b"

    def test_backtest_only(self, make_trade):
        """Trades in backtest but not live appear in backtest_only."""
        live = [
            make_trade(client_order_id="a", source="live"),
        ]
        backtest = [
            make_trade(client_order_id="a", source="backtest"),
            make_trade(client_order_id="c", source="backtest"),
        ]

        matcher = TradeMatcher()
        result = matcher.match(live, backtest)

        assert len(result.matched) == 1
        assert len(result.backtest_only) == 1
        assert result.backtest_only[0].client_order_id == "c"

    def test_no_overlap(self, make_trade):
        """No matching client_order_ids gives zero matched."""
        live = [make_trade(client_order_id="a", source="live")]
        backtest = [make_trade(client_order_id="b", source="backtest")]

        matcher = TradeMatcher()
        result = matcher.match(live, backtest)

        assert len(result.matched) == 0
        assert len(result.live_only) == 1
        assert len(result.backtest_only) == 1

    def test_empty_inputs(self, make_trade):
        """Empty inputs produce empty result."""
        matcher = TradeMatcher()
        result = matcher.match([], [])

        assert len(result.matched) == 0
        assert len(result.live_only) == 0
        assert len(result.backtest_only) == 0

    def test_matched_pair_references_correct_sources(self, make_trade):
        """MatchedTrade.live comes from live source, .backtest from backtest."""
        live = [make_trade(client_order_id="x", price=Decimal("100"), source="live")]
        backtest = [make_trade(client_order_id="x", price=Decimal("200"), source="backtest")]

        matcher = TradeMatcher()
        result = matcher.match(live, backtest)

        pair = result.matched[0]
        assert pair.live.source == "live"
        assert pair.live.price == Decimal("100")
        assert pair.backtest.source == "backtest"
        assert pair.backtest.price == Decimal("200")

    def test_mixed_scenario(self, sample_live_trades, sample_backtest_trades):
        """Mixed scenario: 2 matched, 1 live-only, 1 backtest-only."""
        matcher = TradeMatcher()
        result = matcher.match(sample_live_trades, sample_backtest_trades)

        assert len(result.matched) == 2
        assert len(result.live_only) == 1
        assert len(result.backtest_only) == 1
        assert result.live_only[0].client_order_id == "order_3"
        assert result.backtest_only[0].client_order_id == "order_4"

    def test_reused_client_order_id_matched_by_occurrence(self, make_trade, ts):
        """Reused client_order_id is matched by (id, occurrence) composite key."""
        from datetime import timedelta

        live = [
            make_trade(client_order_id="reused", price=Decimal("100"), source="live",
                       timestamp=ts),
            make_trade(client_order_id="reused", price=Decimal("100"), source="live",
                       timestamp=ts + timedelta(hours=2)),
        ]
        live[0].occurrence = 0
        live[1].occurrence = 1

        backtest = [
            make_trade(client_order_id="reused", price=Decimal("100"), source="backtest",
                       timestamp=ts),
            make_trade(client_order_id="reused", price=Decimal("100"), source="backtest",
                       timestamp=ts + timedelta(hours=2)),
        ]
        backtest[0].occurrence = 0
        backtest[1].occurrence = 1

        matcher = TradeMatcher()
        result = matcher.match(live, backtest)

        assert len(result.matched) == 2
        assert len(result.live_only) == 0
        assert len(result.backtest_only) == 0
        # Verify each occurrence is matched to its counterpart
        assert result.matched[0].live.occurrence == 0
        assert result.matched[0].backtest.occurrence == 0
        assert result.matched[1].live.occurrence == 1
        assert result.matched[1].backtest.occurrence == 1

    def test_reused_id_mismatched_occurrences(self, make_trade, ts):
        """Different occurrence counts produce live-only and backtest-only."""
        from datetime import timedelta

        live = [
            make_trade(client_order_id="reused", source="live", timestamp=ts),
            make_trade(client_order_id="reused", source="live",
                       timestamp=ts + timedelta(hours=1)),
        ]
        live[0].occurrence = 0
        live[1].occurrence = 1

        backtest = [
            make_trade(client_order_id="reused", source="backtest", timestamp=ts),
        ]
        backtest[0].occurrence = 0

        matcher = TradeMatcher()
        result = matcher.match(live, backtest)

        # occurrence=0 matches, occurrence=1 is live-only
        assert len(result.matched) == 1
        assert len(result.live_only) == 1
        assert result.live_only[0].occurrence == 1
