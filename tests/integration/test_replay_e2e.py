"""Integration test: Replay engine end-to-end pipeline.

Validates the full replay pipeline: seed DB with price data + reference
trades → ReplayEngine reads from DB → comparison → 100% match.

Uses a "replay-in-a-bottle" pattern: run a reference backtest to generate
deterministic trades, seed those as PrivateExecutions, then replay the same
price data through ReplayEngine and verify perfect match via TradeMatcher.
"""

from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from backtest.config import BacktestConfig, BacktestStrategyConfig, WindDownMode
from backtest.data_provider import InMemoryDataProvider
from backtest.engine import BacktestEngine
from backtest.instrument_info import InstrumentInfo

from grid_db import (
    DatabaseFactory,
    DatabaseSettings,
    User,
    BybitAccount,
    Strategy,
    Run,
    TickerSnapshot,
    PrivateExecution,
)

from replay.config import ReplayConfig, ReplayStrategyConfig
from replay.engine import ReplayEngine

from datetime import datetime, timezone, timedelta

from integration_helpers import generate_price_series, make_ticker_event


# Grid configuration (must match between backtest and replay configs)
SYMBOL = "BTCUSDT"
STRAT_ID = "replay_e2e"
TICK_SIZE = "0.1"
GRID_COUNT = 20
GRID_STEP = 0.5
AMOUNT = "1000"
COMMISSION_RATE = Decimal("0.0002")

# Test execution constants
INITIAL_BALANCE = Decimal("100000")
RUN_ID = "e2e-recording-run"
QTY_STEP = Decimal("0.001")  # Instrument default

# Price series parameters (tuned for reliable trade generation).
# Amplitude must exceed grid_step * grid_count / 2 (= 0.5 * 20 / 2 = 5%)
# of start_price to guarantee grid crossings. 2000 / 100000 = 2% > 5% of span.
START_PRICE = 100000.0
AMPLITUDE = 2000.0
NUM_TICKS = 500
INTERVAL_SECONDS = 60


def _mock_instrument_info() -> MagicMock:
    """Create mock InstrumentInfoProvider using real InstrumentInfo."""
    mock_cls = MagicMock()
    mock_cls.return_value.get.return_value = InstrumentInfo(
        symbol=SYMBOL,
        qty_step=QTY_STEP,
        tick_size=Decimal(TICK_SIZE),
        min_qty=QTY_STEP,
        max_qty=Decimal("100"),
    )
    return mock_cls


