"""End-to-end tests for ReplayEngine seeding (feature 0029, Phase 3).

These tests construct a ``ReplayEngine`` with ``seed.enabled=True`` against
an in-memory recorder DB pre-populated with one of each snapshot dimension
(positions long+short, wallet, active order) plus a ``GridStateStore`` JSON
file. They stop short of running ticks — the seed payload is verified
directly on the constructed ``BacktestRunner``, ``BacktestSession`` and
``BacktestOrderManager`` to keep the test focused on the wiring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from grid_db import (
    BybitAccount,
    GridStateSnapshot,
    Order,
    OrderRepository,
    PositionSnapshot,
    PositionSnapshotRepository,
    Run,
    Strategy,
    User,
    WalletSnapshot,
    WalletSnapshotRepository,
)
from grid_db.repositories import GridStateSnapshotRepository

from gridcore.persistence import GridStateStore, grid_fingerprint_hash

from replay.config import ReplayConfig, ReplayStrategyConfig, SeedConfig
from replay.engine import ReplayEngine
from replay.snapshot_loader import SeedDataQualityError


# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------


SYMBOL = "BTCUSDT"
STRAT_ID = "ltcusdt_test"  # arbitrary key; matches GridStateStore convention


@pytest.fixture
def seed_ts():
    """Wall-clock moment at which we seed (replay window start)."""
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def snapshot_ts(seed_ts):
    """Initial REST snapshot lands well before seed.at_ts (pre-check happy)."""
    return seed_ts - timedelta(seconds=60)


@pytest.fixture
def seeded_db(db, seed_ts, snapshot_ts):
    """Populate the in-memory DB with one row per snapshot dimension.

    Layout matches the recorder's "initial REST snapshot" contract:
      * Wallet: one USDT row at ``snapshot_ts``.
      * Positions: BOTH sides (long/short) at ``snapshot_ts``; long has
        ``size>0`` (real position), short has ``size=0`` (zero-row half).
      * Orders: one open buy at ``snapshot_ts`` + the prerequisite Run row.
    """
    with db.get_session() as session:
        user = User(user_id="user-1", username="seedtest")
        account = BybitAccount(
            account_id="acc-1",
            user_id="user-1",
            account_name="seed_account",
            environment="testnet",
        )
        strategy = Strategy(
            strategy_id="strat-1",
            account_id="acc-1",
            strategy_type="recorder",
            symbol=SYMBOL,
            config_json={},
        )
        run = Run(
            run_id="seed-run",
            user_id="user-1",
            account_id="acc-1",
            strategy_id="strat-1",
            run_type="recording",
            status="running",
            start_ts=snapshot_ts - timedelta(minutes=5),
            end_ts=seed_ts + timedelta(hours=1),
        )
        session.add_all([user, account, strategy, run])
        session.commit()

        PositionSnapshotRepository(session).bulk_insert([
            PositionSnapshot(
                run_id="seed-run",
                account_id="acc-1",
                symbol=SYMBOL,
                exchange_ts=snapshot_ts,
                local_ts=snapshot_ts,
                side="Buy",  # long
                size=Decimal("1.5"),
                entry_price=Decimal("100000"),
                liq_price=Decimal("90000"),
            ),
            PositionSnapshot(
                run_id="seed-run",
                account_id="acc-1",
                symbol=SYMBOL,
                exchange_ts=snapshot_ts,
                local_ts=snapshot_ts,
                side="Sell",  # short — present but zero size, per contract
                size=Decimal("0"),
                entry_price=Decimal("0"),
                liq_price=None,
            ),
        ])
        WalletSnapshotRepository(session).bulk_insert([
            WalletSnapshot(
                run_id="seed-run",
                account_id="acc-1",
                exchange_ts=snapshot_ts,
                local_ts=snapshot_ts,
                coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
                total_equity=Decimal("15000.50"),
                total_available_balance=Decimal("14000.25"),
                total_margin_balance=Decimal("14900.75"),
                account_im_rate=Decimal("0.01000000"),
                account_mm_rate=Decimal("0.00500000"),
            ),
        ])
        OrderRepository(session).bulk_insert([
            Order(
                run_id="seed-run",
                account_id="acc-1",
                order_id="ORD-SEED-1",
                order_link_id="abcdef0123456789-1715170800000",
                symbol=SYMBOL,
                exchange_ts=snapshot_ts,
                local_ts=snapshot_ts,
                status="New",
                side="Buy",
                price=Decimal("99000"),
                qty=Decimal("0.1"),
                leaves_qty=Decimal("0.1"),
                reduce_only=False,
            ),
        ])
        session.commit()

    return db


@pytest.fixture
def grid_state_path(tmp_path):
    """Write a small grid-state JSON file matching the strategy fingerprint."""
    path = tmp_path / "grid_anchor.json"
    store = GridStateStore(file_path=str(path))
    # Strict ascending price + Buy...Buy → Sell...Sell pattern
    # so Grid.is_grid_correct() accepts the restored layout.
    grid = [
        {"side": "Buy", "price": 99600.0},
        {"side": "Buy", "price": 99800.0},
        {"side": "Sell", "price": 100200.0},
        {"side": "Sell", "price": 100400.0},
    ]
    store.save(STRAT_ID, grid, grid_step=0.2, grid_count=4)
    store.flush()
    return str(path)


@pytest.fixture
def seed_config(seed_ts, grid_state_path):
    return SeedConfig(
        enabled=True,
        at_ts=seed_ts,
        account_id="acc-1",
        strat_id=STRAT_ID,
        grid_state_path=grid_state_path,
        wallet_coin="USDT",
    )


@pytest.fixture
def replay_config(seed_ts, seed_config):
    """Replay config using the seed fingerprint matching grid_state_path."""
    return ReplayConfig(
        database_url="sqlite:///:memory:",
        run_id="seed-run",
        symbol=SYMBOL,
        start_ts=seed_ts,
        end_ts=seed_ts + timedelta(hours=1),
        strategy=ReplayStrategyConfig(
            tick_size=Decimal("0.1"),
            grid_count=4,
            grid_step=0.2,
            enable_risk_multipliers=True,
        ),
        initial_balance=Decimal("10000"),
        enable_funding=False,
        seed=seed_config,
    )


@pytest.fixture
def mock_instrument():
    """Patch the InstrumentInfoProvider — the test does not run ticks."""
    with patch("replay.engine.InstrumentInfoProvider") as mock_cls:
        info = MagicMock()
        info.qty_step = Decimal("0.001")
        info.tick_size = Decimal("0.1")
        info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_cls.return_value.get.return_value = info
        yield info


# ---------------------------------------------------------------------------
# A. Happy path — full pipeline seeds correctly
# ---------------------------------------------------------------------------


class TestReplayEngineSeedingPipeline:
    """End-to-end: every seed dimension lands on the constructed runner."""

    def test_replay_engine_seeds_through_full_pipeline(
        self, seeded_db, replay_config, mock_instrument,
    ):
        """Construct the engine and inspect the BacktestRunner directly.

        We exercise ``_load_seed`` + ``_init_runner`` by calling them
        rather than ``run()`` so the test does not depend on a data
        provider with the right shape — Phase 3 is about wiring, not
        execution.
        """
        engine = ReplayEngine(config=replay_config, db=seeded_db)

        # Mirror the run() call sequence: resolve run → load seed → build runner.
        run_id, _account_id, _start, _end = engine._resolve_run(replay_config)
        assert run_id == "seed-run"

        wallet_seed, long_seed, short_seed, grid_seed, order_seeds = engine._load_seed(
            replay_config, run_id,
        )

        # Seed payload survived loading.
        assert wallet_seed.coin_balance == Decimal("12345.67")
        assert wallet_seed.total_available_balance == Decimal("14000.25")
        assert long_seed.size == Decimal("1.5")
        assert long_seed.entry_price == Decimal("100000")
        assert long_seed.liquidation_price == Decimal("90000")
        assert short_seed.size == Decimal("0")
        assert grid_seed is not None
        assert len(grid_seed.grid) == 4
        assert len(order_seeds) == 1
        assert order_seeds[0].client_id == "abcdef0123456789"

        # Build session + runner the same way run() does.
        from backtest.config import BacktestStrategyConfig
        from backtest.session import BacktestSession

        strategy_config = BacktestStrategyConfig(
            strat_id="replay_btcusdt",
            symbol=SYMBOL,
            tick_size=replay_config.strategy.tick_size,
            grid_count=replay_config.strategy.grid_count,
            grid_step=replay_config.strategy.grid_step,
            amount=replay_config.strategy.amount,
            max_margin=replay_config.strategy.max_margin,
            early_imbalance_multiplier=replay_config.strategy.early_imbalance_multiplier,
            commission_rate=replay_config.strategy.commission_rate,
            enable_risk_multipliers=replay_config.strategy.enable_risk_multipliers,
        )
        session = BacktestSession(initial_balance=wallet_seed.total_available_balance)
        runner = engine._init_runner(
            strategy_config,
            session,
            long_seed=long_seed,
            short_seed=short_seed,
            grid_seed=grid_seed,
            order_seeds=order_seeds,
        )

        # Wallet → BacktestSession.initial_balance.
        assert session.initial_balance == Decimal("14000.25")

        # Position seeds → trackers.
        assert runner.long_tracker.state.size == Decimal("1.5")
        assert runner.long_tracker.state.avg_entry_price == Decimal("100000")
        assert runner.long_tracker.state.liquidation_price == Decimal("90000")
        # Short seed is zero — tracker stays at default.
        assert runner.short_tracker.state.size == Decimal("0")

        # Position seeds also propagated into gridcore.Position when risk is on.
        assert runner._long_position is not None
        assert runner._long_position.size == Decimal("1.5")
        assert runner._long_position.liquidation_price == Decimal("90000")
        # Short Position stays at default (zero seed → no copy).
        assert runner._short_position is not None
        assert runner._short_position.size == Decimal("0")

        # Active order seed → BacktestOrderManager.active_orders +
        # _client_order_ids. Keyed by exchange_order_id (not client_id).
        order_manager = runner.order_manager
        assert "ORD-SEED-1" in order_manager.active_orders
        seeded_order = order_manager.active_orders["ORD-SEED-1"]
        assert seeded_order.client_order_id == "abcdef0123456789"
        assert seeded_order.side == "Buy"
        assert seeded_order.direction == "long"
        assert seeded_order.price == Decimal("99000")
        assert seeded_order.qty == Decimal("0.1")
        assert seeded_order.reduce_only is False
        assert "abcdef0123456789" in order_manager._client_order_ids

        # Grid seed → GridEngine.grid was restored from the seeded levels.
        # Grid.restore_grid keeps the level list intact.
        assert len(runner.engine.grid.grid) == 4

    def test_null_0042_wallet_fields_fall_back_to_config_balance(
        self, seeded_db, replay_config, mock_instrument,
    ):
        """Legacy migrated wallet rows do not override config.initial_balance."""
        with seeded_db.get_session() as session:
            wallet = session.query(WalletSnapshot).filter_by(run_id="seed-run").one()
            wallet.total_available_balance = None
            session.commit()

        engine = ReplayEngine(config=replay_config, db=seeded_db)
        run_id, _account_id, _start, _end = engine._resolve_run(replay_config)
        wallet_seed, long_seed, short_seed, grid_seed, order_seeds = engine._load_seed(
            replay_config, run_id,
        )

        assert wallet_seed is None

        from backtest.session import BacktestSession

        initial_balance = (
            wallet_seed.total_available_balance
            if wallet_seed is not None
            else replay_config.initial_balance
        )
        session = BacktestSession(initial_balance=initial_balance)
        assert session.initial_balance == replay_config.initial_balance
        assert long_seed is not None
        assert short_seed is not None
        assert grid_seed is not None
        assert order_seeds is not None

    def test_load_seed_prefers_db_over_file(
        self, seeded_db, replay_config, snapshot_ts, mock_instrument,
    ):
        """0047: when a DB snapshot covers ``at_ts``, engine returns it
        — the file grid (different payload) is ignored."""
        db_grid = [
            {"side": "Buy", "price": 90000.0},
            {"side": "Buy", "price": 90200.0},
            {"side": "Sell", "price": 90600.0},
            {"side": "Sell", "price": 90800.0},
        ]
        live_run_id = "gridbot-live-run-db-pref"
        with seeded_db.get_session() as session:
            session.add(
                Run(
                    run_id=live_run_id,
                    user_id="user-1",
                    account_id="acc-1",
                    strategy_id="strat-1",
                    run_type="live",
                    status="running",
                    start_ts=snapshot_ts - timedelta(minutes=30),
                    end_ts=None,
                )
            )
            session.flush()
            GridStateSnapshotRepository(session).insert(
                GridStateSnapshot(
                    run_id=live_run_id, account_id="acc-1", strat_id=STRAT_ID,
                    symbol=SYMBOL,
                    exchange_ts=snapshot_ts, local_ts=snapshot_ts,
                    grid_json=db_grid, grid_step=Decimal("0.2"), grid_count=4,
                    raw_fingerprint=grid_fingerprint_hash(db_grid, 0.2, 4),
                )
            )
            session.commit()

        engine = ReplayEngine(config=replay_config, db=seeded_db)
        run_id, *_ = engine._resolve_run(replay_config)
        _, _, _, grid_seed, _ = engine._load_seed(replay_config, run_id)
        assert grid_seed is not None
        assert grid_seed.grid == db_grid

    def test_load_seed_prefers_db_snapshot_from_other_run_id(
        self, seeded_db, replay_config, snapshot_ts, mock_instrument, caplog,
    ):
        """0052 regression guard: gridbot's live ``run_id`` differs from
        the recorder's ``run_id``. The replay engine must still load the
        snapshot via the cross-run lookup.

        The pre-0052 ``test_load_seed_prefers_db_over_file`` happened to
        insert under the same ``run_id="seed-run"`` that the replay
        resolves, so it passed under the broken per-run filter and did
        not catch the production failure mode.
        """
        import logging

        live_run_id = "gridbot-live-run"
        db_grid = [
            {"side": "Buy", "price": 91000.0},
            {"side": "Buy", "price": 91200.0},
            {"side": "Sell", "price": 91600.0},
            {"side": "Sell", "price": 91800.0},
        ]
        with seeded_db.get_session() as session:
            # Run row for the FK; gridbot ran under its own ``live`` run.
            session.add(
                Run(
                    run_id=live_run_id,
                    user_id="user-1",
                    account_id="acc-1",
                    strategy_id="strat-1",
                    run_type="live",
                    status="running",
                    start_ts=snapshot_ts - timedelta(minutes=30),
                    end_ts=None,
                )
            )
            session.flush()
            GridStateSnapshotRepository(session).insert(
                GridStateSnapshot(
                    run_id=live_run_id, account_id="acc-1", strat_id=STRAT_ID,
                    symbol=SYMBOL,
                    exchange_ts=snapshot_ts, local_ts=snapshot_ts,
                    grid_json=db_grid, grid_step=Decimal("0.2"), grid_count=4,
                    raw_fingerprint=grid_fingerprint_hash(db_grid, 0.2, 4),
                )
            )
            session.commit()

        engine = ReplayEngine(config=replay_config, db=seeded_db)
        run_id, *_ = engine._resolve_run(replay_config)
        assert run_id == "seed-run"  # recorder's run_id, not the gridbot one.

        with caplog.at_level(logging.INFO):
            _, _, _, grid_seed, _ = engine._load_seed(replay_config, run_id)

        assert grid_seed is not None
        assert grid_seed.grid == db_grid
        # End-to-end engine wiring: the "grid seed source=db" line must
        # fire (confirms engine took the DB branch, not just the loader).
        assert any(
            "grid seed source=db" in rec.message for rec in caplog.records
        )

    def test_load_seed_raises_when_only_ended_gridbot_run_has_snapshots(
        self, seeded_db, replay_config, seed_ts, snapshot_ts, mock_instrument,
    ):
        """Completed live run snapshots must not seed replay after ``end_ts``.

        When gridbot restarts, ``seed.at_ts`` is often after the new run's
        ``start_ts`` but before its bootstrap snapshot. Without filtering out
        ended runs, replay would load the previous run's grid and pass
        validation with the wrong seed (0054 only raises when *no* row exists).
        """
        ended_grid = [
            {"side": "Buy", "price": 88000.0},
            {"side": "Buy", "price": 88200.0},
            {"side": "Sell", "price": 88600.0},
            {"side": "Sell", "price": 88800.0},
        ]
        with seeded_db.get_session() as session:
            ended_run = Run(
                run_id="ended-gridbot-run",
                user_id="user-1",
                account_id="acc-1",
                strategy_id="strat-1",
                run_type="live",
                status="completed",
                start_ts=seed_ts - timedelta(hours=2),
                end_ts=seed_ts - timedelta(minutes=5),
            )
            session.add(ended_run)
            session.flush()
            GridStateSnapshotRepository(session).insert(
                GridStateSnapshot(
                    run_id="ended-gridbot-run",
                    account_id="acc-1",
                    strat_id=STRAT_ID,
                    symbol=SYMBOL,
                    exchange_ts=snapshot_ts,
                    local_ts=snapshot_ts,
                    grid_json=ended_grid,
                    grid_step=Decimal("0.2"),
                    grid_count=4,
                    raw_fingerprint=grid_fingerprint_hash(ended_grid, 0.2, 4),
                )
            )
            session.commit()

        config = replay_config.model_copy(
            update={
                "seed": replay_config.seed.model_copy(
                    update={"grid_state_path": None},
                ),
            },
        )
        engine = ReplayEngine(config=config, db=seeded_db)
        run_id, *_ = engine._resolve_run(config)
        with pytest.raises(SeedDataQualityError):
            engine._load_seed(config, run_id)

    def test_load_seed_falls_back_to_file_when_no_db_snapshot(
        self, seeded_db, replay_config, mock_instrument,
    ):
        """0047: no DB row at-or-before at_ts → file path wins."""
        engine = ReplayEngine(config=replay_config, db=seeded_db)
        run_id, *_ = engine._resolve_run(replay_config)
        _, _, _, grid_seed, _ = engine._load_seed(replay_config, run_id)
        assert grid_seed is not None
        # File fixture has 4 levels starting at 99600.0 (see grid_state_path).
        assert grid_seed.grid[0]["price"] == 99600.0

    def test_load_seed_falls_back_to_file_when_table_missing(
        self, seeded_db, replay_config, mock_instrument,
    ):
        """0047 P1: pre-0047 DB (no ``grid_state_snapshots`` table) must
        still drop into the file path so old recorder datasets stay
        usable. Without this, SQLAlchemy raises ``no such table`` and the
        plan's "old datasets stay on file mode" out-of-scope clause breaks.
        """
        GridStateSnapshot.__table__.drop(seeded_db.engine)
        try:
            engine = ReplayEngine(config=replay_config, db=seeded_db)
            run_id, *_ = engine._resolve_run(replay_config)
            _, _, _, grid_seed, _ = engine._load_seed(replay_config, run_id)
        finally:
            GridStateSnapshot.__table__.create(seeded_db.engine)

        assert grid_seed is not None
        # File fixture has 4 levels starting at 99600.0.
        assert grid_seed.grid[0]["price"] == 99600.0

# ---------------------------------------------------------------------------
# B. Pre-check: seed.at_ts too early relative to initial REST snapshot
# ---------------------------------------------------------------------------


class TestReplayEnginePreCheck:
    """Engine refuses to seed when the recorder snapshot has not landed."""

    def test_replay_engine_pre_check_rejects_when_at_ts_too_early(
        self, db, seed_ts, snapshot_ts, grid_state_path, mock_instrument,
    ):
        """Snapshot rows at T0; seed.at_ts at T0-1s → ValueError surface.

        The error message must include both the seed.at_ts and the
        snapshot timestamp so the operator can diagnose without
        re-querying the DB.
        """
        # Insert position+wallet at T0 (snapshot_ts), no orders.
        snapshot_t0 = snapshot_ts
        with db.get_session() as session:
            user = User(user_id="user-1", username="precheck")
            account = BybitAccount(
                account_id="acc-1", user_id="user-1",
                account_name="seed", environment="testnet",
            )
            strategy = Strategy(
                strategy_id="strat-1", account_id="acc-1",
                strategy_type="recorder", symbol=SYMBOL, config_json={},
            )
            run = Run(
                run_id="precheck-run",
                user_id="user-1", account_id="acc-1", strategy_id="strat-1",
                run_type="recording", status="running",
                start_ts=snapshot_t0 - timedelta(minutes=1),
                end_ts=snapshot_t0 + timedelta(hours=1),
            )
            session.add_all([user, account, strategy, run])
            session.commit()

            PositionSnapshotRepository(session).bulk_insert([
                PositionSnapshot(
                    run_id="precheck-run", account_id="acc-1", symbol=SYMBOL,
                    exchange_ts=snapshot_t0, local_ts=snapshot_t0,
                    side="Buy", size=Decimal("0"), entry_price=Decimal("0"),
                    liq_price=None,
                ),
                PositionSnapshot(
                    run_id="precheck-run", account_id="acc-1", symbol=SYMBOL,
                    exchange_ts=snapshot_t0, local_ts=snapshot_t0,
                    side="Sell", size=Decimal("0"), entry_price=Decimal("0"),
                    liq_price=None,
                ),
            ])
            WalletSnapshotRepository(session).bulk_insert([
                WalletSnapshot(
                    run_id="precheck-run", account_id="acc-1",
                    exchange_ts=snapshot_t0, local_ts=snapshot_t0,
                    coin="USDT",
                    wallet_balance=Decimal("100"), available_balance=Decimal("100"),
                ),
            ])
            session.commit()

        early_at_ts = snapshot_t0 - timedelta(seconds=1)
        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="precheck-run",
            symbol=SYMBOL,
            start_ts=seed_ts,
            end_ts=seed_ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal("0.1"),
                grid_count=4,
                grid_step=0.2,
                enable_risk_multipliers=True,
            ),
            initial_balance=Decimal("10000"),
            enable_funding=False,
            seed=SeedConfig(
                enabled=True,
                at_ts=early_at_ts,
                account_id="acc-1",
                strat_id=STRAT_ID,
                grid_state_path=grid_state_path,
            ),
        )

        engine = ReplayEngine(config=config, db=db)

        with pytest.raises(ValueError) as excinfo:
            engine._load_seed(config, "precheck-run")

        msg = str(excinfo.value)
        # Both timestamps must be in the message for diagnosability.
        assert "precheck-run" in msg
        # at_ts (the rejected one) appears.
        assert str(early_at_ts.year) in msg  # at least the year identifies the ts
        # latest_min / wallet_min / position_min appears (snapshot_t0).
        assert "wallet_min" in msg or "position_min" in msg or "latest_min" in msg

    def test_pre_check_rejects_when_wallet_snapshot_missing(
        self, db, seed_ts, snapshot_ts, grid_state_path, mock_instrument,
    ):
        """Position snapshot exists but wallet does not → pre-check fails."""
        with db.get_session() as session:
            user = User(user_id="user-1", username="precheck")
            account = BybitAccount(
                account_id="acc-1", user_id="user-1",
                account_name="seed", environment="testnet",
            )
            strategy = Strategy(
                strategy_id="strat-1", account_id="acc-1",
                strategy_type="recorder", symbol=SYMBOL, config_json={},
            )
            run = Run(
                run_id="no-wallet-run",
                user_id="user-1", account_id="acc-1", strategy_id="strat-1",
                run_type="recording", status="running",
                start_ts=snapshot_ts - timedelta(minutes=1),
                end_ts=seed_ts + timedelta(hours=1),
            )
            session.add_all([user, account, strategy, run])
            session.commit()

            PositionSnapshotRepository(session).bulk_insert([
                PositionSnapshot(
                    run_id="no-wallet-run", account_id="acc-1", symbol=SYMBOL,
                    exchange_ts=snapshot_ts, local_ts=snapshot_ts,
                    side="Buy", size=Decimal("0"), entry_price=Decimal("0"),
                    liq_price=None,
                ),
                PositionSnapshot(
                    run_id="no-wallet-run", account_id="acc-1", symbol=SYMBOL,
                    exchange_ts=snapshot_ts, local_ts=snapshot_ts,
                    side="Sell", size=Decimal("0"), entry_price=Decimal("0"),
                    liq_price=None,
                ),
            ])
            session.commit()

        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="no-wallet-run",
            symbol=SYMBOL,
            start_ts=seed_ts, end_ts=seed_ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal("0.1"), grid_count=4, grid_step=0.2,
                enable_risk_multipliers=True,
            ),
            initial_balance=Decimal("10000"),
            enable_funding=False,
            seed=SeedConfig(
                enabled=True, at_ts=seed_ts,
                account_id="acc-1", strat_id=STRAT_ID,
                grid_state_path=grid_state_path,
            ),
        )

        engine = ReplayEngine(config=config, db=db)
        with pytest.raises(ValueError, match="wallet_snapshots"):
            engine._load_seed(config, "no-wallet-run")


# ---------------------------------------------------------------------------
# C. Disabled seed preserves blank-start behaviour
# ---------------------------------------------------------------------------


class TestReplayEngineSeedDisabled:
    def test_disabled_seed_returns_all_none(self, db, seed_ts, mock_instrument):
        config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="any-run",
            symbol=SYMBOL,
            start_ts=seed_ts,
            end_ts=seed_ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(tick_size=Decimal("0.1")),
            initial_balance=Decimal("10000"),
            enable_funding=False,
        )
        assert config.seed.enabled is False

        engine = ReplayEngine(config=config, db=db)
        result = engine._load_seed(config, "any-run")
        assert result == (None, None, None, None, None)


# ---------------------------------------------------------------------------
# D. Fail-loud on missing grid seed (feature 0054)
# ---------------------------------------------------------------------------


class TestReplayEngineSeedFailLoud:
    """``seed.enabled=True`` requires a loadable grid; wallet is soft."""

    def test_load_seed_raises_when_no_grid_db_and_no_file_path(
        self, seeded_db, seed_ts, mock_instrument,
    ):
        """No DB row AND ``grid_state_path is None`` → SeedDataQualityError.

        Primary regression for the 0052 silent-fallback bug: replay used
        to log ``grid seed source=fresh`` and march on.
        """
        seed_config = SeedConfig(
            enabled=True,
            at_ts=seed_ts,
            account_id="acc-1",
            strat_id=STRAT_ID,
            grid_state_path=None,
            wallet_coin="USDT",
        )
        replay_config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="seed-run",
            symbol=SYMBOL,
            start_ts=seed_ts,
            end_ts=seed_ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal("0.1"),
                grid_count=4,
                grid_step=0.2,
                enable_risk_multipliers=True,
            ),
            initial_balance=Decimal("10000"),
            enable_funding=False,
            seed=seed_config,
        )
        engine = ReplayEngine(config=replay_config, db=seeded_db)
        run_id, *_ = engine._resolve_run(replay_config)

        with pytest.raises(SeedDataQualityError) as excinfo:
            engine._load_seed(replay_config, run_id)

        msg = str(excinfo.value)
        assert seed_ts.isoformat() in msg
        assert "acc-1" in msg
        assert STRAT_ID in msg
        assert SYMBOL in msg
        assert "grid_state_path=None" in msg

    def test_load_seed_raises_when_no_grid_db_and_file_path_missing(
        self, seeded_db, seed_ts, tmp_path, mock_instrument,
    ):
        """grid_state_path points at a missing file → loader returns None
        → SeedDataQualityError (no DB row either)."""
        missing_path = str(tmp_path / "does_not_exist.json")
        seed_config = SeedConfig(
            enabled=True,
            at_ts=seed_ts,
            account_id="acc-1",
            strat_id=STRAT_ID,
            grid_state_path=missing_path,
            wallet_coin="USDT",
        )
        replay_config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="seed-run",
            symbol=SYMBOL,
            start_ts=seed_ts,
            end_ts=seed_ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal("0.1"),
                grid_count=4,
                grid_step=0.2,
                enable_risk_multipliers=True,
            ),
            initial_balance=Decimal("10000"),
            enable_funding=False,
            seed=seed_config,
        )
        engine = ReplayEngine(config=replay_config, db=seeded_db)
        run_id, *_ = engine._resolve_run(replay_config)

        with pytest.raises(SeedDataQualityError):
            engine._load_seed(replay_config, run_id)

    def test_load_seed_raises_when_grid_step_count_mismatch(
        self, seeded_db, seed_ts, snapshot_ts, mock_instrument,
    ):
        """DB snapshot exists with mismatched grid_step/grid_count → loader
        returns None (same contract as absence) → SeedDataQualityError.

        Regression for the explicit ``mismatch ≡ absence`` rule.
        """
        mismatched_grid = [
            {"side": "Buy", "price": 99600.0},
            {"side": "Buy", "price": 99800.0},
            {"side": "Sell", "price": 100200.0},
            {"side": "Sell", "price": 100400.0},
        ]
        with seeded_db.get_session() as session:
            GridStateSnapshotRepository(session).insert(
                GridStateSnapshot(
                    run_id="seed-run", account_id="acc-1", strat_id=STRAT_ID,
                    symbol=SYMBOL,
                    exchange_ts=snapshot_ts, local_ts=snapshot_ts,
                    grid_json=mismatched_grid,
                    grid_step=Decimal("0.5"),  # config expects 0.2 → mismatch
                    grid_count=8,              # config expects 4   → mismatch
                    raw_fingerprint=grid_fingerprint_hash(
                        mismatched_grid, 0.5, 8,
                    ),
                )
            )

        seed_config = SeedConfig(
            enabled=True,
            at_ts=seed_ts,
            account_id="acc-1",
            strat_id=STRAT_ID,
            grid_state_path=None,  # exercise DB-only path
            wallet_coin="USDT",
        )
        replay_config = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="seed-run",
            symbol=SYMBOL,
            start_ts=seed_ts,
            end_ts=seed_ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal("0.1"),
                grid_count=4,
                grid_step=0.2,
                enable_risk_multipliers=True,
            ),
            initial_balance=Decimal("10000"),
            enable_funding=False,
            seed=seed_config,
        )
        engine = ReplayEngine(config=replay_config, db=seeded_db)
        run_id, *_ = engine._resolve_run(replay_config)

        with pytest.raises(SeedDataQualityError):
            engine._load_seed(replay_config, run_id)

    def test_load_seed_warns_when_wallet_seed_missing(
        self, seeded_db, replay_config, seed_ts, grid_state_path,
        mock_instrument, caplog,
    ):
        """Wallet is soft-required: missing coin row → WARNING + fallback,
        no exception. Pre-check (USDT row present) stays happy because we
        leave the USDT row alone and set ``seed.wallet_coin='BTC'``."""
        import logging

        seed_config = SeedConfig(
            enabled=True,
            at_ts=seed_ts,
            account_id="acc-1",
            strat_id=STRAT_ID,
            grid_state_path=grid_state_path,
            # BTC has no snapshot in seeded_db (only USDT). USDT row stays so
            # the pre-check passes; the loader returns None for BTC, exercising
            # the wallet-missing WARNING path without tripping _seed_pre_check.
            wallet_coin="BTC",
        )
        replay_config_btc = ReplayConfig(
            database_url="sqlite:///:memory:",
            run_id="seed-run",
            symbol=SYMBOL,
            start_ts=seed_ts,
            end_ts=seed_ts + timedelta(hours=1),
            strategy=ReplayStrategyConfig(
                tick_size=Decimal("0.1"),
                grid_count=4,
                grid_step=0.2,
                enable_risk_multipliers=True,
            ),
            initial_balance=Decimal("10000"),
            enable_funding=False,
            seed=seed_config,
        )
        engine = ReplayEngine(config=replay_config_btc, db=seeded_db)
        run_id, *_ = engine._resolve_run(replay_config_btc)

        with caplog.at_level(logging.WARNING, logger="replay.engine"):
            wallet_seed, _, _, grid_seed, _ = engine._load_seed(
                replay_config_btc, run_id,
            )

        assert wallet_seed is None
        assert grid_seed is not None

        engine_warnings = [
            r for r in caplog.records
            if r.name == "replay.engine" and r.levelno == logging.WARNING
        ]
        matched = [
            r for r in engine_warnings
            if "defaulting to initial_balance" in r.getMessage()
        ]
        assert len(matched) == 1, (
            f"expected exactly one wallet warning, got {len(matched)}: "
            f"{[r.getMessage() for r in engine_warnings]}"
        )
