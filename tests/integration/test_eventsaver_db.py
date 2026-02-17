"""Integration test: EventSaver writers → Database.

Validates that normalized events flow through writers into the database
and are queryable via repositories.
"""

from dataclasses import dataclass

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4, UUID

from grid_db import DatabaseFactory, DatabaseSettings
from grid_db import User, BybitAccount, Strategy, Run
from grid_db import (
    PublicTradeRepository,
    PrivateExecutionRepository,
    OrderRepository,
)
from grid_db.models import PublicTrade, PrivateExecution


@dataclass
class SeededDb:
    """Result of the seeded_db fixture."""
    db: DatabaseFactory
    user_id: UUID
    account_id: UUID
    strategy_id: UUID
    run_id: UUID
from gridcore.events import PublicTradeEvent, ExecutionEvent, OrderUpdateEvent, EventType
from event_saver.writers import TradeWriter, ExecutionWriter, OrderWriter


@pytest.fixture
def db():
    """In-memory SQLite database."""
    settings = DatabaseSettings()
    settings.db_type = "sqlite"
    settings.db_name = ":memory:"
    factory = DatabaseFactory(settings)
    factory.create_tables()
    return factory


@pytest.fixture
def seeded_db(db):
    """Database seeded with user, account, strategy, and run."""
    with db.get_session() as session:
        user = User(username="test_user", email="test@example.com")
        session.add(user)
        session.flush()

        account = BybitAccount(
            user_id=user.user_id,
            account_name="main",
            environment="testnet",
        )
        session.add(account)
        session.flush()

        strategy = Strategy(
            account_id=account.account_id,
            strategy_type="GridStrategy",
            symbol="BTCUSDT",
            config_json={"grid_step": 0.2, "grid_count": 50},
        )
        session.add(strategy)
        session.flush()

        run = Run(
            user_id=user.user_id,
            account_id=account.account_id,
            strategy_id=strategy.strategy_id,
            run_type="live",
        )
        session.add(run)
        session.flush()

        return SeededDb(
            db=db,
            user_id=user.user_id,
            account_id=account.account_id,
            strategy_id=strategy.strategy_id,
            run_id=run.run_id,
        )


class TestPublicTradesPipeline:
    """Test public trades flowing into database."""

    def test_bulk_insert_trades(self, db):
        """Bulk insert trades and query them back."""
        trades = []
        for i in range(10):
            trade = PublicTrade(
                symbol="BTCUSDT",
                trade_id=f"trade_{i}",
                exchange_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                side="Buy" if i % 2 == 0 else "Sell",
                price=Decimal("100000.0"),
                size=Decimal("0.001"),
            )
            trades.append(trade)

        with db.get_session() as session:
            repo = PublicTradeRepository(session)
            inserted = repo.bulk_insert(trades)
            assert inserted == 10

    def test_duplicate_trades_skipped(self, db):
        """Duplicate trade_id should be skipped via ON CONFLICT DO NOTHING."""
        trade = PublicTrade(
            symbol="BTCUSDT",
            trade_id="unique_trade_1",
            exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            local_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            side="Buy",
            price=Decimal("100000.0"),
            size=Decimal("0.001"),
        )

        with db.get_session() as session:
            repo = PublicTradeRepository(session)
            first = repo.bulk_insert([trade])
            assert first == 1

        # Insert same trade_id again
        trade2 = PublicTrade(
            symbol="BTCUSDT",
            trade_id="unique_trade_1",
            exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            local_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            side="Buy",
            price=Decimal("100000.0"),
            size=Decimal("0.001"),
        )

        with db.get_session() as session:
            repo = PublicTradeRepository(session)
            second = repo.bulk_insert([trade2])
            assert second == 0  # Duplicate skipped

    def test_large_batch_insert(self, db):
        """Insert 500+ trades in a single batch."""
        trades = []
        for i in range(500):
            trade = PublicTrade(
                symbol="BTCUSDT",
                trade_id=f"batch_trade_{i}",
                exchange_ts=datetime(2025, 1, 1, 0, 0, 0, i * 1000, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, 0, 0, 0, i * 1000, tzinfo=timezone.utc),
                side="Buy",
                price=Decimal("100000.0"),
                size=Decimal("0.001"),
            )
            trades.append(trade)

        with db.get_session() as session:
            repo = PublicTradeRepository(session)
            inserted = repo.bulk_insert(trades)
            assert inserted == 500