@pytest.fixture
def seed_replay_db():
    """Seed in-memory DB with price data + reference backtest trades.

    Steps:
    1. Generate deterministic price series
    2. Run BacktestEngine to produce reference trades
    3. Seed DB with entities, TickerSnapshots, and PrivateExecutions
    4. Yield (db, reference_session) for test assertions
    """
    # 1. Generate price series
    events = generate_price_series(
        symbol=SYMBOL,
        start_price=START_PRICE,
        amplitude=AMPLITUDE,
        num_ticks=NUM_TICKS,
        interval_seconds=INTERVAL_SECONDS,
    )

    # 2. Run reference backtest
    bt_config = BacktestConfig(
        strategies=[
            BacktestStrategyConfig(
                strat_id=STRAT_ID,
                symbol=SYMBOL,
                tick_size=TICK_SIZE,
                grid_count=GRID_COUNT,
                grid_step=GRID_STEP,
                amount=AMOUNT,
                commission_rate=COMMISSION_RATE,
            )
        ],
        initial_balance=INITIAL_BALANCE,
        wind_down_mode=WindDownMode.LEAVE_OPEN,
        enable_funding=False,
    )
    bt_engine = BacktestEngine(config=bt_config)
    bt_provider = InMemoryDataProvider(events)
    bt_session = bt_engine.run(
        symbol=SYMBOL,
        start_ts=events[0].exchange_ts,
        end_ts=events[-1].exchange_ts,
        data_provider=bt_provider,
    )

    # With amplitude=2000 (2% of start_price) crossing a grid_step=0.5% grid
    # over 500 ticks, expect ~110 trades. Use 10 as threshold to catch major
    # regressions without being overly strict about exact count.
    min_expected = 10
    assert len(bt_session.trades) >= min_expected, (
        f"Expected at least {min_expected} trades from oscillating prices, "
        f"got {len(bt_session.trades)}"
    )

    # 3. Create DB and seed
    db_settings = DatabaseSettings(db_type="sqlite", db_name=":memory:")
    db = DatabaseFactory(db_settings)
    db.create_tables()

    with db.get_session() as s:
        # Core entities (FK chain: User → Account → Strategy → Run)
        user = User(user_id="u1", username="e2e_test")
        account = BybitAccount(
            account_id="a1",
            user_id="u1",
            account_name="e2e_test",
            environment="testnet",
        )
        strategy = Strategy(
            strategy_id="s1",
            account_id="a1",
            strategy_type="recorder",
            symbol=SYMBOL,
            config_json={},
        )
        run = Run(
            run_id=RUN_ID,
            user_id="u1",
            account_id="a1",
            strategy_id="s1",
            run_type="recording",
            status="completed",
            start_ts=events[0].exchange_ts,
            end_ts=events[-1].exchange_ts,
        )
        s.add_all([user, account, strategy, run])

        # Seed TickerSnapshots from price events
        for ev in events:
            s.add(
                TickerSnapshot(
                    symbol=ev.symbol,
                    exchange_ts=ev.exchange_ts,
                    local_ts=ev.local_ts,
                    last_price=ev.last_price,
                    mark_price=ev.mark_price,
                    bid1_price=ev.bid1_price,
                    ask1_price=ev.ask1_price,
                    funding_rate=ev.funding_rate,
                )
            )

        # Seed PrivateExecutions from reference backtest trades
        for i, trade in enumerate(bt_session.trades):
            s.add(
                PrivateExecution(
                    run_id=RUN_ID,
                    account_id="a1",
                    symbol=trade.symbol,
                    exec_id=f"exec_{i}",
                    order_id=trade.order_id,
                    order_link_id=trade.client_order_id,
                    exchange_ts=trade.timestamp,
                    side=trade.side,
                    exec_price=trade.price,
                    exec_qty=trade.qty,
                    exec_fee=trade.commission,
                    closed_pnl=trade.realized_pnl,
                )
            )

        s.commit()

    yield db, bt_session
    db.drop_tables()


def _make_replay_config() -> ReplayConfig:
    """Create ReplayConfig matching the backtest config."""
    return ReplayConfig(
        database_url="sqlite:///:memory:",
        run_id=RUN_ID,
        symbol=SYMBOL,
        strategy=ReplayStrategyConfig(
            tick_size=Decimal(TICK_SIZE),
            grid_count=GRID_COUNT,
            grid_step=GRID_STEP,
            amount=AMOUNT,
            commission_rate=COMMISSION_RATE,
        ),
        initial_balance=INITIAL_BALANCE,
        enable_funding=False,
        wind_down_mode=WindDownMode.LEAVE_OPEN,
    )


