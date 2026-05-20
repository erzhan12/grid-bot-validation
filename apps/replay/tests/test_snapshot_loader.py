"""Tests for replay.snapshot_loader (feature 0029, Phase 2A)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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

from replay.snapshot_loader import (
    ActiveOrderSeed,
    GridStateSeed,
    PositionStateSeed,
    SeedDataQualityError,
    SeedSchemaError,
    WalletSeed,
    load_active_orders,
    load_grid_state,
    load_grid_state_from_snapshots,
    load_position_snapshots,
    load_wallet_seed_full,
    load_wallet_snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures (the replay conftest only provides ``db``; we replicate the
# session + sample-entities pattern from shared/db/tests/conftest.py here).
# ---------------------------------------------------------------------------


@pytest.fixture
def session(db):
    """Provide a session per-test, rolling back at teardown."""
    sess = db.session_factory()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()


@pytest.fixture
def sample_user(session):
    user = User(username="testuser", email="test@example.com")
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def sample_account(session, sample_user):
    account = BybitAccount(
        user_id=sample_user.user_id,
        account_name="test_account",
        environment="testnet",
    )
    session.add(account)
    session.flush()
    return account


@pytest.fixture
def sample_strategy(session, sample_account):
    strategy = Strategy(
        account_id=sample_account.account_id,
        strategy_type="GridStrategy",
        symbol="BTCUSDT",
        config_json={"grid_count": 50, "grid_step": 0.2},
    )
    session.add(strategy)
    session.flush()
    return strategy


@pytest.fixture
def sample_run(session, sample_user, sample_account, sample_strategy):
    run = Run(
        user_id=sample_user.user_id,
        account_id=sample_account.account_id,
        strategy_id=sample_strategy.strategy_id,
        run_type="recording",
        status="running",
        start_ts=datetime(2026, 5, 7, 9, 0, 0, tzinfo=timezone.utc),
    )
    session.add(run)
    session.flush()
    return run


@pytest.fixture
def base_ts():
    return datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# load_grid_state
# ---------------------------------------------------------------------------


class TestLoadGridState:
    """Tolerant grid-state loader: returns None on every miss path."""

    def test_happy_path_returns_seed(self, tmp_path):
        store = GridStateStore(file_path=str(tmp_path / "grid.json"))
        grid = [
            {"side": "Buy", "price": 99.8},
            {"side": "Buy", "price": 99.6},
            {"side": "Sell", "price": 100.2},
        ]
        store.save("strat-A", grid, grid_step=0.2, grid_count=3)
        store.flush()

        seed = load_grid_state(store, "strat-A", expected_step=0.2, expected_count=3)
        assert isinstance(seed, GridStateSeed)
        assert seed.strat_id == "strat-A"
        assert seed.grid_step == 0.2
        assert seed.grid_count == 3
        assert seed.grid == grid

    def test_no_entry_logs_info_and_returns_none(self, tmp_path, caplog):
        store = GridStateStore(file_path=str(tmp_path / "grid.json"))

        with caplog.at_level(logging.INFO, logger="replay.snapshot_loader"):
            seed = load_grid_state(store, "missing-strat", 0.2, 50)

        assert seed is None
        assert any(
            "no saved grid state" in rec.message
            for rec in caplog.records
            if rec.name == "replay.snapshot_loader"
        )

    def test_legacy_anchor_only_format_logs_info_and_returns_none(
        self, tmp_path, caplog
    ):
        # Hand-write a legacy entry with no `grid` field. GridStateStore.load
        # already detects this shape, logs INFO, and returns None — the
        # loader propagates and emits its own "no saved grid state" log.
        path = tmp_path / "grid.json"
        import json

        path.write_text(json.dumps({
            "strat-legacy": {
                "anchor_price": 50000.0,
                "grid_step": 0.2,
                "grid_count": 50,
            }
        }))

        store = GridStateStore(file_path=str(path))

        with caplog.at_level(logging.INFO):
            seed = load_grid_state(store, "strat-legacy", 0.2, 50)

        assert seed is None
        # Either the GridStateStore log OR the loader's no-entry log
        # qualifies as "an INFO log explaining why".
        assert any(
            "Legacy anchor format" in rec.message
            or "no saved grid state" in rec.message
            for rec in caplog.records
        )

    def test_step_mismatch_logs_info_and_returns_none(self, tmp_path, caplog):
        store = GridStateStore(file_path=str(tmp_path / "grid.json"))
        grid = [{"side": "Buy", "price": 100.0}]
        store.save("strat-S", grid, grid_step=0.2, grid_count=1)
        store.flush()

        with caplog.at_level(logging.INFO, logger="replay.snapshot_loader"):
            seed = load_grid_state(store, "strat-S", expected_step=0.5, expected_count=1)

        assert seed is None
        assert any(
            "differs from replay config" in rec.message
            for rec in caplog.records
            if rec.name == "replay.snapshot_loader"
        )

    def test_count_mismatch_returns_none(self, tmp_path):
        store = GridStateStore(file_path=str(tmp_path / "grid.json"))
        store.save(
            "strat-C",
            [{"side": "Buy", "price": 100.0}],
            grid_step=0.2,
            grid_count=1,
        )
        store.flush()

        seed = load_grid_state(store, "strat-C", expected_step=0.2, expected_count=99)
        assert seed is None


# ---------------------------------------------------------------------------
# load_grid_state_from_snapshots (feature 0047)
# ---------------------------------------------------------------------------


def _make_grid_row(
    sample_run,
    sample_account,
    *,
    strat_id: str = "strat-A",
    grid: list[dict] | None = None,
    grid_step: float = 0.2,
    grid_count: int = 3,
    exchange_ts: datetime | None = None,
    raw_fingerprint: str | None = None,
) -> GridStateSnapshot:
    grid = grid or [
        {"side": "Buy", "price": 100.0},
        {"side": "Wait", "price": 101.0},
        {"side": "Sell", "price": 102.0},
    ]
    ts = exchange_ts or datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)
    if raw_fingerprint is None:
        raw_fingerprint = grid_fingerprint_hash(grid, grid_step, grid_count)
    return GridStateSnapshot(
        run_id=sample_run.run_id,
        account_id=str(sample_account.account_id),
        strat_id=strat_id,
        symbol="BTCUSDT",
        exchange_ts=ts,
        local_ts=ts,
        grid_json=grid,
        grid_step=Decimal(str(grid_step)),
        grid_count=grid_count,
        raw_fingerprint=raw_fingerprint,
    )


class TestLoadGridStateFromSnapshots:
    """Loader-level coverage for the 0047 DB grid-state path."""

    def test_happy_path_returns_seed(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = GridStateSnapshotRepository(session)
        repo.insert(_make_grid_row(sample_run, sample_account, exchange_ts=base_ts))
        session.flush()

        seed = load_grid_state_from_snapshots(
            session,
            run_id=sample_run.run_id,
            account_id=str(sample_account.account_id),
            strat_id="strat-A",
            at_ts=base_ts + timedelta(minutes=1),
            expected_step=0.2,
            expected_count=3,
        )
        assert seed is not None
        assert seed.strat_id == "strat-A"
        assert seed.grid_step == 0.2
        assert seed.grid_count == 3
        assert seed.grid[0]["side"] == "Buy"

    def test_no_snapshot_returns_none(
        self, session, sample_account, sample_run, base_ts, caplog
    ):
        with caplog.at_level(logging.INFO):
            seed = load_grid_state_from_snapshots(
                session,
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                strat_id="missing",
                at_ts=base_ts,
                expected_step=0.2,
                expected_count=3,
            )
        assert seed is None
        assert any(
            "no grid snapshot at-or-before" in rec.message for rec in caplog.records
        )

    def test_same_exchange_ts_picks_latest_by_id(
        self, session, sample_account, sample_run, base_ts
    ):
        """Two snapshots at the same exchange_ts (multi-notify simulation) →
        loader returns the row inserted second (larger id)."""
        repo = GridStateSnapshotRepository(session)
        intermediate = [
            {"side": "Buy", "price": 100.0},
            {"side": "Wait", "price": 101.0},
        ]
        final = [
            {"side": "Buy", "price": 100.0},
            {"side": "Sell", "price": 101.0},
        ]
        repo.insert(
            _make_grid_row(
                sample_run, sample_account, grid=intermediate, grid_count=2,
                exchange_ts=base_ts,
            )
        )
        repo.insert(
            _make_grid_row(
                sample_run, sample_account, grid=final, grid_count=2,
                exchange_ts=base_ts,
            )
        )
        session.flush()

        seed = load_grid_state_from_snapshots(
            session,
            run_id=sample_run.run_id,
            account_id=str(sample_account.account_id),
            strat_id="strat-A",
            at_ts=base_ts + timedelta(minutes=1),
            expected_step=0.2,
            expected_count=2,
        )
        assert seed is not None
        assert seed.grid == final

    def test_picks_latest_at_or_before(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = GridStateSnapshotRepository(session)
        repo.insert(_make_grid_row(sample_run, sample_account, exchange_ts=base_ts))
        repo.insert(
            _make_grid_row(
                sample_run, sample_account,
                grid=[
                    {"side": "Buy", "price": 200.0},
                    {"side": "Wait", "price": 201.0},
                    {"side": "Sell", "price": 202.0},
                ],
                exchange_ts=base_ts + timedelta(minutes=10),
            )
        )
        session.flush()

        seed = load_grid_state_from_snapshots(
            session,
            run_id=sample_run.run_id,
            account_id=str(sample_account.account_id),
            strat_id="strat-A",
            at_ts=base_ts + timedelta(minutes=5),
            expected_step=0.2,
            expected_count=3,
        )
        assert seed is not None
        # First row (base_ts) wins — later row (+10m) is past the at_ts upper bound.
        assert seed.grid[0]["price"] == 100.0

    def test_cross_run_isolation(
        self, session, sample_user, sample_account, sample_strategy, sample_run,
        base_ts,
    ):
        """Snapshot present for another run only → loader returns None."""
        other_run = Run(
            user_id=sample_user.user_id,
            account_id=sample_account.account_id,
            strategy_id=sample_strategy.strategy_id,
            run_type="recording",
            status="running",
            start_ts=base_ts,
        )
        session.add(other_run)
        session.flush()
        repo = GridStateSnapshotRepository(session)
        repo.insert(_make_grid_row(other_run, sample_account, exchange_ts=base_ts))
        session.flush()

        seed = load_grid_state_from_snapshots(
            session,
            run_id=sample_run.run_id,
            account_id=str(sample_account.account_id),
            strat_id="strat-A",
            at_ts=base_ts + timedelta(minutes=1),
            expected_step=0.2,
            expected_count=3,
        )
        assert seed is None

    def test_cross_strat_isolation(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = GridStateSnapshotRepository(session)
        repo.insert(
            _make_grid_row(
                sample_run, sample_account, strat_id="other-strat",
                exchange_ts=base_ts,
            )
        )
        session.flush()
        seed = load_grid_state_from_snapshots(
            session,
            run_id=sample_run.run_id,
            account_id=str(sample_account.account_id),
            strat_id="strat-A",
            at_ts=base_ts + timedelta(minutes=1),
            expected_step=0.2,
            expected_count=3,
        )
        assert seed is None

    def test_step_count_mismatch_returns_none(
        self, session, sample_account, sample_run, base_ts, caplog
    ):
        repo = GridStateSnapshotRepository(session)
        repo.insert(_make_grid_row(sample_run, sample_account, exchange_ts=base_ts))
        session.flush()
        with caplog.at_level(logging.INFO):
            seed = load_grid_state_from_snapshots(
                session,
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                strat_id="strat-A",
                at_ts=base_ts + timedelta(minutes=1),
                expected_step=0.2,
                expected_count=99,  # Mismatch.
            )
        assert seed is None
        assert any("grid_count" in rec.message for rec in caplog.records)

    def test_pre_0047_db_missing_table_returns_none(
        self, db, sample_account, sample_run, base_ts, caplog
    ):
        """0047 P1: replay against a pre-0047 recorder DB (no
        ``grid_state_snapshots`` table) must return ``None`` with INFO so
        the engine falls back to ``seed.grid_state_path``. Without this,
        SQLAlchemy raises ``no such table`` and the file fallback never
        runs — breaking the plan's "old datasets stay on file mode"
        out-of-scope clause.
        """
        # The other sample_* fixtures already used ``db`` to create all
        # tables; drop the 0047 table to simulate the pre-0047 schema.
        GridStateSnapshot.__table__.drop(db.engine)
        try:
            with db.get_session() as sess:
                with caplog.at_level(logging.INFO):
                    seed = load_grid_state_from_snapshots(
                        sess,
                        run_id=sample_run.run_id,
                        account_id=str(sample_account.account_id),
                        strat_id="strat-A",
                        at_ts=base_ts,
                        expected_step=0.2,
                        expected_count=3,
                    )
            assert seed is None
            assert any(
                "table not present" in rec.message for rec in caplog.records
            )
        finally:
            # Restore the table so other tests in the session aren't affected.
            GridStateSnapshot.__table__.create(db.engine)

    def test_step_value_mismatch_returns_none(
        self, session, sample_account, sample_run, base_ts, caplog
    ):
        """Step branch is exercised independently of the count branch:
        stored step != expected step → None + INFO. Count is left equal
        on purpose so count branch is not what catches the mismatch.
        """
        repo = GridStateSnapshotRepository(session)
        repo.insert(
            _make_grid_row(
                sample_run, sample_account,
                grid_step=0.2, grid_count=3, exchange_ts=base_ts,
            )
        )
        session.flush()
        with caplog.at_level(logging.INFO):
            seed = load_grid_state_from_snapshots(
                session,
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                strat_id="strat-A",
                at_ts=base_ts + timedelta(minutes=1),
                expected_step=0.5,  # Mismatch — distinct decimal value.
                expected_count=3,
            )
        assert seed is None
        assert any("grid_step" in rec.message for rec in caplog.records)

    def test_loader_output_round_trips_through_restore_grid(
        self, session, sample_account, sample_run, base_ts
    ):
        """The loader's payload must drop straight into
        ``Grid.restore_grid`` (live's restart API) without massaging.
        Locks the invariant: anything ``GridStateWriter`` writes must be
        consumable by the same code that consumes the legacy JSON file.
        """
        from decimal import Decimal
        from gridcore.grid import Grid

        # Use a Grid-valid layout (strict-ascending prices, Buy → Wait →
        # Sell). ``is_grid_correct`` rejects anything else.
        canonical_grid = [
            {"side": "Buy", "price": 99.0},
            {"side": "Buy", "price": 99.5},
            {"side": "Wait", "price": 100.0},
            {"side": "Sell", "price": 100.5},
            {"side": "Sell", "price": 101.0},
        ]
        repo = GridStateSnapshotRepository(session)
        repo.insert(
            _make_grid_row(
                sample_run, sample_account,
                grid=canonical_grid, grid_step=0.5, grid_count=5,
                exchange_ts=base_ts,
            )
        )
        session.flush()
        seed = load_grid_state_from_snapshots(
            session,
            run_id=sample_run.run_id,
            account_id=str(sample_account.account_id),
            strat_id="strat-A",
            at_ts=base_ts + timedelta(minutes=1),
            expected_step=0.5,
            expected_count=5,
        )
        assert seed is not None

        # Same restore_grid call live performs on cold start.
        grid_obj = Grid(
            tick_size=Decimal("0.1"),
            grid_count=seed.grid_count,
            grid_step=seed.grid_step,
        )
        assert grid_obj.restore_grid(seed.grid) is True
        assert grid_obj.grid == canonical_grid

    def test_step_binary_imprecise_match_accepted(
        self, session, sample_account, sample_run, base_ts
    ):
        """``expected_step=0.1`` (float) must accept ``Decimal('0.10000000')`` row.

        Naive ``Decimal == float`` would reject because Python's ``0.1``
        literal is ``0.1000000000000000055511...``.
        """
        repo = GridStateSnapshotRepository(session)
        repo.insert(
            _make_grid_row(
                sample_run, sample_account,
                grid_step=0.1,
                exchange_ts=base_ts,
            )
        )
        session.flush()
        seed = load_grid_state_from_snapshots(
            session,
            run_id=sample_run.run_id,
            account_id=str(sample_account.account_id),
            strat_id="strat-A",
            at_ts=base_ts + timedelta(minutes=1),
            expected_step=0.1,
            expected_count=3,
        )
        assert seed is not None


# ---------------------------------------------------------------------------
# load_position_snapshots
# ---------------------------------------------------------------------------


class TestLoadPositionSnapshots:
    """Per the recorder initial-snapshot contract, both sides must be
    present together."""

    def test_both_sides_present_seeds_both(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = PositionSnapshotRepository(session)
        repo.bulk_insert([
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                side="Buy",
                size=Decimal("1.5"),
                entry_price=Decimal("50000"),
                liq_price=Decimal("45000"),
            ),
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                side="Sell",
                size=Decimal("0.5"),
                entry_price=Decimal("51000"),
                liq_price=Decimal("55000"),
            ),
        ])

        long_seed, short_seed = load_position_snapshots(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            "BTCUSDT",
            base_ts + timedelta(seconds=1),
        )

        assert isinstance(long_seed, PositionStateSeed)
        assert long_seed.direction == "long"
        assert long_seed.size == Decimal("1.5")
        assert long_seed.entry_price == Decimal("50000")
        assert long_seed.liquidation_price == Decimal("45000")

        assert short_seed.direction == "short"
        assert short_seed.size == Decimal("0.5")
        assert short_seed.entry_price == Decimal("51000")
        assert short_seed.liquidation_price == Decimal("55000")

    def test_both_sides_absent_returns_zero_pair(
        self, session, sample_account, sample_run, base_ts
    ):
        long_seed, short_seed = load_position_snapshots(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            "BTCUSDT",
            base_ts,
        )
        assert long_seed.direction == "long"
        assert long_seed.size == Decimal("0")
        assert long_seed.entry_price == Decimal("0")
        assert long_seed.liquidation_price == Decimal("0")
        assert short_seed.direction == "short"
        assert short_seed.size == Decimal("0")

    def test_one_side_only_raises_data_quality_error(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = PositionSnapshotRepository(session)
        repo.bulk_insert([
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                side="Buy",
                size=Decimal("1.0"),
                entry_price=Decimal("50000"),
                liq_price=Decimal("45000"),
            ),
        ])

        with pytest.raises(SeedDataQualityError, match="missing Sell"):
            load_position_snapshots(
                session,
                sample_run.run_id,
                str(sample_account.account_id),
                "BTCUSDT",
                base_ts + timedelta(seconds=1),
            )

    def test_one_side_only_sell_raises_data_quality_error(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = PositionSnapshotRepository(session)
        repo.bulk_insert([
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                side="Sell",
                size=Decimal("1.0"),
                entry_price=Decimal("51000"),
                liq_price=Decimal("55000"),
            ),
        ])

        with pytest.raises(SeedDataQualityError, match="missing Buy"):
            load_position_snapshots(
                session,
                sample_run.run_id,
                str(sample_account.account_id),
                "BTCUSDT",
                base_ts + timedelta(seconds=1),
            )

    def test_null_liq_price_coerces_to_zero(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = PositionSnapshotRepository(session)
        repo.bulk_insert([
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                side="Buy",
                size=Decimal("0"),
                entry_price=Decimal("0"),
                liq_price=None,  # zero-size side from initial REST snapshot
            ),
            PositionSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                side="Sell",
                size=Decimal("0"),
                entry_price=Decimal("0"),
                liq_price=None,
            ),
        ])

        long_seed, short_seed = load_position_snapshots(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            "BTCUSDT",
            base_ts + timedelta(seconds=1),
        )
        assert long_seed.liquidation_price == Decimal("0")
        assert short_seed.liquidation_price == Decimal("0")


# ---------------------------------------------------------------------------
# load_wallet_snapshot
# ---------------------------------------------------------------------------


class TestLoadWalletSnapshot:
    def test_happy_path_returns_decimal(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = WalletSnapshotRepository(session)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts, local_ts=base_ts,
                coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
            ),
        ])

        balance = load_wallet_snapshot(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            base_ts + timedelta(seconds=1),
        )
        assert balance == Decimal("12345.67")

    def test_coin_filter_isolates_usdt_from_btc(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = WalletSnapshotRepository(session)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts, local_ts=base_ts,
                coin="USDT",
                wallet_balance=Decimal("100"),
                available_balance=Decimal("100"),
            ),
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts, local_ts=base_ts,
                coin="BTC",
                wallet_balance=Decimal("0.5"),
                available_balance=Decimal("0.5"),
            ),
        ])

        usdt = load_wallet_snapshot(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            base_ts + timedelta(seconds=1),
            coin="USDT",
        )
        assert usdt == Decimal("100")

    def test_no_rows_returns_none(
        self, session, sample_account, sample_run, base_ts
    ):
        balance = load_wallet_snapshot(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            base_ts,
        )
        assert balance is None


# ---------------------------------------------------------------------------
# load_wallet_seed_full
# ---------------------------------------------------------------------------


class TestLoadWalletSeedFull:
    def test_happy_path_returns_wallet_seed(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = WalletSnapshotRepository(session)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts,
                local_ts=base_ts,
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

        seed = load_wallet_seed_full(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            base_ts + timedelta(seconds=1),
        )

        assert isinstance(seed, WalletSeed)
        assert seed.coin_balance == Decimal("12345.67")
        assert seed.total_available_balance == Decimal("14000.25")
        assert seed.total_equity == Decimal("15000.50")
        assert seed.total_margin_balance == Decimal("14900.75")
        assert seed.account_im_rate == Decimal("0.01000000")
        assert seed.account_mm_rate == Decimal("0.00500000")

    def test_null_total_available_balance_returns_none(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = WalletSnapshotRepository(session)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts,
                local_ts=base_ts,
                coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
            ),
        ])

        seed = load_wallet_seed_full(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            base_ts + timedelta(seconds=1),
        )

        assert seed is None

    def test_zero_total_available_balance_returns_none(
        self, session, sample_account, sample_run, base_ts, caplog
    ):
        """0043 review: refuse rows where total_available_balance == 0.

        Symmetric to the total_equity guard. Defends against Bybit dropping
        the `totalAvailableBalance` key while keeping `totalEquity` — writer
        would store 0 via decimal_or_zero, loader would otherwise pass the
        row and seed `BacktestSession.current_balance = 0`, breaking
        executor margin gating / qty calculator / risk multipliers.
        """
        import logging

        repo = WalletSnapshotRepository(session)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts,
                local_ts=base_ts,
                coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
                total_available_balance=Decimal("0"),  # zero — refuse
                total_equity=Decimal("15000.50"),
            ),
        ])

        with caplog.at_level(logging.WARNING, logger="replay.snapshot_loader"):
            seed = load_wallet_seed_full(
                session,
                sample_run.run_id,
                str(sample_account.account_id),
                base_ts + timedelta(seconds=1),
            )

        assert seed is None
        warnings = [
            r for r in caplog.records
            if "total_available_balance=0" in r.message and "<= 0" in r.message
        ]
        assert len(warnings) == 1

    def test_zero_total_equity_returns_none(
        self, session, sample_account, sample_run, base_ts, caplog
    ):
        """0043 review: refuse rows where total_equity == 0.

        Defends against the WS writer's `decimal_or_zero` fallback: a future
        Bybit payload-shape change that drops account-level keys would
        currently land as `0` in the DB instead of `NULL`, bypassing the
        `is None` guard. Also covers the genuinely-empty-account case.
        """
        import logging

        repo = WalletSnapshotRepository(session)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts,
                local_ts=base_ts,
                coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
                total_available_balance=Decimal("14000.25"),
                total_equity=Decimal("0"),  # simulated writer-zero fallback
            ),
        ])

        with caplog.at_level(logging.WARNING, logger="replay.snapshot_loader"):
            seed = load_wallet_seed_full(
                session,
                sample_run.run_id,
                str(sample_account.account_id),
                base_ts + timedelta(seconds=1),
            )

        assert seed is None
        warnings = [
            r for r in caplog.records
            if "total_equity=0" in r.message and "<= 0" in r.message
        ]
        assert len(warnings) == 1

    def test_null_total_equity_returns_none(
        self, session, sample_account, sample_run, base_ts, caplog
    ):
        """0043 review fix: refuse to seed when total_equity is NULL even if
        total_available_balance is populated. Zero is not a safe default for
        the pair-liq pool input — it would silently corrupt liq for the
        entire replay run.
        """
        import logging

        repo = WalletSnapshotRepository(session)
        repo.bulk_insert([
            WalletSnapshot(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                exchange_ts=base_ts,
                local_ts=base_ts,
                coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
                total_available_balance=Decimal("14000.25"),
                # total_equity intentionally NULL — partial-migration shape.
            ),
        ])

        with caplog.at_level(logging.WARNING, logger="replay.snapshot_loader"):
            seed = load_wallet_seed_full(
                session,
                sample_run.run_id,
                str(sample_account.account_id),
                base_ts + timedelta(seconds=1),
            )

        assert seed is None
        warnings = [
            r for r in caplog.records
            if "NULL total_equity" in r.message
        ]
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# load_active_orders
# ---------------------------------------------------------------------------


class TestLoadActiveOrders:
    def test_empty_list_for_clean_account(
        self, session, sample_account, sample_run, base_ts
    ):
        seeds = load_active_orders(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            "BTCUSDT",
            base_ts,
        )
        assert seeds == []

    def test_happy_path_with_order_link_id_and_fallback(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = OrderRepository(session)
        repo.bulk_insert([
            # Has order_link_id → client_id == order_link_id (prefix).
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="ORD-LX-1",
                order_link_id="LX1",
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                status="New", side="Buy",
                price=Decimal("99.8"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0.001"),
                reduce_only=False,
            ),
            # No order_link_id → client_id falls back to order_id.
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="ORD-NOLINK-2",
                order_link_id=None,
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                status="PartiallyFilled", side="Sell",
                price=Decimal("100.2"), qty=Decimal("0.002"),
                leaves_qty=Decimal("0.001"),
                reduce_only=False,
            ),
        ])

        seeds = load_active_orders(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            "BTCUSDT",
            base_ts + timedelta(seconds=10),
        )

        assert len(seeds) == 2
        by_oid = {s.exchange_order_id: s for s in seeds}

        s1 = by_oid["ORD-LX-1"]
        assert isinstance(s1, ActiveOrderSeed)
        assert s1.client_id == "LX1"  # order_link_id wins
        assert s1.side == "Buy"
        assert s1.direction == "long"
        assert s1.remaining_qty == Decimal("0.001")
        assert s1.reduce_only is False

        s2 = by_oid["ORD-NOLINK-2"]
        assert s2.client_id == "ORD-NOLINK-2"  # fallback to order_id
        assert s2.side == "Sell"
        assert s2.direction == "short"
        assert s2.remaining_qty == Decimal("0.001")

    def test_strips_orderlinkid_suffix_post_hotfix(
        self, session, sample_account, sample_run, base_ts
    ):
        """Post-2026-05-08, recorder-DB Order.order_link_id carries a
        `-{millis}` suffix. The seed must key by the deterministic prefix
        so replay's re-placed intents (which produce the unsuffixed
        client_order_id from PlaceLimitIntent.create) match the seeded
        active order on cancel-on-mismatch and tracking lookups."""
        repo = OrderRepository(session)
        repo.bulk_insert([
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="ORD-SFX",
                order_link_id="cffab542de0a6295-1715170800000",
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                status="New", side="Buy",
                price=Decimal("99.8"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0.001"),
                reduce_only=False,
            ),
        ])

        seeds = load_active_orders(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            "BTCUSDT",
            base_ts + timedelta(seconds=10),
        )

        assert len(seeds) == 1
        assert seeds[0].client_id == "cffab542de0a6295"
        assert seeds[0].exchange_order_id == "ORD-SFX"

    def test_reduce_only_null_raises_schema_error(
        self, session, sample_account, sample_run, base_ts
    ):
        repo = OrderRepository(session)
        repo.bulk_insert([
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id="ORD-NULL-RO",
                order_link_id="LX-NULL",
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                status="New", side="Buy",
                price=Decimal("99.8"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0.001"),
                reduce_only=None,  # pre-Phase-1 row
            ),
        ])

        with pytest.raises(SeedSchemaError, match="reduce_only=NULL"):
            load_active_orders(
                session,
                sample_run.run_id,
                str(sample_account.account_id),
                "BTCUSDT",
                base_ts + timedelta(seconds=10),
            )

    @pytest.mark.parametrize(
        "side, reduce_only, expected_direction",
        [
            ("Buy", False, "long"),
            ("Sell", False, "short"),
            ("Buy", True, "short"),
            ("Sell", True, "long"),
        ],
    )
    def test_direction_derivation_all_combos(
        self,
        session,
        sample_account,
        sample_run,
        base_ts,
        side,
        reduce_only,
        expected_direction,
    ):
        repo = OrderRepository(session)
        repo.bulk_insert([
            Order(
                run_id=sample_run.run_id,
                account_id=str(sample_account.account_id),
                order_id=f"ORD-{side}-{reduce_only}",
                order_link_id=f"LX-{side}-{reduce_only}",
                symbol="BTCUSDT",
                exchange_ts=base_ts, local_ts=base_ts,
                status="New", side=side,
                price=Decimal("100"), qty=Decimal("0.001"),
                leaves_qty=Decimal("0.001"),
                reduce_only=reduce_only,
            ),
        ])

        seeds = load_active_orders(
            session,
            sample_run.run_id,
            str(sample_account.account_id),
            "BTCUSDT",
            base_ts + timedelta(seconds=10),
        )
        assert len(seeds) == 1
        assert seeds[0].direction == expected_direction
        assert seeds[0].side == side
        assert seeds[0].reduce_only is reduce_only