class TestPrivateExecutionsPipeline:
    """Test private executions flowing into database."""

    def test_bulk_insert_executions(self, seeded_db):
        """Bulk insert executions with run_id and query them back."""
        db = seeded_db.db
        run_id = seeded_db.run_id
        account_id = seeded_db.account_id

        executions = []
        for i in range(5):
            exc = PrivateExecution(
                run_id=run_id,
                account_id=account_id,
                # PrivateExecution has no user_id field
                symbol="BTCUSDT",
                exec_id=f"exec_{i}",
                order_id=f"order_{i}",
                order_link_id=f"link_{i}",
                exchange_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                side="Buy",
                exec_price=Decimal("100000.0"),
                exec_qty=Decimal("0.001"),
                exec_fee=Decimal("0.1"),
            )
            executions.append(exc)

        with db.get_session() as session:
            repo = PrivateExecutionRepository(session)
            inserted = repo.bulk_insert(executions)
            assert inserted == 5

    def test_duplicate_executions_skipped(self, seeded_db):
        """Duplicate exec_id should be skipped."""
        db = seeded_db.db
        run_id = seeded_db.run_id
        account_id = seeded_db.account_id

        exc = PrivateExecution(
            run_id=run_id,
            account_id=account_id,
            # PrivateExecution has no user_id field
            symbol="BTCUSDT",
            exec_id="dup_exec_1",
            order_id="order_1",
            order_link_id="link_1",
            exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            side="Buy",
            exec_price=Decimal("100000.0"),
            exec_qty=Decimal("0.001"),
            exec_fee=Decimal("0.1"),
        )

        with db.get_session() as session:
            repo = PrivateExecutionRepository(session)
            first = repo.bulk_insert([exc])
            assert first == 1

        exc2 = PrivateExecution(
            run_id=run_id,
            account_id=account_id,
            # PrivateExecution has no user_id field
            symbol="BTCUSDT",
            exec_id="dup_exec_1",
            order_id="order_1",
            order_link_id="link_1",
            exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            side="Buy",
            exec_price=Decimal("100000.0"),
            exec_qty=Decimal("0.001"),
            exec_fee=Decimal("0.1"),
        )

        with db.get_session() as session:
            repo = PrivateExecutionRepository(session)
            second = repo.bulk_insert([exc2])
            assert second == 0

    def test_cascade_delete_removes_executions(self, seeded_db):
        """Deleting a Run should cascade-delete its executions."""
        db = seeded_db.db
        run_id = seeded_db.run_id
        account_id = seeded_db.account_id

        exc = PrivateExecution(
            run_id=run_id,
            account_id=account_id,
            # PrivateExecution has no user_id field
            symbol="BTCUSDT",
            exec_id="cascade_test",
            order_id="order_cascade",
            order_link_id="link_cascade",
            exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            side="Sell",
            exec_price=Decimal("100000.0"),
            exec_qty=Decimal("0.001"),
            exec_fee=Decimal("0.1"),
        )

        with db.get_session() as session:
            repo = PrivateExecutionRepository(session)
            repo.bulk_insert([exc])

        # Delete the run
        with db.get_session() as session:
            run = session.get(Run, run_id)
            session.delete(run)

        # Executions should be gone
        with db.get_session() as session:
            repo = PrivateExecutionRepository(session)
            assert repo.exists_by_exec_id("cascade_test") is False


class TestWriterPipeline:
    """Test EventSaver writers → Database end-to-end pipeline."""

    @pytest.mark.asyncio
    async def test_trade_writer_flush_persists_to_db(self, db):
        """TradeWriter.write() + flush() → trades queryable via repository."""
        writer = TradeWriter(db=db, batch_size=100, flush_interval=60.0)

        events = [
            PublicTradeEvent(
                event_type=EventType.PUBLIC_TRADE,
                symbol="BTCUSDT",
                exchange_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                trade_id=f"writer_trade_{i}",
                side="Buy" if i % 2 == 0 else "Sell",
                price=Decimal("100000.0"),
                size=Decimal("0.001"),
            )
            for i in range(5)
        ]

        await writer.write(events)
        await writer.flush()

        with db.get_session() as session:
            repo = PublicTradeRepository(session)
            for i in range(5):
                assert repo.exists_by_trade_id(f"writer_trade_{i}")

    @pytest.mark.asyncio
    async def test_execution_writer_flush_persists_to_db(self, seeded_db):
        """ExecutionWriter.write() + flush() → executions queryable via repository."""
        db = seeded_db.db
        run_id = seeded_db.run_id
        account_id = seeded_db.account_id

        writer = ExecutionWriter(db=db, batch_size=100, flush_interval=60.0)

        events = [
            ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol="BTCUSDT",
                exchange_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                run_id=run_id,
                account_id=account_id,
                exec_id=f"writer_exec_{i}",
                order_id=f"writer_order_{i}",
                order_link_id=f"writer_link_{i}",
                side="Buy",
                price=Decimal("100000.0"),
                qty=Decimal("0.001"),
                fee=Decimal("0.05"),
            )
            for i in range(3)
        ]

        await writer.write(events)
        await writer.flush()

        with db.get_session() as session:
            repo = PrivateExecutionRepository(session)
            for i in range(3):
                assert repo.exists_by_exec_id(f"writer_exec_{i}")

    @pytest.mark.asyncio
    async def test_execution_writer_filters_events_without_run_id(self, db):
        """Events with run_id=None should not be persisted."""
        writer = ExecutionWriter(db=db, batch_size=100, flush_interval=60.0)

        events = [
            ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol="BTCUSDT",
                exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
                run_id=None,  # No run_id
                account_id=uuid4(),
                exec_id="no_run_exec",
                order_id="no_run_order",
                side="Buy",
                price=Decimal("100000.0"),
                qty=Decimal("0.001"),
            ),
        ]

        await writer.write(events)
        await writer.flush()

        with db.get_session() as session:
            repo = PrivateExecutionRepository(session)
            assert repo.exists_by_exec_id("no_run_exec") is False

    @pytest.mark.asyncio
    async def test_trade_writer_duplicate_handling(self, db):
        """Writing same trade_id twice results in only 1 row in DB."""
        writer = TradeWriter(db=db, batch_size=100, flush_interval=60.0)

        event = PublicTradeEvent(
            event_type=EventType.PUBLIC_TRADE,
            symbol="BTCUSDT",
            exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            local_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
            trade_id="dup_writer_trade",
            side="Buy",
            price=Decimal("100000.0"),
            size=Decimal("0.001"),
        )

        # Write and flush twice with same trade_id
        await writer.write([event])
        await writer.flush()

        await writer.write([event])
        await writer.flush()

        # Should only have 1 row
        with db.get_session() as session:
            repo = PublicTradeRepository(session)
            assert repo.exists_by_trade_id("dup_writer_trade")
            # Verify only 1 row via bulk_insert returning 0 for duplicate
            trade_model = PublicTrade(
                symbol="BTCUSDT",
                trade_id="dup_writer_trade",
                exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
                side="Buy",
                price=Decimal("100000.0"),
                size=Decimal("0.001"),
            )
            inserted = repo.bulk_insert([trade_model])
            assert inserted == 0  # Already exists