class TestReplayE2E:
    """End-to-end replay pipeline: seed DB → replay → compare → assert match."""

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_produces_trades(self, _mock_iip, seed_replay_db):
        """ReplayEngine produces trades when fed real price data from DB."""
        db, bt_session = seed_replay_db
        config = _make_replay_config()

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert len(result.session.trades) > 0, (
            f"Replay must produce trades (got {len(result.session.trades)})"
        )

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_match_rate_100_percent(self, _mock_iip, seed_replay_db):
        """Replay trades match 100% of recorded ground truth."""
        db, bt_session = seed_replay_db
        config = _make_replay_config()

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert result.metrics.match_rate == 1.0, (
            f"Expected 100% match rate, got {result.metrics.match_rate:.1%}"
        )

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_zero_deltas(self, _mock_iip, seed_replay_db):
        """Matched trades have zero price and quantity deltas."""
        db, bt_session = seed_replay_db
        config = _make_replay_config()

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert result.metrics.price_mean_abs_delta == 0.0, (
            f"Expected 0 price delta, got {result.metrics.price_mean_abs_delta}"
        )
        assert result.metrics.qty_mean_abs_delta == 0.0, (
            f"Expected 0 qty delta, got {result.metrics.qty_mean_abs_delta}"
        )

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_no_unmatched_trades(self, _mock_iip, seed_replay_db):
        """No live-only or backtest-only unmatched trades."""
        db, bt_session = seed_replay_db
        config = _make_replay_config()

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert len(result.match_result.live_only) == 0, (
            f"Expected 0 live-only trades, got {len(result.match_result.live_only)}"
        )
        assert len(result.match_result.backtest_only) == 0, (
            f"Expected 0 backtest-only trades, got {len(result.match_result.backtest_only)}"
        )

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_pnl_totals_match(self, _mock_iip, seed_replay_db):
        """Cumulative PnL delta between replay and recorded is negligible.

        Note: Small delta expected due to DB precision limits. The database stores
        closed_pnl as Numeric(20,8) (8 decimal places), while replay computes at
        full Decimal precision. For 100+ trades, cumulative rounding error should
        still be < 0.001 USDT.
        """
        db, bt_session = seed_replay_db
        config = _make_replay_config()

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert abs(result.metrics.cumulative_pnl_delta) < Decimal("0.001"), (
            f"PnL delta too large: {result.metrics.cumulative_pnl_delta}"
        )

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_session_equity_populated(self, _mock_iip, seed_replay_db):
        """Session equity curve is populated with entries for each tick."""
        db, bt_session = seed_replay_db
        config = _make_replay_config()

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert len(result.session.equity_curve) == NUM_TICKS, (
            f"Expected {NUM_TICKS} equity entries, got {len(result.session.equity_curve)}"
        )


def _seed_db_with_events(events, run_id, symbol=SYMBOL):
    """Create a seeded DB with core entities and TickerSnapshots (no executions).

    Returns db for use in edge case tests.
    """
    db_settings = DatabaseSettings(db_type="sqlite", db_name=":memory:")
    db = DatabaseFactory(db_settings)
    db.create_tables()

    # Ensure end_ts > start_ts even for single-tick cases
    end_ts = events[-1].exchange_ts
    if end_ts <= events[0].exchange_ts:
        end_ts = events[0].exchange_ts + timedelta(seconds=1)

    with db.get_session() as s:
        user = User(user_id="u1", username="e2e_test")
        account = BybitAccount(
            account_id="a1", user_id="u1",
            account_name="e2e_test", environment="testnet",
        )
        strategy = Strategy(
            strategy_id="s1", account_id="a1",
            strategy_type="recorder", symbol=symbol, config_json={},
        )
        run = Run(
            run_id=run_id, user_id="u1", account_id="a1", strategy_id="s1",
            run_type="recording", status="completed",
            start_ts=events[0].exchange_ts, end_ts=end_ts,
        )
        s.add_all([user, account, strategy, run])

        for ev in events:
            s.add(TickerSnapshot(
                symbol=ev.symbol, exchange_ts=ev.exchange_ts, local_ts=ev.local_ts,
                last_price=ev.last_price, mark_price=ev.mark_price,
                bid1_price=ev.bid1_price, ask1_price=ev.ask1_price,
                funding_rate=ev.funding_rate,
            ))
        s.commit()

    return db


