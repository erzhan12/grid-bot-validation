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
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Optional

from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from grid_db import (
    DatabaseFactory,
    PositionSnapshot,
    PositionSnapshotRepository,
    PrivateExecutionRepository,
    Run,
    RunRepository,
    TickerSnapshot,
    WalletSnapshot,
    redact_db_url,
)

from gridcore import DirectionType, create_qty_calculator
from gridcore.persistence import GridStateStore

from backtest.config import BacktestStrategyConfig, WindDownMode
from backtest.data_provider import HistoricalDataProvider, InMemoryDataProvider
from backtest.engine import FundingSimulator
from backtest.executor import BacktestExecutor
from backtest.fill_simulator import (
    EventFollower,
    FillMode,
    RecordedExecution,
    TradeThroughFillSimulator,
)
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
    SeedDataQualityError,
    WalletSeed,
    _strip_tz,
    load_active_orders,
    load_collateral_seed,
    load_grid_state,
    load_grid_state_from_active_snapshots,
    load_grid_state_from_snapshots,
    load_position_snapshots,
    load_wallet_seed_full,
)


logger = logging.getLogger(__name__)


class CollateralMarkFeed:
    """Per-coin collateral mark stream over the replay window (feature 0065).

    The traded-symbol ``HistoricalDataProvider`` only carries the traded
    symbol's ticks, so non-USDT collateral coins need a separate mark source.
    This feed streams each coin's ``(exchange_ts, mark_price)`` from
    ``ticker_snapshots`` (same cursor-pagination pattern as
    ``HistoricalDataProvider._iterate_tickers``) and exposes carry-forward,
    at-or-before lookup via :meth:`mark_at`.

    Memory: one batch per coin is held at a time — marks are NOT materialised
    eagerly, so a multi-week window of a high-volume collateral symbol stays
    bounded. :meth:`mark_at` assumes the requested ``ts`` is non-decreasing
    across calls (the replay tick loop is ordered by ``exchange_ts``).
    """

    def __init__(
        self,
        db: DatabaseFactory,
        symbol_for: dict[str, str],
        start_ts: datetime,
        end_ts: datetime,
        seed_at_ts: Optional[datetime] = None,
        batch_size: int = 1000,
    ):
        self._db = db
        self._symbol_for = dict(symbol_for)
        self._start_ts = start_ts
        self._end_ts = end_ts
        # Lower floor for the carry-forward anchor: the seed moment. Defaults to
        # start_ts (no pre-window carry) when not given.
        self._seed_at_ts = seed_at_ts if seed_at_ts is not None else start_ts
        self._batch_size = batch_size
        # Per-coin streaming state.
        self._iters: dict[str, "object"] = {}
        self._pending: dict[str, Optional[tuple[datetime, Decimal]]] = {}
        self._current: dict[str, Optional[Decimal]] = {}
        self._last_ts: dict[str, datetime] = {}
        # Anchor each coin's carry-forward to the latest mark in the half-open
        # seed window [seed_at_ts, start_ts], so the first replay tick uses a
        # correct carry-forward mark when seed.at_ts < start_ts or the symbol
        # has no row exactly at start_ts (sparse stream). Floored at seed_at_ts
        # so a mark from BEFORE the seed anchor is NOT applied (that would be
        # false backward drift relative to the at_ts seed mark). No mark in the
        # window → None, and the session keeps the seed mark until a genuine
        # in-window mark arrives. The forward stream below covers rows >= start_ts.
        with self._db.get_session() as session:
            anchors = {
                coin: self._anchor_mark(session, symbol)
                for coin, symbol in self._symbol_for.items()
            }
        for coin, symbol in self._symbol_for.items():
            it = self._iter_marks(symbol)
            self._iters[coin] = it
            self._pending[coin] = next(it, None)
            self._current[coin] = anchors[coin]

    def _anchor_mark(self, session, symbol: str) -> Optional[Decimal]:
        """Latest ``mark_price`` in ``[seed_at_ts, start_ts]`` (the seed window),
        or ``None`` when the symbol has no row in that range. Floored at
        ``seed_at_ts`` so a pre-seed mark is never used as the t0 carry-forward."""
        row = (
            session.query(TickerSnapshot.mark_price)
            .filter(TickerSnapshot.symbol == symbol)
            .filter(TickerSnapshot.exchange_ts >= self._seed_at_ts)
            .filter(TickerSnapshot.exchange_ts <= self._start_ts)
            .order_by(TickerSnapshot.exchange_ts.desc())
            .first()
        )
        return row[0] if row else None

    def _iter_marks(self, symbol: str):
        """Yield ``(exchange_ts, mark_price)`` ascending across the window."""
        with self._db.get_session() as session:
            cursor_ts = self._start_ts
            use_gte = True
            while True:
                query = (
                    session.query(TickerSnapshot.exchange_ts, TickerSnapshot.mark_price)
                    .filter(TickerSnapshot.symbol == symbol)
                    .filter(TickerSnapshot.exchange_ts <= self._end_ts)
                )
                if use_gte:
                    query = query.filter(TickerSnapshot.exchange_ts >= cursor_ts)
                else:
                    query = query.filter(TickerSnapshot.exchange_ts > cursor_ts)

                rows = (
                    query.order_by(TickerSnapshot.exchange_ts)
                    .limit(self._batch_size)
                    .all()
                )
                if not rows:
                    break
                for ts, mark in rows:
                    yield ts, mark
                if len(rows) < self._batch_size:
                    break
                cursor_ts = rows[-1][0]
                use_gte = False

    def mark_at(self, coin: str, ts: datetime) -> Optional[Decimal]:
        """Latest ``mark_price`` with ``exchange_ts <= ts`` (carry-forward).

        Returns ``None`` until the first row at-or-before ``ts`` is seen (the
        caller then keeps the seed mark). Advances the per-coin cursor in place;
        ``ts`` MUST be non-decreasing across calls — the forward-only cursor
        cannot rewind, so a regression is rejected with ``ValueError`` rather
        than silently returning a too-new mark (guards against a future caller
        or an out-of-order data provider feeding ticks in the wrong order).
        """
        if coin not in self._iters:
            return None
        ts_cmp = _strip_tz(ts)
        last = self._last_ts.get(coin)
        if last is not None and ts_cmp < last:
            raise ValueError(
                f"CollateralMarkFeed.mark_at: non-monotonic ts for {coin}: "
                f"{ts} precedes previous {last}; the forward-only cursor cannot "
                f"rewind (replay ticks must be ordered by exchange_ts)."
            )
        self._last_ts[coin] = ts_cmp
        pend = self._pending.get(coin)
        while pend is not None and _strip_tz(pend[0]) <= ts_cmp:
            self._current[coin] = pend[1]
            pend = next(self._iters[coin], None)
            self._pending[coin] = pend
        return self._current[coin]


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
        # Feature 0080 (issue #183): client_order_id is now namespaced by strat_id.
        # Recorded LIVE orders were salted with the live strat_id, so replay MUST
        # salt with the SAME id to keep the comparator's (client_order_id,
        # occurrence) join and the seed round-trip matching. The recording's
        # strat_id is NOT stored on the Order/PrivateExecution/Strategy DB rows, so
        # it must be supplied via config. Precedence: explicit strategy.strat_id
        # (set when comparing against a recording, incl. blank-start) -> seed.strat_id
        # (when seeding) -> synthetic id (no recorded orders to match).
        if config.strategy.strat_id:
            strat_id = config.strategy.strat_id
        elif config.seed.enabled and config.seed.strat_id:
            strat_id = config.seed.strat_id
        else:
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
            # Feature 0071: risk-mgmt tunables pass-through (issue #162).
            min_liq_ratio=config.strategy.min_liq_ratio,
            max_liq_ratio=config.strategy.max_liq_ratio,
            min_total_margin=config.strategy.min_total_margin,
            increase_same_position_on_low_margin=(
                config.strategy.increase_same_position_on_low_margin
            ),
            leverage=config.strategy.leverage,
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
        # 0065: seed non-USDT collateral re-mark state onto the session
        # (empty when no seed / no collateral → identical to pre-0065).
        collateral_balances = (
            wallet_seed.coin_balances if wallet_seed is not None else {}
        )
        collateral_seed_marks = (
            wallet_seed.seed_marks if wallet_seed is not None else {}
        )
        session = BacktestSession(
            initial_balance=initial_balance,
            initial_equity=initial_equity,
            collateral_balances=collateral_balances,
            collateral_seed_marks=collateral_seed_marks,
        )

        # 2b. 0072: event_follower fill source — load the recorded live
        # execution stream for the window. ORM rows are materialized to
        # plain RecordedExecution dataclasses INSIDE the session context
        # (DetachedInstanceError on lazy access otherwise; cf. feature
        # 0038). get_by_run_range filters run_id + time only (same as
        # LiveTradeLoader), so other-symbol rows are skipped here.
        event_follower: Optional[EventFollower] = None
        if fill_mode == FillMode.EVENT_FOLLOWER:
            recorded_execs: list[RecordedExecution] = []
            skipped_other_symbol = 0
            with self._db.get_session() as db_session:
                exec_repo = PrivateExecutionRepository(db_session)
                # Ordered (exchange_ts, exec_id) by the repository — the
                # single sort site; not re-sorted here or in the follower.
                for ex in exec_repo.get_by_run_range(run_id, start_ts, end_ts):
                    if ex.symbol != config.symbol:
                        skipped_other_symbol += 1
                        continue
                    recorded_execs.append(
                        RecordedExecution(
                            exec_id=ex.exec_id,
                            order_link_id=ex.order_link_id,
                            order_id=ex.order_id,
                            side=ex.side,
                            exec_price=ex.exec_price,
                            exec_qty=ex.exec_qty,
                            exec_fee=(
                                ex.exec_fee
                                if ex.exec_fee is not None
                                else Decimal("0")
                            ),
                            closed_pnl=(
                                ex.closed_pnl
                                if ex.closed_pnl is not None
                                else Decimal("0")
                            ),
                            exchange_ts=_strip_tz(ex.exchange_ts),
                        )
                    )
            event_follower = EventFollower(
                recorded_execs,
                symbol=config.symbol,
                start_ts=_strip_tz(start_ts),
            )
            logger.info(
                "event_follower: loaded %d recorded executions for %s "
                "(%d other-symbol rows skipped)",
                len(recorded_execs), config.symbol, skipped_other_symbol,
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
            event_follower=event_follower,
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

        # 0065: collateral mark feed for non-USDT coins (the traded-symbol
        # provider does not carry their ticks). Built only when collateral is
        # modelled; reads ticker_snapshots for each coin's mapped *USDT symbol.
        collateral_feed: Optional[CollateralMarkFeed] = None
        if collateral_balances:
            symbol_for = {
                coin: config.seed.collateral_symbol_map.get(coin, f"{coin}USDT")
                for coin in collateral_balances
            }
            collateral_feed = CollateralMarkFeed(
                db=self._db,
                symbol_for=symbol_for,
                start_ts=start_ts,
                end_ts=end_ts,
                seed_at_ts=config.seed.at_ts,
            )
            logger.info(
                "0065 collateral re-mark active: coins=%s symbols=%s",
                list(collateral_balances), symbol_for,
            )

        # 4. Funding simulator
        funding_simulator = None
        if config.enable_funding:
            funding_simulator = FundingSimulator(rate=config.funding_rate)

        # 5. Replay loop (two-phase tick processing)
        last_price = Decimal("0")
        last_timestamp = None
        tick_count = 0
        # 0065: track which modelled collateral coins got at least one intra-run
        # ticker mark, so we can WARN about coins that ran on the seed mark for
        # the whole window (almost always a missing recorder.collateral_symbols).
        collateral_marked_coins: set[str] = set()

        for tick in provider:
            last_price = tick.last_price
            last_timestamp = tick.exchange_ts

            # 0065: refresh collateral marks at the TOP of the tick — BEFORE
            # process_fills. On a fill tick, process_fills -> refresh_balances
            # -> _estimate_pair_liq_prices reads session.total_equity for the
            # emitted snapshot's liq_price; refreshing the mark here keeps that
            # snapshot on the CURRENT tick's collateral value (else it lags one
            # collateral step on exactly the rows the comparator measures).
            if collateral_feed is not None:
                for coin in collateral_balances:
                    mark = collateral_feed.mark_at(coin, tick.exchange_ts)
                    if mark is not None:
                        session.update_collateral_mark(coin, mark)
                        collateral_marked_coins.add(coin)

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

        # 0072: trigger-4 end-of-replay sweep of remaining partial-fill
        # rollups. MUST run before wind-down, position-writer flush, and
        # session.finalize so swept record_trade rows land in session.trades
        # for wind-down inputs, DB telemetry, finalize stats, and
        # BacktestTradeLoader.load_from_session. No-op for simulator modes.
        runner.finalize_event_follower()

        # 0065: surface collateral coins that never got an intra-run ticker mark
        # (ran on the seed mark all window — usually a missing collateral symbol
        # in the recorder window). Silent otherwise → an unexplained #3a gap.
        if collateral_feed is not None:
            never_marked = set(collateral_balances) - collateral_marked_coins
            for coin in sorted(never_marked):
                logger.warning(
                    "0065: collateral coin %s had no ticker rows in the replay "
                    "window; used the seed mark throughout (check "
                    "recorder.collateral_symbols for its *USDT symbol).",
                    coin,
                )

        # 6. Wind down
        # NOTE (0065 #3a): when wind_down_mode == CLOSE_ALL, _wind_down closes
        # positions via tracker.process_fill + record_trade but does NOT call
        # refresh_balances / update_equity, and finalize() does not rewrite
        # session.total_equity. So after run(), session.total_equity reflects the
        # LAST tick-loop update_equity (open-position unrealized), not the flat
        # post-close state. #3a totalEquity-parity checks therefore require
        # wind_down_mode == LEAVE_OPEN (the default); close_all is out of scope
        # for #3a (see docs/features/0065_PLAN.md Phase 2B wind-down note).
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

        # 0065: collateral re-mark attribution (acceptance #3b + under-coverage
        # surfacing). Replay-side assignment from the session's latest marks +
        # the WalletSeed exclusion lists — NOT recomputed in fold_metrics_into.
        metrics.non_usdt_collateral_drift_total = session.collateral_drift_total
        metrics.collateral_drift_by_coin = session.collateral_drift_by_coin
        if wallet_seed is not None:
            metrics.collateral_excluded_coins = list(
                wallet_seed.collateral_excluded_coins
            )
            metrics.collateral_missing_mark_coins = list(
                wallet_seed.collateral_missing_mark_coins
            )
            metrics.collateral_switch_off_coins = list(
                wallet_seed.collateral_switch_off_coins
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
        event_follower: Optional[EventFollower] = None,
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
            event_follower: 0072 recorded-execution fill source. Stashed on
                the runner post-construction (same pattern as
                ``_position_writer``) — NOT a ``BacktestRunner.__init__``
                kwarg. The ``TradeThroughFillSimulator`` is still constructed
                unconditionally below; in event_follower mode it is never
                consulted because ``process_fills`` skips ``check_fills``.
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

        # 0072: stash the recorded-execution fill source. process_fills
        # detects the mode via `self._event_follower is not None`.
        if event_follower is not None:
            runner._event_follower = event_follower

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
        2. Loads grid state — DB snapshot first, then file when
           ``seed.grid_state_path`` is set. Both loaders fold step/count
           mismatch into a ``None`` return (treated the same as absence).
           If both return ``None``, raises :class:`SeedDataQualityError`:
           grid state is hard-required when ``seed.enabled=True`` (0054).
        3. Loads position pair, full 0042 wallet seed, and active orders
           inside a single DB session. Wallet is soft-required: when
           ``load_wallet_seed_full`` returns ``None``, the engine falls
           back to ``initial_balance`` and emits a ``WARNING``.

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

        # 0047 + 0054: try the DB grid-state snapshot first, fall back to the
        # file path when ``grid_state_path`` is set, then raise
        # ``SeedDataQualityError`` if both return ``None``. DB lookup needs a
        # session, so open it FIRST and run all loaders inside.
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
            grid_source: Optional[str] = "db" if grid_seed is not None else None
            if grid_seed is None:
                grid_seed = load_grid_state_from_active_snapshots(
                    db_session,
                    seed.strat_id,
                    config.symbol,
                    seed.at_ts,
                    expected_step=config.strategy.grid_step,
                    expected_count=config.strategy.grid_count,
                )
                if grid_seed is not None:
                    grid_source = "db"
            if grid_seed is None and seed.grid_state_path is not None:
                grid_seed = load_grid_state(
                    GridStateStore(file_path=seed.grid_state_path),
                    seed.strat_id,
                    expected_step=config.strategy.grid_step,
                    expected_count=config.strategy.grid_count,
                )
                grid_source = "file" if grid_seed is not None else None

            if grid_source is None:
                raise SeedDataQualityError(
                    f"seed.enabled=True but no grid state found for "
                    f"strat_id={seed.strat_id!r} "
                    f"account_id={seed.account_id!r} "
                    f"symbol={config.symbol!r} "
                    f"at_ts={seed.at_ts.isoformat()} "
                    f"grid_state_path={seed.grid_state_path!r}"
                )
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

            # 0065: merge non-USDT collateral re-mark seed onto the wallet
            # seed (same DB session — load_collateral_seed needs DB access).
            # Non-empty collateral_coins HARD-REQUIRES a valid wallet seed:
            # the soft-fallback to initial_balance is unsafe here because the
            # collateral re-mark term is anchored to live total_equity at
            # at_ts, and dataclasses.replace cannot operate on None.
            if seed.collateral_coins:
                if wallet_seed is None:
                    raise SeedDataQualityError(
                        f"seed.collateral_coins={seed.collateral_coins!r} requires a "
                        f"valid account-level wallet seed (total_equity / "
                        f"total_available_balance > 0) at at_ts="
                        f"{seed.at_ts.isoformat()} for run_id={run_id!r} "
                        f"account_id={seed.account_id!r} coin={seed.wallet_coin!r}; "
                        f"collateral re-marking and end-of-window totalEquity "
                        f"parity cannot be modelled without it."
                    )
                (
                    coin_balances,
                    seed_marks,
                    value_ratios,
                    excluded_coins,
                    missing_mark_coins,
                    switch_off_coins,
                ) = load_collateral_seed(
                    db_session,
                    run_id,
                    seed.account_id,
                    seed.at_ts,
                    seed.wallet_coin,
                    seed.collateral_coins,
                    seed.collateral_symbol_map,
                    seed.collateral_value_ratios,
                    seed.collateral_wallet_max_staleness,
                )
                wallet_seed = replace(
                    wallet_seed,
                    coin_balances=coin_balances,
                    seed_marks=seed_marks,
                    collateral_value_ratios=value_ratios,
                    collateral_excluded_coins=excluded_coins,
                    collateral_missing_mark_coins=missing_mark_coins,
                    collateral_switch_off_coins=switch_off_coins,
                )

        # Reached only when collateral_coins is empty — the non-empty case
        # raises above instead of soft-falling back.
        if wallet_seed is None:
            logger.warning(
                "%s: wallet seed missing, defaulting to initial_balance",
                seed.strat_id,
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
        # (or vice versa). Strip uniformly before comparing (module-level
        # _strip_tz, shared with snapshot_loader.load_collateral_seed).
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
