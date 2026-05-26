"""Replay engine — replays recorded mainnet data through GridEngine.

Orchestrates:
- HistoricalDataProvider (reads recorded TickerSnapshots)
- BacktestRunner (GridEngine + simulated order book)
- LiveTradeLoader (loads recorded executions as ground truth)
- TradeMatcher + calculate_metrics (comparison)
- ComparatorReporter (output)
"""

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from grid_db import (
    DatabaseFactory,
    PositionSnapshot,
    PositionSnapshotRepository,
    Run,
    RunRepository,
    WalletSnapshot,
    redact_db_url,
)

from gridcore import DirectionType, create_qty_calculator
from gridcore.persistence import GridStateStore

from backtest.config import BacktestStrategyConfig, WindDownMode
from backtest.data_provider import HistoricalDataProvider, InMemoryDataProvider
from backtest.engine import FundingSimulator
from backtest.executor import BacktestExecutor
from backtest.fill_simulator import FillMode, TradeThroughFillSimulator
from backtest.instrument_info import InstrumentInfoProvider
from backtest.order_manager import BacktestOrderManager
from backtest.position_tracker import BacktestPositionTracker
from backtest.runner import BacktestRunner
from backtest.session import BacktestSession, BacktestTrade

from comparator import (
    BacktestTradeLoader,
    LiveTradeLoader,
    TradeMatcher,
    calculate_metrics,
    MatchResult,
    ValidationMetrics,
)
from comparator.position_loader import (
    load_position_snapshots as load_position_snapshot_rows,
)
from comparator.position_metrics import PositionComparator

from replay.config import ReplayConfig, SeedConfig
from replay.snapshot_loader import (
    ActiveOrderSeed,
    GridStateSeed,
    PositionStateSeed,
    WalletSeed,
    load_active_orders,
    load_grid_state,
    load_grid_state_from_snapshots,
    load_position_snapshots,
    load_wallet_seed_full,
)


logger = logging.getLogger(__name__)


# Minimum gap between the latest required initial-snapshot row and seed.at_ts.
# Used by the Phase 4 pre-check (which lives in the engine — see Phase 3 plan).
# 5 seconds matches the example in docs/features/0029_PLAN.md and gives enough
# slack for clock skew between recorder and live without masking the
# "initial REST snapshot has not landed yet" misconfig.
_SEED_PRE_CHECK_MARGIN = timedelta(seconds=5)


class _BatchPositionSnapshotWriter:
    """Synchronous writer for backtest position snapshots (0034).

    Buffers snapshots emitted by ``BacktestRunner._emit_position_snapshot``
    and flushes in bulk at end-of-run. Synchronous because replay is
    single-threaded — no auto-flush loop, no asyncio coupling.

    Stamps ``run_id``, ``account_id``, and ``source='backtest'`` on every
    row. The runner only fills the symbol/side/size/entry/etc.
    """

    def __init__(
        self,
        db: DatabaseFactory,
        run_id: str,
        account_id: str,
        source: str = "backtest",
        flush_batch_size: int = 500,
    ):
        self._db = db
        self._run_id = run_id
        self._account_id = account_id
        self._source = source
        self._flush_batch_size = flush_batch_size
        self._buffer: list[PositionSnapshot] = []
        self._total_written = 0

    def write(self, snapshot: PositionSnapshot) -> None:
        """Stamp run-context fields and buffer for flush."""
        snapshot.run_id = self._run_id
        snapshot.account_id = self._account_id
        snapshot.source = self._source
        self._buffer.append(snapshot)
        if len(self._buffer) >= self._flush_batch_size:
            self.flush()

    def flush(self) -> int:
        """Bulk-insert any buffered snapshots; returns rowcount."""
        if not self._buffer:
            return 0
        with self._db.get_session() as session:
            repo = PositionSnapshotRepository(session)
            inserted = repo.bulk_insert(self._buffer)
        self._total_written += inserted
        self._buffer.clear()
        return inserted

    @property
    def total_written(self) -> int:
        return self._total_written