class TestReplayEdgeCases:
    """Edge case tests for replay pipeline robustness."""

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_no_trades_flat_price(self, _mock_iip):
        """Flat price series produces zero trades and handles gracefully."""
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        events = [
            make_ticker_event(SYMBOL, START_PRICE, ts + timedelta(minutes=i))
            for i in range(50)
        ]

        db = _seed_db_with_events(events, run_id="flat-run")
        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="flat-run",
            symbol=SYMBOL,
            strategy=ReplayStrategyConfig(
                tick_size=Decimal(TICK_SIZE), grid_count=GRID_COUNT,
                grid_step=GRID_STEP, amount=AMOUNT,
                commission_rate=COMMISSION_RATE,
            ),
            initial_balance=INITIAL_BALANCE,
            enable_funding=False,
            wind_down_mode=WindDownMode.LEAVE_OPEN,
        )

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert len(result.session.trades) == 0
        assert result.metrics.match_rate == 0.0
        assert len(result.match_result.live_only) == 0
        assert len(result.match_result.backtest_only) == 0
        db.drop_tables()

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_single_tick(self, _mock_iip):
        """Single tick does not crash and produces empty trade set."""
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        events = [make_ticker_event(SYMBOL, START_PRICE, ts)]

        db = _seed_db_with_events(events, run_id="single-tick-run")
        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="single-tick-run",
            symbol=SYMBOL,
            strategy=ReplayStrategyConfig(
                tick_size=Decimal(TICK_SIZE), grid_count=GRID_COUNT,
                grid_step=GRID_STEP, amount=AMOUNT,
                commission_rate=COMMISSION_RATE,
            ),
            initial_balance=INITIAL_BALANCE,
            enable_funding=False,
            wind_down_mode=WindDownMode.LEAVE_OPEN,
        )

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        assert result.session is not None
        assert len(result.session.trades) == 0
        assert len(result.session.equity_curve) == 1
        db.drop_tables()

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_symbol_isolation(self, _mock_iip):
        """Other symbols in DB don't contaminate replay results."""
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)

        db_settings = DatabaseSettings(db_type="sqlite", db_name=":memory:")
        db = DatabaseFactory(db_settings)
        db.create_tables()

        with db.get_session() as s:
            user = User(user_id="u1", username="e2e_test")
            account = BybitAccount(
                account_id="a1", user_id="u1",
                account_name="e2e_test", environment="testnet",
            )
            strategy = Strategy(
                strategy_id="s1", account_id="a1",
                strategy_type="recorder", symbol=SYMBOL, config_json={},
            )
            run = Run(
                run_id="iso-run", user_id="u1", account_id="a1",
                strategy_id="s1", run_type="recording", status="completed",
                start_ts=ts, end_ts=ts + timedelta(hours=1),
            )
            s.add_all([user, account, strategy, run])

            # Seed BTCUSDT ticks (flat — no trades expected)
            for i in range(10):
                tick_ts = ts + timedelta(minutes=i)
                s.add(TickerSnapshot(
                    symbol=SYMBOL, exchange_ts=tick_ts, local_ts=tick_ts,
                    last_price=Decimal("100000"), mark_price=Decimal("100000"),
                    bid1_price=Decimal("99999.5"), ask1_price=Decimal("100000.5"),
                    funding_rate=Decimal("0.0001"),
                ))

            # Seed ETHUSDT ticks (should be ignored)
            for i in range(10):
                tick_ts = ts + timedelta(minutes=i)
                s.add(TickerSnapshot(
                    symbol="ETHUSDT", exchange_ts=tick_ts, local_ts=tick_ts,
                    last_price=Decimal("3000"), mark_price=Decimal("3000"),
                    bid1_price=Decimal("2999.5"), ask1_price=Decimal("3000.5"),
                    funding_rate=Decimal("0.0001"),
                ))

            # Seed a fake ETHUSDT execution (should not appear in BTCUSDT replay)
            s.add(PrivateExecution(
                run_id="iso-run", account_id="a1", symbol="ETHUSDT",
                exec_id="eth-exec-1", order_id="eth-order-1",
                order_link_id="eth-client-1", exchange_ts=ts + timedelta(minutes=5),
                side="Buy", exec_price=Decimal("3000"), exec_qty=Decimal("1"),
                exec_fee=Decimal("0.6"), closed_pnl=Decimal("0"),
            ))
            s.commit()

        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="iso-run",
            symbol=SYMBOL,
            start_ts=ts,
            end_ts=ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal(TICK_SIZE), grid_count=GRID_COUNT,
                grid_step=GRID_STEP, amount=AMOUNT,
                commission_rate=COMMISSION_RATE,
            ),
            initial_balance=INITIAL_BALANCE,
            enable_funding=False,
            wind_down_mode=WindDownMode.LEAVE_OPEN,
        )

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        # ETHUSDT execution must not appear as live trade
        assert result.metrics.total_live_trades == 0, (
            f"ETHUSDT execution leaked into BTCUSDT replay: "
            f"{result.metrics.total_live_trades} live trades"
        )
        db.drop_tables()

    @patch("replay.engine.InstrumentInfoProvider", new_callable=_mock_instrument_info)
    def test_replay_run_isolation(self, _mock_iip):
        """Executions from other run_ids don't contaminate replay."""
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)

        db_settings = DatabaseSettings(db_type="sqlite", db_name=":memory:")
        db = DatabaseFactory(db_settings)
        db.create_tables()

        with db.get_session() as s:
            user = User(user_id="u1", username="e2e_test")
            account = BybitAccount(
                account_id="a1", user_id="u1",
                account_name="e2e_test", environment="testnet",
            )
            strategy = Strategy(
                strategy_id="s1", account_id="a1",
                strategy_type="recorder", symbol=SYMBOL, config_json={},
            )
            # Two runs
            run1 = Run(
                run_id="run-1", user_id="u1", account_id="a1",
                strategy_id="s1", run_type="recording", status="completed",
                start_ts=ts, end_ts=ts + timedelta(hours=1),
            )
            run2 = Run(
                run_id="run-2", user_id="u1", account_id="a1",
                strategy_id="s1", run_type="recording", status="completed",
                start_ts=ts, end_ts=ts + timedelta(hours=1),
            )
            s.add_all([user, account, strategy, run1, run2])

            # Seed ticks for both runs to use
            for i in range(10):
                tick_ts = ts + timedelta(minutes=i)
                s.add(TickerSnapshot(
                    symbol=SYMBOL, exchange_ts=tick_ts, local_ts=tick_ts,
                    last_price=Decimal("100000"), mark_price=Decimal("100000"),
                    bid1_price=Decimal("99999.5"), ask1_price=Decimal("100000.5"),
                    funding_rate=Decimal("0.0001"),
                ))

            # Seed execution only on run-2 (should not appear in run-1 replay)
            s.add(PrivateExecution(
                run_id="run-2", account_id="a1", symbol=SYMBOL,
                exec_id="other-exec-1", order_id="other-order-1",
                order_link_id="other-client-1",
                exchange_ts=ts + timedelta(minutes=5),
                side="Buy", exec_price=Decimal("100000"),
                exec_qty=Decimal("0.01"), exec_fee=Decimal("0.002"),
                closed_pnl=Decimal("0"),
            ))
            s.commit()

        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="run-1",
            symbol=SYMBOL,
            start_ts=ts,
            end_ts=ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal(TICK_SIZE), grid_count=GRID_COUNT,
                grid_step=GRID_STEP, amount=AMOUNT,
                commission_rate=COMMISSION_RATE,
            ),
            initial_balance=INITIAL_BALANCE,
            enable_funding=False,
            wind_down_mode=WindDownMode.LEAVE_OPEN,
        )

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        # run-2 execution must not appear in run-1 replay
        assert result.metrics.total_live_trades == 0, (
            f"run-2 execution leaked into run-1 replay: "
            f"{result.metrics.total_live_trades} live trades"
        )
        db.drop_tables()
