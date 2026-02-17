"""Integration test: Backtest → Comparator pipeline.

Validates that backtest results can be exported, loaded by comparator,
and matched correctly. A backtest compared to itself should produce
100% match rate and zero deltas.
"""

from dataclasses import replace

import pytest
from pathlib import Path

from backtest.config import BacktestConfig, BacktestStrategyConfig, WindDownMode
from backtest.engine import BacktestEngine
from backtest.data_provider import InMemoryDataProvider
from backtest.reporter import BacktestReporter

from comparator.loader import BacktestTradeLoader
from comparator.matcher import TradeMatcher
from comparator.metrics import calculate_metrics

from helpers import generate_price_series


def _make_backtest_config(strat_id="test_strat"):
    """Create a minimal BacktestConfig for testing."""
    strategy = BacktestStrategyConfig(
        strat_id=strat_id,
        symbol="BTCUSDT",
        tick_size="0.1",
        grid_count=20,
        grid_step=0.5,
        amount="1000",
    )
    return BacktestConfig(
        strategies=[strategy],
        initial_balance=100000,
        wind_down_mode=WindDownMode.LEAVE_OPEN,
        enable_funding=False,
    )


class TestBacktestToComparator:
    """Test the full backtest → export → comparator pipeline."""

    def test_backtest_produces_trades(self):
        """Backtest with oscillating prices should produce trades."""
        config = _make_backtest_config()
        engine = BacktestEngine(config=config)

        # Large amplitude (2000) with grid_step=0.5 and grid_count=20 ensures
        # price crosses multiple grid levels to trigger fills
        events = generate_price_series(
            symbol="BTCUSDT",
            start_price=100000.0,
            amplitude=2000.0,
            num_ticks=500,
            interval_seconds=60,
        )
        provider = InMemoryDataProvider(events)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=events[0].exchange_ts,
            end_ts=events[-1].exchange_ts,
            data_provider=provider,
        )

        assert len(session.trades) > 0

    def test_export_and_load_round_trip(self, tmp_path):
        """Exported trades CSV should load correctly via BacktestTradeLoader."""
        config = _make_backtest_config()
        engine = BacktestEngine(config=config)

        events = generate_price_series(
            symbol="BTCUSDT",
            start_price=100000.0,
            amplitude=2000.0,
            num_ticks=500,
            interval_seconds=60,
        )
        provider = InMemoryDataProvider(events)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=events[0].exchange_ts,
            end_ts=events[-1].exchange_ts,
            data_provider=provider,
        )

        # Export trades
        csv_path = str(tmp_path / "trades.csv")
        reporter = BacktestReporter(session)
        reporter.export_trades(csv_path)

        # Load via comparator
        loader = BacktestTradeLoader()
        normalized = loader.load_from_csv(csv_path)

        # Count should match
        assert len(normalized) == len(session.trades)

        # All trades should have valid fields
        for trade in normalized:
            assert trade.symbol == "BTCUSDT"
            assert trade.side in ("Buy", "Sell")
            assert trade.price > 0
            assert trade.qty > 0
            assert trade.client_order_id

    def test_self_comparison_perfect_match(self, tmp_path):
        """Backtest compared to itself should produce 100% match rate."""
        config = _make_backtest_config()
        engine = BacktestEngine(config=config)

        events = generate_price_series(
            symbol="BTCUSDT",
            start_price=100000.0,
            amplitude=2000.0,
            num_ticks=500,
            interval_seconds=60,
        )
        provider = InMemoryDataProvider(events)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=events[0].exchange_ts,
            end_ts=events[-1].exchange_ts,
            data_provider=provider,
        )

        if len(session.trades) == 0:
            pytest.skip("No trades produced, cannot test matching")

        # Export trades
        csv_path = str(tmp_path / "trades.csv")
        reporter = BacktestReporter(session)
        reporter.export_trades(csv_path)

        # Load same CSV twice (simulating backtest vs "live")
        loader = BacktestTradeLoader()
        trades_a = loader.load_from_csv(csv_path)
        trades_b = loader.load_from_csv(csv_path)

        # Set different sources

        trades_b_as_live = [replace(t, source="live") for t in trades_b]

        # Match
        matcher = TradeMatcher()
        result = matcher.match(trades_b_as_live, trades_a)
        matched, live_only, backtest_only = result.matched, result.live_only, result.backtest_only

        assert len(matched) == len(trades_a)
        assert len(live_only) == 0
        assert len(backtest_only) == 0

    def test_self_comparison_zero_deltas(self, tmp_path):
        """Self-comparison should produce zero price/qty/PnL deltas."""
        config = _make_backtest_config()
        engine = BacktestEngine(config=config)

        events = generate_price_series(
            symbol="BTCUSDT",
            start_price=100000.0,
            amplitude=2000.0,
            num_ticks=500,
            interval_seconds=60,
        )
        provider = InMemoryDataProvider(events)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=events[0].exchange_ts,
            end_ts=events[-1].exchange_ts,
            data_provider=provider,
        )

        if len(session.trades) == 0:
            pytest.skip("No trades produced, cannot test metrics")

        csv_path = str(tmp_path / "trades.csv")
        reporter = BacktestReporter(session)
        reporter.export_trades(csv_path)

        loader = BacktestTradeLoader()
        trades_a = loader.load_from_csv(csv_path)
        trades_b = loader.load_from_csv(csv_path)


        trades_b_as_live = [replace(t, source="live") for t in trades_b]

        matcher = TradeMatcher()
        result = matcher.match(trades_b_as_live, trades_a)
        metrics = calculate_metrics(result)

        assert metrics.match_rate == 1.0
        assert metrics.price_mean_abs_delta == 0.0
        assert metrics.qty_mean_abs_delta == 0.0

    def test_export_metrics_csv(self, tmp_path):
        """Full pipeline: backtest → export metrics → verify file."""
        config = _make_backtest_config()
        engine = BacktestEngine(config=config)

        events = generate_price_series(
            symbol="BTCUSDT",
            start_price=100000.0,
            amplitude=2000.0,
            num_ticks=500,
            interval_seconds=60,
        )
        provider = InMemoryDataProvider(events)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=events[0].exchange_ts,
            end_ts=events[-1].exchange_ts,
            data_provider=provider,
        )

        reporter = BacktestReporter(session)
        metrics_path = str(tmp_path / "metrics.csv")
        reporter.export_metrics(metrics_path)

        assert Path(metrics_path).exists()
        content = Path(metrics_path).read_text()
        assert "initial_balance" in content
        assert "100000" in content
