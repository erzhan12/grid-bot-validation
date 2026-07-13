"""Tests for ReplayEngine snapshot-emission disable under read-only (1B(b)).

FILE-BACKED temp SQLite — mode=ro is skipped for :memory:, so only a file DB
actually exercises the read-only open during a full engine run.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func

from backtest.data_provider import InMemoryDataProvider
from grid_db import DatabaseFactory, DatabaseSettings, PositionSnapshot
from grid_db.models import BybitAccount, Run, Strategy, User
from gridcore import EventType, TickerEvent

from backtest.config import BacktestStrategyConfig
from backtest.session import BacktestSession
from replay.config import ReplayConfig, ReplayStrategyConfig
from replay.engine import (
    ReplayEngine,
    _BatchPositionSnapshotWriter,
    _NoopPositionSnapshotWriter,
)

RUN_ID = "test-run-id"
TS = datetime(2026, 7, 1, 12, 0, 0)


def _tick(price, ts):
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="LTCUSDT",
        exchange_ts=ts,
        local_ts=ts,
        last_price=price,
        mark_price=price,
        bid1_price=price - Decimal("0.1"),
        ask1_price=price + Decimal("0.1"),
        funding_rate=Decimal("0.0001"),
    )


def _seed_run(db) -> str:
    with db.get_session() as session:
        user = User(username="testuser", email="t@example.com")
        session.add(user)
        session.flush()
        account = BybitAccount(
            user_id=user.user_id,
            account_name="test_account",
            environment="testnet",
        )
        session.add(account)
        session.flush()
        account_id = str(account.account_id)
        strategy = Strategy(
            account_id=account.account_id,
            strategy_type="GridStrategy",
            symbol="LTCUSDT",
            config_json={},
        )
        session.add(strategy)
        session.flush()
        session.add(Run(
            run_id=RUN_ID,
            user_id=user.user_id,
            account_id=account_id,
            strategy_id=strategy.strategy_id,
            run_type="recording",
            start_ts=TS - timedelta(hours=1),
        ))
        session.commit()
    return account_id


def _config() -> ReplayConfig:
    return ReplayConfig(
        database_url="sqlite:///:memory:",  # engine uses the db= factory
        run_id=RUN_ID,
        symbol="LTCUSDT",
        start_ts=TS,
        end_ts=TS + timedelta(minutes=10),
        strategy=ReplayStrategyConfig(
            tick_size=Decimal("0.1"),
            grid_count=10,
            grid_step=0.4,
            amount="100",
        ),
        initial_balance=Decimal("10000"),
        enable_funding=False,
        output_dir="results/test_live_check",
    )


@pytest.fixture
def mock_instrument():
    with patch("replay.engine.InstrumentInfoProvider") as mock_provider_cls:
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.01")
        mock_info.tick_size = Decimal("0.1")
        mock_info.round_qty = lambda q: q.quantize(Decimal("0.01"))
        mock_provider_cls.return_value.get.return_value = mock_info
        yield mock_provider_cls


class TestNoopWriterWiring:
    def test_disabled_emission_wires_noop_writer(self, mock_instrument):
        """emit_backtest_snapshots=False wires ONLY the no-op writer."""
        engine = ReplayEngine(
            _config(),
            db=MagicMock(),
            emit_backtest_snapshots=False,
        )
        runner = engine._init_runner(
            BacktestStrategyConfig(
                strat_id="s", symbol="LTCUSDT", tick_size=Decimal("0.1")
            ),
            BacktestSession(initial_balance=Decimal("10000")),
            run_id=RUN_ID,
            account_id="acc1",
        )
        assert isinstance(runner._position_writer, _NoopPositionSnapshotWriter)
        assert not isinstance(
            runner._position_writer, _BatchPositionSnapshotWriter
        )
        assert runner._position_writer.flush() == 0
        assert runner._position_writer.total_written == 0

    def test_default_keeps_batch_writer(self, mock_instrument):
        """Flag unset → existing callers keep the flushing batch writer."""
        engine = ReplayEngine(_config(), db=MagicMock())
        runner = engine._init_runner(
            BacktestStrategyConfig(
                strat_id="s", symbol="LTCUSDT", tick_size=Decimal("0.1")
            ),
            BacktestSession(initial_balance=Decimal("10000")),
            run_id=RUN_ID,
            account_id="acc1",
        )
        assert isinstance(runner._position_writer, _BatchPositionSnapshotWriter)


class TestReadOnlyRun:
    def test_run_completes_on_readonly_db_without_writes(
        self, tmp_path, mock_instrument
    ):
        """Engine .run() with emission disabled never writes the ro live DB.

        Completes without OperationalError and leaves the live DB's
        source='backtest' PositionSnapshot rowcount at zero.
        """
        url = f"sqlite:///{tmp_path}/recorder.db"
        writer_db = DatabaseFactory(DatabaseSettings(database_url=url))
        writer_db.create_tables()
        _seed_run(writer_db)

        ro_db = DatabaseFactory(
            DatabaseSettings(database_url=url, read_only=True)
        )
        engine = ReplayEngine(_config(), db=ro_db, emit_backtest_snapshots=False)
        ticks = [
            _tick(Decimal("80"), TS + timedelta(minutes=1)),
            _tick(Decimal("80.4"), TS + timedelta(minutes=2)),
            _tick(Decimal("79.6"), TS + timedelta(minutes=3)),
        ]
        result = engine.run(data_provider=InMemoryDataProvider(ticks))
        assert result.run_id == RUN_ID

        with writer_db.get_session() as session:
            bt_rows = (
                session.query(func.count(PositionSnapshot.id))
                .filter(PositionSnapshot.source == "backtest")
                .scalar()
            )
        assert bt_rows == 0
