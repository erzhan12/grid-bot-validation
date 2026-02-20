"""Tests for replay engine."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from gridcore import TickerEvent, EventType
from grid_db import Run, User, BybitAccount, Strategy

from backtest.data_provider import InMemoryDataProvider

from replay.config import ReplayConfig, ReplayStrategyConfig
from replay.engine import ReplayEngine, ReplayResult


def _make_tick(price: Decimal, ts: datetime) -> TickerEvent:
    """Helper to create TickerEvent for tests."""
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        exchange_ts=ts,
        local_ts=ts,
        last_price=price,
        mark_price=price,
        bid1_price=price - Decimal("1"),
        ask1_price=price + Decimal("1"),
        funding_rate=Decimal("0.0001"),
    )


@pytest.fixture
def ts():
    return datetime(2025, 2, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ticker_events(ts):
    """Create a sequence of ticker events with price movement."""
    prices = [
        Decimal("100000"),
        Decimal("99800"),   # Drop → BUY fills possible
        Decimal("99600"),
        Decimal("99800"),   # Recover
        Decimal("100000"),
        Decimal("100200"),  # Rise → SELL fills possible
        Decimal("100400"),
        Decimal("100200"),  # Drop back
        Decimal("100000"),
    ]
    return [_make_tick(price, ts + timedelta(minutes=i)) for i, price in enumerate(prices)]


@pytest.fixture
def replay_config(ts):
    return ReplayConfig(
        database_url="sqlite:///:memory:",
        run_id="test-run-id",
        symbol="BTCUSDT",
        start_ts=ts,
        end_ts=ts + timedelta(hours=1),
        strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
        initial_balance=Decimal("10000"),
        enable_funding=False,
        output_dir="results/test",
    )


class TestReplayEngine:
    """Tests for ReplayEngine."""

    @patch("replay.engine.InstrumentInfoProvider")
    def test_replay_produces_result(self, mock_provider_cls, db, replay_config, ticker_events):
        """Replay with price movement produces a ReplayResult."""
        # Mock instrument info
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_provider_cls.return_value.get.return_value = mock_info

        engine = ReplayEngine(config=replay_config, db=db)
        provider = InMemoryDataProvider(ticker_events)

        result = engine.run(data_provider=provider)

        assert isinstance(result, ReplayResult)
        assert result.session is not None
        assert result.metrics is not None
        assert result.match_result is not None

    @patch("replay.engine.InstrumentInfoProvider")
    def test_replay_empty_data(self, mock_provider_cls, db, replay_config):
        """Replay with no data produces empty result."""
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_provider_cls.return_value.get.return_value = mock_info

        engine = ReplayEngine(config=replay_config, db=db)
        provider = InMemoryDataProvider([])

        result = engine.run(data_provider=provider)

        assert result.session is not None
        assert len(result.session.trades) == 0
        assert result.metrics.total_live_trades == 0
        assert result.metrics.total_backtest_trades == 0

    @patch("replay.engine.InstrumentInfoProvider")
    def test_replay_session_has_equity_curve(self, mock_provider_cls, db, replay_config, ticker_events):
        """Session equity curve is populated during replay."""
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_provider_cls.return_value.get.return_value = mock_info

        engine = ReplayEngine(config=replay_config, db=db)
        provider = InMemoryDataProvider(ticker_events)

        result = engine.run(data_provider=provider)

        # Equity curve should have entries for each tick
        assert len(result.session.equity_curve) == len(ticker_events)


class TestResolveRun:
    """Tests for run auto-discovery."""

    @patch("replay.engine.InstrumentInfoProvider")
    def test_auto_discover_latest_run(self, mock_provider_cls, db, ts):
        """Auto-discovers latest recording run when run_id is None."""
        # Seed a recording run
        with db.get_session() as session:
            user = User(user_id="user-1", username="test")
            account = BybitAccount(
                account_id="acc-1", user_id="user-1",
                account_name="test", environment="testnet",
            )
            strategy = Strategy(
                strategy_id="strat-1", account_id="acc-1",
                strategy_type="recorder", symbol="BTCUSDT",
                config_json={},
            )
            run = Run(
                run_id="discovered-run",
                user_id="user-1",
                account_id="acc-1",
                strategy_id="strat-1",
                run_type="recording",
                status="completed",
                start_ts=ts,
                end_ts=ts + timedelta(hours=2),
            )
            session.add_all([user, account, strategy, run])
            session.commit()

        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id=None,  # auto-discover
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
            enable_funding=False,
        )

        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_provider_cls.return_value.get.return_value = mock_info

        engine = ReplayEngine(config=config, db=db)
        provider = InMemoryDataProvider([])

        result = engine.run(data_provider=provider)

        # Should have resolved without error
        assert result is not None

    @patch("replay.engine.InstrumentInfoProvider")
    def test_no_recording_run_raises(self, mock_provider_cls, db):
        """Raises ValueError when no recording runs exist."""
        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id=None,
            symbol="BTCUSDT",
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
        )

        engine = ReplayEngine(config=config, db=db)

        with pytest.raises(ValueError, match="No recording runs found"):
            engine.run()

    @patch("replay.engine.InstrumentInfoProvider")
    def test_explicit_run_id_requires_timestamps(self, mock_provider_cls, db):
        """Raises ValueError when run_id is explicit but no timestamps given."""
        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="some-run-id",
            symbol="BTCUSDT",
            start_ts=None,
            end_ts=None,
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
        )

        engine = ReplayEngine(config=config, db=db)

        with pytest.raises(ValueError, match="start_ts and end_ts must be specified"):
            engine.run()


class TestWindDown:
    """Tests for wind-down behavior."""

    @patch("replay.engine.InstrumentInfoProvider")
    def test_leave_open_preserves_positions(self, mock_provider_cls, db, replay_config, ticker_events):
        """leave_open mode does not create wind-down trades."""
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_provider_cls.return_value.get.return_value = mock_info

        replay_config.wind_down_mode = "leave_open"
        engine = ReplayEngine(config=replay_config, db=db)
        provider = InMemoryDataProvider(ticker_events)

        result = engine.run(data_provider=provider)

        # No wind_down trades
        wind_down_trades = [t for t in result.session.trades if "wind_down" in t.client_order_id]
        assert len(wind_down_trades) == 0

    @patch("replay.engine.InstrumentInfoProvider")
    def test_close_all_creates_wind_down_trades(self, mock_provider_cls, db, replay_config, ticker_events):
        """close_all mode creates wind-down trades for open positions."""
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_provider_cls.return_value.get.return_value = mock_info

        replay_config.wind_down_mode = "close_all"
        engine = ReplayEngine(config=replay_config, db=db)
        provider = InMemoryDataProvider(ticker_events)

        result = engine.run(data_provider=provider)

        # If there were any fills, wind_down trades should exist
        if any(t for t in result.session.trades if "wind_down" not in t.client_order_id):
            wind_down_trades = [t for t in result.session.trades if "wind_down" in t.client_order_id]
            assert len(wind_down_trades) > 0