class TestOrderWriterPipeline:
    """Test OrderWriter → Database end-to-end pipeline."""

    @pytest.mark.asyncio
    async def test_order_writer_flush_persists_to_db(self, seeded_db):
        """OrderWriter.write() + flush() → orders queryable via repository."""
        db = seeded_db.db
        run_id = seeded_db.run_id
        account_id = seeded_db.account_id

        writer = OrderWriter(db=db, batch_size=100, flush_interval=60.0)

        events = [
            OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                exchange_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, 0, 0, i, tzinfo=timezone.utc),
                run_id=run_id,
                account_id=account_id,
                order_id=f"order_{i}",
                order_link_id=f"link_{i}",
                status="New",
                side="Buy",
                price=Decimal("42000.0"),
                qty=Decimal("0.1"),
                leaves_qty=Decimal("0.1"),
            )
            for i in range(3)
        ]

        await writer.write(account_id, events)
        await writer.flush()

        with db.get_session() as session:
            repo = OrderRepository(session)
            orders = repo.get_by_run_range(
                str(run_id),
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                datetime(2025, 1, 2, tzinfo=timezone.utc),
            )
            assert len(orders) == 3

    @pytest.mark.asyncio
    async def test_order_writer_duplicate_updates_status(self, seeded_db):
        """Writing same order_id twice with different status triggers ON CONFLICT DO UPDATE."""
        db = seeded_db.db
        run_id = seeded_db.run_id
        account_id = seeded_db.account_id

        writer = OrderWriter(db=db, batch_size=100, flush_interval=60.0)

        ts = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        event_new = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            run_id=run_id,
            account_id=account_id,
            order_id="dup_order_1",
            status="New",
            side="Buy",
            price=Decimal("42000.0"),
            qty=Decimal("0.1"),
            leaves_qty=Decimal("0.1"),
        )

        event_filled = OrderUpdateEvent(
            event_type=EventType.ORDER_UPDATE,
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            run_id=run_id,
            account_id=account_id,
            order_id="dup_order_1",
            status="Filled",
            side="Buy",
            price=Decimal("42000.0"),
            qty=Decimal("0.1"),
            leaves_qty=Decimal("0.0"),
        )

        # Write first, then update with same (account_id, order_id, exchange_ts)
        await writer.write(account_id, [event_new])
        await writer.flush()

        await writer.write(account_id, [event_filled])
        await writer.flush()

        with db.get_session() as session:
            repo = OrderRepository(session)
            orders = repo.get_by_run_range(
                str(run_id),
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                datetime(2025, 1, 2, tzinfo=timezone.utc),
            )
            # ON CONFLICT DO UPDATE means 1 row with updated status
            assert len(orders) == 1
            assert orders[0].status == "Filled"
            assert orders[0].leaves_qty == Decimal("0.0")

    @pytest.mark.asyncio
    async def test_order_writer_filters_events_without_run_id(self, db):
        """Events with run_id=None should not be persisted."""
        writer = OrderWriter(db=db, batch_size=100, flush_interval=60.0)

        events = [
            OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol="BTCUSDT",
                exchange_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
                local_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
                run_id=None,  # No run_id
                account_id=uuid4(),
                order_id="no_run_order",
                status="New",
                side="Buy",
                price=Decimal("42000.0"),
                qty=Decimal("0.1"),
                leaves_qty=Decimal("0.1"),
            ),
        ]

        await writer.write(uuid4(), events)
        await writer.flush()

        with db.get_session() as session:
            repo = OrderRepository(session)
            orders = repo.get_by_run_range(
                "nonexistent",
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                datetime(2025, 1, 2, tzinfo=timezone.utc),
            )
            assert len(orders) == 0