@dataclass
class ReplayResult:
    """Result of a replay run."""

    session: BacktestSession
    metrics: ValidationMetrics
    match_result: MatchResult
    run_id: str
    symbol: str
    start_ts: datetime
    end_ts: datetime
    fill_mode: FillMode
    # 0034: paired live/backtest position snapshots for the CSV export.
    # Empty list when no pairs found OR comparator not run.
    position_pairs: list = field(default_factory=list)


class ReplayEngine:
    """Replays recorded data through GridEngine and compares against real executions.

    Example:
        config = load_config("replay.yaml")
        db = DatabaseFactory(DatabaseSettings(database_url=config.database_url))
        engine = ReplayEngine(config, db)
        result = engine.run()
        print(result.session.get_summary())
    """

    def __init__(
        self,
        config: ReplayConfig,
        db: DatabaseFactory,
    ):
        self._config = config
        self._db = db
        self._instrument_provider = InstrumentInfoProvider()

    def run(
        self,
        data_provider: Optional[InMemoryDataProvider] = None,
    ) -> ReplayResult:
        """Run replay and comparison.

        Args:
            data_provider: Optional in-memory data provider (for testing).

        Returns:
            ReplayResult with session, metrics, and match result.
        """
        config = self._config

        # 1. Resolve run_id, account_id and time range
        run_id, account_id, start_ts, end_ts = self._resolve_run(config)
        fill_mode = FillMode(config.fill_simulator.mode)

        logger.info(
            f"Replay: symbol={config.symbol}, run_id={run_id}, "
            f"range={start_ts} to {end_ts}, fill_mode={fill_mode.value}"
        )

        # 2. Build backtest components
        strat_id = f"replay_{config.symbol.lower()}"
        strategy_config = BacktestStrategyConfig(
            strat_id=strat_id,
            symbol=config.symbol,
            tick_size=config.strategy.tick_size,
            grid_count=config.strategy.grid_count,
            grid_step=config.strategy.grid_step,
            amount=config.strategy.amount,
            max_margin=config.strategy.max_margin,
            early_imbalance_multiplier=config.strategy.early_imbalance_multiplier,
            commission_rate=config.strategy.commission_rate,
            enable_risk_multipliers=config.strategy.enable_risk_multipliers,
        )

        # 2a. Load seed material (positions/wallet/orders/grid) when enabled.
        # When disabled this returns the all-None tuple and the runner
        # constructs from scratch — preserving pre-0029 behaviour exactly.
        (
            wallet_seed,
            long_seed,
            short_seed,
            grid_seed,
            order_seeds,
        ) = self._load_seed(config, run_id)

        initial_balance = (
            wallet_seed.total_available_balance
            if wallet_seed is not None
            else config.initial_balance
        )
        # 0043: total_equity is the pool input for the hedge-aware pair
        # liquidation formula. Falls back to initial_balance when no seed
        # is available (non-replay / pre-0043 paths).
        initial_equity = (
            wallet_seed.total_equity
            if wallet_seed is not None
            else initial_balance
        )
        session = BacktestSession(
            initial_balance=initial_balance,
            initial_equity=initial_equity,
        )
        runner = self._init_runner(
            strategy_config,
            session,
            long_seed=long_seed,
            short_seed=short_seed,
            grid_seed=grid_seed,
            order_seeds=order_seeds,
            fill_mode=fill_mode,
            run_id=run_id,
            account_id=account_id,
        )

        if config.seed.enabled:
            logger.info(
                "Seeded run_id=%s: long.size=%s, short.size=%s, "
                "anchor_or_grid_levels=%s, coin_balance=%s, "
                "total_available_balance=%s, total_equity=%s, active_orders=%s",
                run_id,
                long_seed.size if long_seed is not None else Decimal("0"),
                short_seed.size if short_seed is not None else Decimal("0"),
                len(grid_seed.grid) if grid_seed is not None else 0,
                wallet_seed.coin_balance if wallet_seed is not None else None,
                initial_balance,
                initial_equity,
                len(order_seeds) if order_seeds is not None else 0,
            )

        # 3. Create data provider
        if data_provider is not None:
            provider = data_provider
        else:
            provider = HistoricalDataProvider(
                db=self._db,
                symbol=config.symbol,
                start_ts=start_ts,
                end_ts=end_ts,
            )

        range_info = provider.get_data_range_info()
        logger.info(
            f"Data range: {range_info.start_ts} to {range_info.end_ts} "
            f"({range_info.total_records} records)"
        )

        # 4. Funding simulator
        funding_simulator = None
        if config.enable_funding:
            funding_simulator = FundingSimulator(rate=config.funding_rate)

        # 5. Replay loop (two-phase tick processing)
        last_price = Decimal("0")
        last_timestamp = None
        tick_count = 0

        for tick in provider:
            last_price = tick.last_price
            last_timestamp = tick.exchange_ts

            # Funding
            if funding_simulator and funding_simulator.should_apply_funding(tick.exchange_ts):
                funding = runner.apply_funding(funding_simulator.rate, tick.last_price)
                if funding != 0:
                    logger.debug(f"Funding payment: {funding:.4f}")
                funding_simulator.mark_funding_applied(tick.exchange_ts)

            # Phase 1: process fills
            runner.process_fills(tick)

            # Update equity
            unrealized = (
                runner.long_tracker.calculate_unrealized_pnl(tick.last_price)
                + runner.short_tracker.calculate_unrealized_pnl(tick.last_price)
            )
            session.update_equity(tick.exchange_ts, unrealized)

            # Phase 2: execute tick intents
            runner.execute_tick(tick)

            tick_count += 1
            if tick_count % 10000 == 0:
                logger.info(f"Processed {tick_count} ticks...")

        logger.info(f"Replay complete: {tick_count} ticks processed")

        # 6. Wind down
        if config.wind_down_mode == WindDownMode.CLOSE_ALL and last_price > 0:
            self._wind_down(runner, session, last_price, last_timestamp)

        # 6b. 0034: flush buffered backtest position snapshots so the
        # comparator (and the pair_and_compare call below) can read them.
        position_writer = getattr(runner, "_position_writer", None)
        if position_writer is not None:
            position_writer.flush()
            logger.info(
                "Position telemetry: wrote %d backtest snapshots to DB",
                position_writer.total_written,
            )

        # 7. Finalize session
        final_unrealized = (
            runner.long_tracker.calculate_unrealized_pnl(last_price)
            + runner.short_tracker.calculate_unrealized_pnl(last_price)
        ) if last_price > 0 else Decimal("0")
        session.finalize(final_unrealized)

        # 8. Load ground truth (recorded executions)
        with self._db.get_session() as db_session:
            live_loader = LiveTradeLoader(db_session)
            recorded_trades = live_loader.load(
                run_id=run_id,
                start_ts=start_ts,
                end_ts=end_ts,
                symbol=config.symbol,
            )

        logger.info(f"Loaded {len(recorded_trades)} recorded trades (ground truth)")

        # 9. Convert simulated trades
        bt_loader = BacktestTradeLoader()
        simulated_trades = bt_loader.load_from_session(session.trades)

        logger.info(f"Produced {len(simulated_trades)} simulated trades")

        # 10. Match and compute metrics
        matcher = TradeMatcher()
        match_result = matcher.match(recorded_trades, simulated_trades)

        metrics = calculate_metrics(
            match_result,
            price_tolerance=config.price_tolerance,
            qty_tolerance=config.qty_tolerance,
        )

        # 11. 0034: pair live/backtest position snapshots and fold telemetry
        # parity metrics into the same ValidationMetrics object. 0038:
        # pairing and telemetry fold must run INSIDE the session so attribute
        # access (`.side`, `.exchange_ts`, telemetry columns) happens while
        # rows are still attached; `expunge_all()` then detaches the rows so
        # `commit()`'s expiration sweep on __exit__ leaves them readable for
        # downstream consumers (ComparatorReporter CSV export).
        position_pairs: list = []
        with self._db.get_session() as db_session:
            live_snaps = load_position_snapshot_rows(
                db_session,
                run_id=run_id,
                symbol=config.symbol,
                source="live",
                start_ts=start_ts,
                end_ts=end_ts,
            )
            bt_snaps = load_position_snapshot_rows(
                db_session,
                run_id=run_id,
                symbol=config.symbol,
                source="backtest",
                start_ts=start_ts,
                end_ts=end_ts,
            )
            if live_snaps and bt_snaps:
                pc = PositionComparator()
                position_pairs = pc.pair_and_compare(live_snaps, bt_snaps)
                pc.fold_metrics_into(metrics, position_pairs)
                logger.info(
                    "Position telemetry: %d pairs compared, %d unmatched bt, "
                    "%d state diverged, %d missing telemetry",
                    metrics.position_pairs_compared,
                    metrics.position_pairs_unmatched_bt,
                    metrics.position_pairs_state_diverged,
                    metrics.position_pairs_missing_telemetry,
                )
            else:
                logger.info(
                    "Position telemetry: skipped (live_snaps=%d, bt_snaps=%d)",
                    len(live_snaps), len(bt_snaps),
                )
            db_session.expunge_all()

        return ReplayResult(
            session=session,
            metrics=metrics,
            match_result=match_result,
            run_id=run_id,
            symbol=config.symbol,
            start_ts=start_ts,
            end_ts=end_ts,
            fill_mode=fill_mode,
            position_pairs=position_pairs,
        )

    def _resolve_run(self, config: ReplayConfig):
        """Resolve run_id, account_id and time range from config or database.

        Returns:
            Tuple of (run_id, account_id, start_ts, end_ts). ``account_id``
            is the ``Run.account_id`` column — used by the 0034 position
            telemetry writer. May be ``None`` on legacy pre-0029 runs.
        """
        run_id = config.run_id

        if run_id is None:
            # Auto-discover latest recording run
            with self._db.get_session() as session:
                repo = RunRepository(session)
                run = repo.get_latest_by_type("recording")
                if run is None:
                    safe_url = redact_db_url(self._db.settings.get_database_url())
                    raise ValueError(
                        f"No recording runs found in database ({safe_url}). "
                        "Ensure recorder has completed at least one run."
                    )
                # Extract values while still in session scope
                run_id = run.run_id
                run_account_id = run.account_id
                run_start = run.start_ts
                run_end = run.end_ts

            logger.info(f"Auto-discovered recording run: {run_id}")

            start_ts = config.start_ts or run_start
            end_ts = config.end_ts or run_end
            account_id = run_account_id
        else:
            start_ts = config.start_ts
            end_ts = config.end_ts
            # 0034: always load the Run row so account_id is available, even
            # when both timestamps were supplied in the config. One PK lookup.
            with self._db.get_session() as session:
                run = session.get(Run, run_id)
                if run is None:
                    raise ValueError(f"Run '{run_id}' not found in database")
                if start_ts is None:
                    start_ts = run.start_ts
                if end_ts is None:
                    end_ts = run.end_ts
                account_id = run.account_id

        # Handle active runs (end_ts still None → use utcnow)
        if end_ts is None:
            end_ts = datetime.now(timezone.utc)
            logger.info(f"Run still active, using now as end_ts: {end_ts}")

        if start_ts is None:
            raise ValueError("start_ts could not be resolved")

        # Compare without tz info — SQLite may strip timezone from stored timestamps
        if start_ts.replace(tzinfo=None) >= end_ts.replace(tzinfo=None):
            raise ValueError(
                f"Invalid time range: start_ts ({start_ts}) must be before end_ts ({end_ts})"
            )

        return run_id, account_id, start_ts, end_ts

    def _init_runner(
        self,
        strategy_config: BacktestStrategyConfig,
        session: BacktestSession,
        long_seed: Optional[PositionStateSeed] = None,
        short_seed: Optional[PositionStateSeed] = None,
        grid_seed: Optional[GridStateSeed] = None,
        order_seeds: Optional[list[ActiveOrderSeed]] = None,
        fill_mode: FillMode = FillMode.STRICT_CROSS,
        run_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> BacktestRunner:
        """Initialize a BacktestRunner for the replay.

        Args:
            strategy_config: Backtest strategy config (already projected
                from ``ReplayConfig``).
            session: Pre-constructed ``BacktestSession`` — when 0042 wallet
                seed data is present, its ``initial_balance`` is account-level
                UTA ``total_available_balance``.
            long_seed: Optional long-direction position seed; when present
                AND non-zero, ``BacktestPositionTracker.seed_state`` is
                called before the runner gets the tracker. Skipping the
                ``seed_state`` call on a zero seed avoids polluting the
                tracker with a no-op write — both branches yield the same
                tracker state, but the unconditional path is cheaper to
                reason about, so we still call it whenever a seed exists.
            short_seed: Same for short direction.
            grid_seed: Optional grid-state seed; ``grid_seed.grid`` (the
                full level list) is forwarded to ``BacktestRunner`` as
                ``restored_grid``, which routes through
                ``GridEngine.__init__(restored_grid=...)``.
            order_seeds: Optional list of pre-existing live orders; passed
                to ``BacktestRunner`` as ``seeded_active_orders``, which
                pre-loads the ``BacktestOrderManager`` so they participate
                in fill checks on the first tick.
            fill_mode: Fill simulator mode for the replay runner.
        """
        instrument_info = self._instrument_provider.get(strategy_config.symbol)
        logger.info(
            f"Instrument {strategy_config.symbol}: "
            f"qty_step={instrument_info.qty_step}, tick_size={instrument_info.tick_size}"
        )

        fill_simulator = TradeThroughFillSimulator(mode=fill_mode)
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=strategy_config.commission_rate,
        )

        qty_calculator = self._create_qty_calculator(strategy_config, instrument_info)
        executor = BacktestExecutor(
            order_manager=order_manager,
            qty_calculator=qty_calculator,
        )

        long_tracker = BacktestPositionTracker(
            direction=DirectionType.LONG,
            commission_rate=strategy_config.commission_rate,
        )
        short_tracker = BacktestPositionTracker(
            direction=DirectionType.SHORT,
            commission_rate=strategy_config.commission_rate,
        )

        # Seed trackers BEFORE handing them to BacktestRunner so the
        # runner's _copy_seeded_state_to_positions sees the seeded values
        # when constructing the gridcore.Position pair (risk path).
        if long_seed is not None:
            long_tracker.seed_state(long_seed)
        if short_seed is not None:
            short_tracker.seed_state(short_seed)

        runner = BacktestRunner(
            strategy_config=strategy_config,
            executor=executor,
            session=session,
            long_tracker=long_tracker,
            short_tracker=short_tracker,
            instrument_info=instrument_info,
            restored_grid=grid_seed.grid if grid_seed is not None else None,
            seeded_active_orders=order_seeds,
        )

        # 0034: wire the position telemetry writer. Replay always emits to
        # the recorder DB so the comparator can read both live and backtest
        # rows from the same table (filtered by `source`).
        if run_id is not None:
            if account_id is None:
                # Pre-0029 legacy data: Run.account_id is NULL. Don't
                # silently fall back — surface the issue so the operator
                # re-records with the current writer. A broken pre-0029
                # run would otherwise look identical to a healthy empty
                # data run (comparator would just see zero backtest rows).
                raise ValueError(
                    f"Run {run_id} has no account_id; cannot emit backtest "
                    "position snapshots. Re-record after the 0029+0034 "
                    "migrations to populate Run.account_id."
                )
            position_writer = _BatchPositionSnapshotWriter(
                db=self._db,
                run_id=run_id,
                account_id=account_id,
                source="backtest",
            )
            runner.position_snapshot_callback = position_writer.write
            # Stash on runner so the engine can call .flush() at end-of-run.
            runner._position_writer = position_writer  # type: ignore[attr-defined]

        return runner

    def _load_seed(
        self,
        config: ReplayConfig,
        run_id: str,
    ) -> tuple[
        Optional[WalletSeed],
        Optional[PositionStateSeed],
        Optional[PositionStateSeed],
        Optional[GridStateSeed],
        Optional[list[ActiveOrderSeed]],
    ]:
        """Load all four seed dimensions for a replay run.

        Returns ``(None, None, None, None, None)`` when ``seed.enabled``
        is False — caller falls back to blank-start construction.

        When enabled:

        1. Runs the Phase 4 pre-check inline: both ``wallet_snapshots``
           and ``position_snapshots`` must have at least one row for the
           ``run_id`` AND the latest of those two ``MIN(exchange_ts)``
           values must be ``<= seed.at_ts - 5s``. ``orders`` is excluded
           from the pre-check because a clean grid legitimately has no
           open orders at recorder start.
        2. Loads grid state from the shared ``GridStateStore`` JSON file
           (returns ``None`` on no-entry / legacy / step-or-count
           mismatch — replay then falls back to fresh-build).
        3. Loads position pair, full 0042 wallet seed, and active orders
           inside a single DB session.

        Args:
            config: Full replay config (read for ``seed`` and ``symbol``).
            run_id: Resolved recorder run identifier.
        """
        seed: SeedConfig = config.seed
        if not seed.enabled:
            return (None, None, None, None, None)

        # Defensive: validators run at config load, but a programmatic
        # constructor that bypasses model validation could leave these
        # None. Guard so the loaders below don't get None where they
        # expect strings/datetimes.
        if seed.at_ts is None or seed.account_id is None or seed.strat_id is None:
            raise ValueError(
                "seed.enabled=True but at_ts/account_id/strat_id are unset"
            )

        # 0047: try the DB grid-state snapshot first, fall back to the file
        # path, then to a blank build. DB lookup needs a session, so open it
        # FIRST and run all loaders inside.
        with self._db.get_session() as db_session:
            self._seed_pre_check(db_session, run_id, seed.at_ts)

            grid_seed = load_grid_state_from_snapshots(
                db_session,
                seed.account_id,
                seed.strat_id,
                config.symbol,
                seed.at_ts,
                expected_step=config.strategy.grid_step,
                expected_count=config.strategy.grid_count,
            )
            grid_source = "db"
            if grid_seed is None and seed.grid_state_path is not None:
                grid_seed = load_grid_state(
                    GridStateStore(file_path=seed.grid_state_path),
                    seed.strat_id,
                    expected_step=config.strategy.grid_step,
                    expected_count=config.strategy.grid_count,
                )
                grid_source = "file" if grid_seed is not None else "fresh"
            elif grid_seed is None:
                grid_source = "fresh"
            logger.info(
                "%s: grid seed source=%s", seed.strat_id, grid_source,
            )

            long_seed, short_seed = load_position_snapshots(
                db_session,
                run_id,
                seed.account_id,
                config.symbol,
                seed.at_ts,
            )
            wallet_seed = load_wallet_seed_full(
                db_session,
                run_id,
                seed.account_id,
                seed.at_ts,
                coin=seed.wallet_coin,
            )
            order_seeds = load_active_orders(
                db_session,
                run_id,
                seed.account_id,
                config.symbol,
                seed.at_ts,
            )

        return wallet_seed, long_seed, short_seed, grid_seed, order_seeds

    @staticmethod
    def _seed_pre_check(
        db_session,
        run_id: str,
        at_ts: datetime,
    ) -> None:
        """Phase 4 pre-check (lives in engine per Phase 3 plan).

        Confirms the recorder's initial REST snapshot landed for both
        wallet and position dimensions before ``at_ts``. ``orders`` is
        intentionally excluded — a clean account legitimately has zero
        open orders at recorder start, so a strict ``MIN`` requirement
        would falsely reject a valid happy path.

        Raises ``ValueError`` (not ``SeedError``) so the failure surfaces
        as a config / setup error to the operator, distinct from data-
        quality errors raised by the loaders.
        """
        wallet_min = (
            db_session.query(func.min(WalletSnapshot.exchange_ts))
            .filter(WalletSnapshot.run_id == run_id)
            .scalar()
        )
        position_min = (
            db_session.query(func.min(PositionSnapshot.exchange_ts))
            .filter(PositionSnapshot.run_id == run_id)
            .scalar()
        )

        missing = []
        if wallet_min is None:
            missing.append("wallet_snapshots")
        if position_min is None:
            missing.append("position_snapshots")
        if missing:
            raise ValueError(
                f"Seed pre-check failed for run_id={run_id}: no rows in "
                f"{', '.join(missing)}. Recorder must write an initial REST "
                "snapshot on private-stream connect; this run cannot be seeded."
            )

        # Compare ts values without tzinfo — SQLite may strip timezone on read,
        # so wallet_min / position_min may be naive while at_ts may be aware
        # (or vice versa). Strip uniformly before comparing.
        def _strip_tz(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

        latest_min = max(wallet_min, position_min)
        required_at_ts = _strip_tz(latest_min) + _SEED_PRE_CHECK_MARGIN

        if _strip_tz(at_ts) < required_at_ts:
            raise ValueError(
                f"Seed pre-check failed for run_id={run_id}: seed.at_ts "
                f"({at_ts}) must be at least {_SEED_PRE_CHECK_MARGIN.total_seconds():.0f}s "
                f"after the latest initial-snapshot row "
                f"(wallet_min={wallet_min}, position_min={position_min}, "
                f"latest_min={latest_min}). Initial REST snapshot has not "
                "landed yet — seeding would silently fall back to defaults."
            )

    def _create_qty_calculator(self, config, instrument_info):
        """Create qty calculator from amount pattern.

        Delegates to gridcore.create_qty_calculator for shared logic.
        """
        return create_qty_calculator(config.amount, instrument_info)

    def _wind_down(
        self,
        runner: BacktestRunner,
        session: BacktestSession,
        last_price: Decimal,
        last_timestamp: Optional[datetime],
    ) -> None:
        """Force close open positions at last price."""
        for direction, tracker, close_side in [
            (DirectionType.LONG, runner.long_tracker, "Sell"),
            (DirectionType.SHORT, runner.short_tracker, "Buy"),
        ]:
            if not tracker.has_position:
                continue

            size = tracker.state.size
            logger.info(
                f"{runner.strat_id}: Force closing {direction} position: "
                f"size={size} @ {last_price}"
            )

            realized_pnl = tracker.process_fill(
                side=close_side,
                qty=size,
                price=last_price,
            )
            tracker.calculate_unrealized_pnl(last_price)

            trade = BacktestTrade(
                trade_id=f"close_{uuid.uuid4().hex[:8]}",
                symbol=runner.symbol,
                side=close_side,
                price=last_price,
                qty=size,
                direction=direction,
                timestamp=last_timestamp,
                order_id=f"wind_down_{direction}",
                client_order_id=f"wind_down_{direction}",
                realized_pnl=realized_pnl,
                commission=size * last_price * tracker.commission_rate,
                strat_id=runner.strat_id,
            )
            session.record_trade(trade)

            # 0034: wind_down bypasses runner._process_fill, so emit the
            # snapshot explicitly. Without this, backtest cum_realised_pnl
            # drifts behind live by the close-out PnL.
            # Use the runner's cached ticker mark (same semantics as
            # _process_fill's emission) so the mark_price column matches
            # the recorder. Fall back to last_price only when no ticker
            # mark was seen during the run.
            if runner.position_snapshot_callback is not None and last_timestamp is not None:
                mark = (
                    runner._last_mark_price
                    if runner._last_mark_price is not None
                    else last_price
                )
                snap = runner._emit_position_snapshot(
                    direction, last_timestamp, mark,
                )
                runner.position_snapshot_callback(snap)
