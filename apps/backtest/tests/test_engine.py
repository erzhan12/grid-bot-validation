"""Tests for backtest engine."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from gridcore import TickerEvent, EventType

from backtest.engine import BacktestEngine, FundingSimulator
from backtest.config import BacktestConfig, BacktestStrategyConfig, WindDownMode
from backtest.data_provider import InMemoryDataProvider


class TestFundingSimulator:
    """Tests for FundingSimulator."""

    def test_should_apply_at_funding_hour(self):
        """Funding applies at 0, 8, 16 hours."""
        sim = FundingSimulator()

        # 00:00 - should apply
        t1 = datetime(2025, 1, 15, 0, 0, 0)
        assert sim.should_apply_funding(t1) is True

        # 08:00 - should apply
        t2 = datetime(2025, 1, 15, 8, 0, 0)
        assert sim.should_apply_funding(t2) is True

        # 16:00 - should apply
        t3 = datetime(2025, 1, 15, 16, 0, 0)
        assert sim.should_apply_funding(t3) is True

    def test_should_not_apply_at_non_funding_hour(self):
        """Funding does not apply at non-funding hours."""
        sim = FundingSimulator()

        # 01:00 - should not apply
        t1 = datetime(2025, 1, 15, 1, 0, 0)
        assert sim.should_apply_funding(t1) is False

        # 12:00 - should not apply
        t2 = datetime(2025, 1, 15, 12, 0, 0)
        assert sim.should_apply_funding(t2) is False

    def test_should_not_apply_twice_same_period(self):
        """Funding does not apply twice in same period."""
        sim = FundingSimulator()

        t1 = datetime(2025, 1, 15, 8, 0, 0)
        assert sim.should_apply_funding(t1) is True
        sim.mark_funding_applied(t1)

        # Same hour, a bit later
        t2 = datetime(2025, 1, 15, 8, 30, 0)
        assert sim.should_apply_funding(t2) is False

    def test_should_apply_next_period(self):
        """Funding applies in next period."""
        sim = FundingSimulator()

        t1 = datetime(2025, 1, 15, 8, 0, 0)
        assert sim.should_apply_funding(t1) is True
        sim.mark_funding_applied(t1)

        # Next funding period (16:00)
        t2 = datetime(2025, 1, 15, 16, 0, 0)
        assert sim.should_apply_funding(t2) is True


class TestBacktestEngine:
    """Tests for BacktestEngine."""

    @pytest.fixture
    def config_close_all(self, sample_strategy_config):
        """Config with close_all wind-down mode."""
        return BacktestConfig(
            strategies=[sample_strategy_config],
            database_url="sqlite:///:memory:",
            initial_balance=Decimal("10000"),
            enable_funding=False,
            wind_down_mode=WindDownMode.CLOSE_ALL,
        )

    @pytest.fixture
    def config_leave_open(self, sample_strategy_config):
        """Config with leave_open wind-down mode."""
        return BacktestConfig(
            strategies=[sample_strategy_config],
            database_url="sqlite:///:memory:",
            initial_balance=Decimal("10000"),
            enable_funding=False,
            wind_down_mode=WindDownMode.LEAVE_OPEN,
        )

    @pytest.fixture
    def simple_ticks(self, sample_timestamp):
        """Simple tick sequence for testing."""
        base_time = sample_timestamp
        return [
            TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=base_time + timedelta(seconds=i),
                local_ts=base_time + timedelta(seconds=i),
                last_price=Decimal("100000") + Decimal(i * 10),
                mark_price=Decimal("100000") + Decimal(i * 10),
                bid1_price=Decimal("99999") + Decimal(i * 10),
                ask1_price=Decimal("100001") + Decimal(i * 10),
                funding_rate=Decimal("0.0001"),
            )
            for i in range(5)
        ]

    def test_run_with_in_memory_provider(self, sample_config, simple_ticks, sample_timestamp):
        """Engine runs with in-memory data provider."""
        engine = BacktestEngine(config=sample_config)
        provider = InMemoryDataProvider(simple_ticks)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )

        assert session is not None
        assert session.metrics is not None

    def test_run_creates_runners(self, sample_config, simple_ticks, sample_timestamp):
        """Engine creates runners for configured strategies."""
        engine = BacktestEngine(config=sample_config)
        provider = InMemoryDataProvider(simple_ticks)

        engine.run(
            symbol="BTCUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )

        assert len(engine.runners) == 1
        assert "test_btc" in engine.runners

    def test_run_resets_state_between_runs(self, sample_config, simple_ticks, sample_timestamp):
        """Engine resets state between runs (multi-symbol fix)."""
        engine = BacktestEngine(config=sample_config)
        provider = InMemoryDataProvider(simple_ticks)

        # First run with BTCUSDT (has configured strategy)
        engine.run(
            symbol="BTCUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )
        assert len(engine.runners) == 1

        # Second run with ETHUSDT (no configured strategy)
        # Should clear old runners and start fresh
        engine.run(
            symbol="ETHUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )

        # Old BTCUSDT runner should be cleared
        # ETHUSDT has no strategy, so runners should be empty
        assert len(engine.runners) == 0

    def test_run_creates_new_runner_instances(self, sample_config, simple_ticks, sample_timestamp):
        """Engine creates new runner instances on each run (not reusing old ones)."""
        engine = BacktestEngine(config=sample_config)
        provider = InMemoryDataProvider(simple_ticks)

        # First run
        engine.run(
            symbol="BTCUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )
        first_runner = engine.runners["test_btc"]

        # Second run with same symbol
        engine.run(
            symbol="BTCUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )
        second_runner = engine.runners["test_btc"]

        # Should be a NEW runner instance, not the same object
        assert first_runner is not second_runner

    def test_run_no_strategies_for_symbol(self, sample_config, simple_ticks, sample_timestamp):
        """Engine handles symbol with no strategies."""
        engine = BacktestEngine(config=sample_config)
        provider = InMemoryDataProvider(simple_ticks)

        session = engine.run(
            symbol="ETHUSDT",  # No strategies configured for this
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )

        assert session is not None
        assert len(engine.runners) == 0

    def test_run_without_provider_or_db_raises(self, sample_config, sample_timestamp):
        """Engine raises when no data source provided."""
        engine = BacktestEngine(config=sample_config, db=None)

        with pytest.raises(ValueError, match="Either db or data_provider"):
            engine.run(
                symbol="BTCUSDT",
                start_ts=sample_timestamp,
                end_ts=sample_timestamp + timedelta(hours=1),
            )

    def test_wind_down_close_all_closes_positions(
        self, config_close_all, sample_timestamp
    ):
        """close_all mode closes open positions."""
        # Create ticks that will trigger fills
        ticks = [
            TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=sample_timestamp,
                local_ts=sample_timestamp,
                last_price=Decimal("100000"),
                mark_price=Decimal("100000"),
                bid1_price=Decimal("99999"),
                ask1_price=Decimal("100001"),
                funding_rate=Decimal("0.0001"),
            ),
        ]

        engine = BacktestEngine(config=config_close_all)
        provider = InMemoryDataProvider(ticks)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )

        # With close_all, final unrealized should be 0 (all closed)
        assert session.metrics.total_unrealized_pnl == Decimal("0")

    def test_wind_down_leave_open_preserves_positions(
        self, config_leave_open, sample_timestamp
    ):
        """leave_open mode preserves open positions."""
        ticks = [
            TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=sample_timestamp,
                local_ts=sample_timestamp,
                last_price=Decimal("100000"),
                mark_price=Decimal("100000"),
                bid1_price=Decimal("99999"),
                ask1_price=Decimal("100001"),
                funding_rate=Decimal("0.0001"),
            ),
        ]

        engine = BacktestEngine(config=config_leave_open)
        provider = InMemoryDataProvider(ticks)

        session = engine.run(
            symbol="BTCUSDT",
            start_ts=sample_timestamp,
            end_ts=sample_timestamp + timedelta(hours=1),
            data_provider=provider,
        )

        # Session should complete (positions may or may not be open)
        assert session.metrics is not None


class TestInMemoryDataProvider:
    """Tests for InMemoryDataProvider."""

    def test_iterate_returns_events(self, sample_timestamp):
        """Provider iterates over events."""
        events = [
            TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=sample_timestamp,
                local_ts=sample_timestamp,
                last_price=Decimal("100000"),
                mark_price=Decimal("100000"),
                bid1_price=Decimal("99999"),
                ask1_price=Decimal("100001"),
                funding_rate=Decimal("0.0001"),
            ),
            TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=sample_timestamp + timedelta(seconds=1),
                local_ts=sample_timestamp + timedelta(seconds=1),
                last_price=Decimal("100010"),
                mark_price=Decimal("100010"),
                bid1_price=Decimal("100009"),
                ask1_price=Decimal("100011"),
                funding_rate=Decimal("0.0001"),
            ),
        ]

        provider = InMemoryDataProvider(events)
        result = list(provider)

        assert len(result) == 2
        assert result[0].last_price == Decimal("100000")
        assert result[1].last_price == Decimal("100010")

    def test_get_data_range_info(self, sample_timestamp):
        """Provider returns correct range info."""
        events = [
            TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=sample_timestamp,
                local_ts=sample_timestamp,
                last_price=Decimal("100000"),
                mark_price=Decimal("100000"),
                bid1_price=Decimal("99999"),
                ask1_price=Decimal("100001"),
                funding_rate=Decimal("0.0001"),
            ),
            TickerEvent(
                event_type=EventType.TICKER,
                symbol="BTCUSDT",
                exchange_ts=sample_timestamp + timedelta(hours=1),
                local_ts=sample_timestamp + timedelta(hours=1),
                last_price=Decimal("100010"),
                mark_price=Decimal("100010"),
                bid1_price=Decimal("100009"),
                ask1_price=Decimal("100011"),
                funding_rate=Decimal("0.0001"),
            ),
        ]

        provider = InMemoryDataProvider(events)
        info = provider.get_data_range_info()

        assert info.symbol == "BTCUSDT"
        assert info.start_ts == sample_timestamp
        assert info.end_ts == sample_timestamp + timedelta(hours=1)
        assert info.total_records == 2

    def test_get_data_range_info_empty(self):
        """Provider handles empty events list."""
        provider = InMemoryDataProvider([])
        info = provider.get_data_range_info()

        assert info.symbol == ""
        assert info.start_ts is None
        assert info.end_ts is None
        assert info.total_records == 0
