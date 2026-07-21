"""Shared-wallet multi-strategy replay engine."""

from __future__ import annotations

import heapq
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Iterator, Optional

from sqlalchemy import desc

from grid_db import (
    DatabaseFactory,
    PrivateExecutionRepository,
    Run,
    RunRepository,
    TickerSnapshot,
    redact_db_url,
)

from backtest.config import BacktestStrategyConfig, WindDownMode
from backtest.data_provider import HistoricalDataProvider, InMemoryDataProvider
from backtest.engine import FundingSimulator
from backtest.fill_simulator import EventFollower, FillMode, RecordedExecution
from backtest.runner import BacktestRunner
from backtest.session import BacktestSession

from comparator import (
    BacktestTradeLoader,
    LiveTradeLoader,
    MatchResult,
    TradeMatcher,
    ValidationMetrics,
    calculate_metrics,
)

from replay.engine import (
    CollateralMarkFeed,
    ReplayEngine,
    _NoopPositionSnapshotWriter,
)
from replay.multi_config import MultiReplayConfig, MultiReplayStrategyConfig
from replay.snapshot_loader import (
    ActiveOrderSeed,
    GridStateSeed,
    PositionStateSeed,
    SeedDataQualityError,
    WalletSeed,
    _strip_tz,
    load_active_orders,
    load_collateral_seed,
    load_grid_state_from_active_snapshots,
    load_grid_state_from_snapshots,
    load_position_snapshots,
    load_wallet_seed_full,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountCurveSample:
    """One replayed account-level reconciliation sample."""

    exchange_ts: datetime
    total_equity: Decimal
    total_margin_balance: Decimal
    account_mm_rate: Decimal


@dataclass(frozen=True)
class MultiStrategyReplayResult:
    """Per-strategy replay comparison output under one shared session."""

    strategy: MultiReplayStrategyConfig
    runner: BacktestRunner
    metrics: ValidationMetrics
    match_result: MatchResult
    final_unrealized: Decimal


@dataclass(frozen=True)
class MultiReplayResult:
    """Result of a shared-wallet multi-strategy replay."""

    session: BacktestSession
    strategies: dict[str, MultiStrategyReplayResult]
    run_id: str
    account_id: Optional[str]
    start_ts: datetime
    end_ts: datetime
    fill_mode: FillMode
    account_curve: list[AccountCurveSample] = field(default_factory=list)

    @property
    def total_equity_curve(self) -> list[tuple[datetime, Decimal]]:
        """Replayed ``total_equity`` series."""
        return [(s.exchange_ts, s.total_equity) for s in self.account_curve]

    @property
    def total_margin_balance_curve(self) -> list[tuple[datetime, Decimal]]:
        """Replayed ``total_margin_balance`` series."""
        return [
            (s.exchange_ts, s.total_margin_balance) for s in self.account_curve
        ]

    @property
    def account_mm_rate_curve(self) -> list[tuple[datetime, Decimal]]:
        """Replayed account maintenance-margin ratio series."""
        return [(s.exchange_ts, s.account_mm_rate) for s in self.account_curve]


@dataclass
class _RunnerBundle:
    config: MultiReplayStrategyConfig
    runner: BacktestRunner
    funding: Optional[FundingSimulator]


class _SharedSessionCoordinator:
    """Aggregate session-global refresh/pending values across runners."""

    def __init__(
        self,
        session: BacktestSession,
        runners: dict[str, BacktestRunner],
        last_prices: dict[str, Decimal],
    ):
        self._session = session
        self._runners = runners
        self._last_prices = last_prices
        self._pending: dict[str, tuple[Decimal, Decimal]] = {
            symbol: (Decimal("0"), Decimal("0")) for symbol in runners
        }
        self._active_symbol: Optional[str] = None
        self._orig_set_pending = session.set_pending_wallet
        self._orig_clear_pending = session.clear_pending_wallet
        self._orig_refresh = session.refresh_balances
        session.set_pending_wallet = self.set_pending_wallet  # type: ignore[method-assign]
        session.clear_pending_wallet = self.clear_pending_wallet  # type: ignore[method-assign]
        session.refresh_balances = self.refresh_balances  # type: ignore[method-assign]

    @contextmanager
    def active(self, symbol: str):
        """Route runner-owned session calls to the symbol's aggregate slot."""
        previous = self._active_symbol
        self._active_symbol = symbol
        try:
            yield
        finally:
            self._active_symbol = previous

    def set_pending_wallet(
        self,
        pending_realized: Decimal,
        pending_commission: Decimal,
    ) -> None:
        """Store active-symbol pending and publish the account-wide sum."""
        if self._active_symbol is None:
            self._orig_set_pending(pending_realized, pending_commission)
            return
        self._pending[self._active_symbol] = (pending_realized, pending_commission)
        self._publish_pending()

    def clear_pending_wallet(self) -> None:
        """Clear the active symbol's pending state, or all state outside a runner."""
        if self._active_symbol is None:
            self._pending = {
                symbol: (Decimal("0"), Decimal("0")) for symbol in self._runners
            }
        else:
            self._pending[self._active_symbol] = (Decimal("0"), Decimal("0"))
        self._publish_pending()

    def refresh_balances(self, unrealized_pnl: Decimal) -> None:
        """Refresh shared balances with Σ unrealized, not the caller's own leg."""
        del unrealized_pnl
        self._orig_refresh(self.total_unrealized())

    def total_unrealized(self) -> Decimal:
        """Return Σ unrealized across all runners at cached own-symbol marks."""
        total = Decimal("0")
        for symbol, runner in self._runners.items():
            price = self._last_prices.get(symbol, Decimal("0"))
            if price <= 0:
                continue
            total += runner.long_tracker.calculate_unrealized_pnl(price)
            total += runner.short_tracker.calculate_unrealized_pnl(price)
        return total

    def total_im_mm(self) -> tuple[Decimal, Decimal]:
        """Return account-level Σ initial and maintenance margin."""
        total_im = Decimal("0")
        total_mm = Decimal("0")
        for symbol, runner in self._runners.items():
            price = self._last_prices.get(symbol, Decimal("0"))
            if price <= 0:
                continue
            im_long, mm_long, im_short, mm_short = runner._estimate_pair_im_mm(
                runner.long_tracker.state,
                runner.short_tracker.state,
                price,
            )
            total_im += im_long + im_short
            total_mm += mm_long + mm_short
        return total_im, total_mm

    def _publish_pending(self) -> None:
        pending_realized = sum((v[0] for v in self._pending.values()), Decimal("0"))
        pending_commission = sum((v[1] for v in self._pending.values()), Decimal("0"))
        self._orig_set_pending(pending_realized, pending_commission)


class MultiReplayEngine(ReplayEngine):
    """Replay multiple strategies against one shared backtest wallet."""

    def __init__(
        self,
        config: MultiReplayConfig,
        db: DatabaseFactory,
        emit_backtest_snapshots: bool = True,
    ):
        self._multi_config = config
        self._db = db
        self._emit_backtest_snapshots = emit_backtest_snapshots
        from backtest.instrument_info import InstrumentInfoProvider

        self._instrument_provider = InstrumentInfoProvider()

    def run(
        self,
        data_providers: Optional[dict[str, InMemoryDataProvider]] = None,
    ) -> MultiReplayResult:
        """Run shared-wallet replay.

        Args:
            data_providers: Optional per-symbol in-memory providers for tests.

        Returns:
            Shared-wallet replay result with per-strategy comparisons.
        """
        config = self._multi_config
        run_id, account_id, start_ts, end_ts = self._resolve_run_multi(config)
        fill_mode = FillMode(config.fill_simulator.mode)
        wallet_seed, seed_data = self._load_multi_seed(config, run_id)
        session = self._build_shared_session(config, wallet_seed)

        bundles: dict[str, _RunnerBundle] = {}
        last_prices = self._startup_mark_cache(config, start_ts, data_providers)
        for strat in config.strategies:
            yaml_tick = strat.tick_size
            instrument_info = self._instrument_provider.get(
                strat.symbol, require_live=yaml_tick is None
            )
            from backtest.instrument_info import resolve_tick_size

            strategy_config = self._strategy_config(
                strat,
                resolve_tick_size(yaml_tick, instrument_info.tick_size),
            )
            long_seed, short_seed, grid_seed, order_seeds = seed_data[strat.symbol]
            event_follower = self._event_follower(
                run_id, strat.symbol, start_ts, end_ts, fill_mode
            )
            runner = self._init_runner(
                strategy_config,
                session,
                instrument_info=instrument_info,
                long_seed=long_seed,
                short_seed=short_seed,
                grid_seed=grid_seed,
                order_seeds=order_seeds,
                fill_mode=fill_mode,
                run_id=run_id,
                account_id=account_id,
                event_follower=event_follower,
            )
            if not self._emit_backtest_snapshots:
                runner._position_writer = _NoopPositionSnapshotWriter()  # type: ignore[attr-defined]
            bundles[strat.symbol] = _RunnerBundle(
                config=strat,
                runner=runner,
                funding=(
                    FundingSimulator(rate=config.funding_rate)
                    if config.enable_funding else None
                ),
            )

        runners = {symbol: bundle.runner for symbol, bundle in bundles.items()}
        coordinator = _SharedSessionCoordinator(session, runners, last_prices)
        providers = data_providers or {
            strat.symbol: HistoricalDataProvider(
                db=self._db,
                symbol=strat.symbol,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            for strat in config.strategies
        }
        collateral_feed = self._collateral_feed(
            config, session, start_ts, end_ts, wallet_seed
        )
        collateral_marked: set[str] = set()
        account_curve: list[AccountCurveSample] = []
        tick_count = 0

        for symbol, tick in self._merge_ticks(providers):
            last_prices[symbol] = tick.last_price
            if collateral_feed is not None:
                for coin in session.collateral_balances:
                    mark = collateral_feed.mark_at(coin, tick.exchange_ts)
                    if mark is not None:
                        session.update_collateral_mark(coin, mark)
                        collateral_marked.add(coin)

            bundle = bundles[symbol]
            if (
                bundle.funding is not None
                and bundle.funding.should_apply_funding(tick.exchange_ts)
            ):
                funding = bundle.runner.apply_funding(
                    bundle.funding.rate, tick.last_price
                )
                if funding != 0:
                    logger.debug("%s funding payment: %s", symbol, funding)
                bundle.funding.mark_funding_applied(tick.exchange_ts)

            with coordinator.active(symbol):
                bundle.runner.process_fills(tick)

            unrealized = coordinator.total_unrealized()
            total_im, total_mm = coordinator.total_im_mm()
            session.update_equity(tick.exchange_ts, unrealized, total_im, total_mm)
            account_curve.append(
                self._account_sample(tick.exchange_ts, session, total_mm)
            )

            with coordinator.active(symbol):
                bundle.runner.execute_tick(tick)

            tick_count += 1
            if tick_count % 10000 == 0:
                logger.info("Processed %d merged ticks...", tick_count)

        logger.info("Multi replay complete: %d merged ticks processed", tick_count)
        self._warn_unmarked_collateral(session, collateral_feed, collateral_marked)

        for symbol, bundle in bundles.items():
            with coordinator.active(symbol):
                bundle.runner.finalize_event_follower()

        if config.wind_down_mode == WindDownMode.CLOSE_ALL:
            for symbol, bundle in bundles.items():
                price = last_prices.get(symbol, Decimal("0"))
                if price > 0:
                    self._wind_down(bundle.runner, session, price, end_ts)

        for bundle in bundles.values():
            writer = getattr(bundle.runner, "_position_writer", None)
            if writer is not None:
                writer.flush()

        final_unrealized_by_symbol = {
            symbol: self._runner_unrealized(bundle.runner, last_prices)
            for symbol, bundle in bundles.items()
        }
        session.finalize(
            sum(final_unrealized_by_symbol.values(), Decimal("0"))
        )

        per_strategy = {
            symbol: self._compare_symbol(
                bundle.config,
                bundle.runner,
                session,
                run_id,
                start_ts,
                end_ts,
                fill_mode,
                final_unrealized_by_symbol[symbol],
            )
            for symbol, bundle in bundles.items()
        }

        return MultiReplayResult(
            session=session,
            strategies=per_strategy,
            run_id=run_id,
            account_id=account_id,
            start_ts=start_ts,
            end_ts=end_ts,
            fill_mode=fill_mode,
            account_curve=account_curve,
        )

    @staticmethod
    def _merge_ticks(
        providers: dict[str, Iterable],
    ) -> Iterator[tuple[str, object]]:
        """K-way merge provider ticks by ``(exchange_ts, symbol, sequence)``."""
        heap: list[tuple[datetime, str, int, object, Iterator]] = []
        for symbol in sorted(providers):
            iterator = iter(providers[symbol])
            try:
                tick = next(iterator)
            except StopIteration:
                continue
            heapq.heappush(heap, (tick.exchange_ts, symbol, 0, tick, iterator))

        while heap:
            _ts, symbol, seq, tick, iterator = heapq.heappop(heap)
            yield symbol, tick
            try:
                nxt = next(iterator)
            except StopIteration:
                continue
            heapq.heappush(heap, (nxt.exchange_ts, symbol, seq + 1, nxt, iterator))

    def _resolve_run_multi(
        self, config: MultiReplayConfig
    ) -> tuple[str, Optional[str], datetime, datetime]:
        """Resolve run_id/account_id/time range for a multi replay."""
        run_id = config.run_id
        with self._db.get_session() as session:
            if run_id is None:
                run = RunRepository(session).get_latest_by_type("recording")
                if run is None:
                    safe_url = redact_db_url(self._db.settings.get_database_url())
                    raise ValueError(f"No recording runs found in database ({safe_url})")
            else:
                run = session.get(Run, run_id)
                if run is None:
                    raise ValueError(f"Run '{run_id}' not found in database")
            resolved_run_id = run.run_id
            account_id = run.account_id
            start_ts = config.start_ts or run.start_ts
            end_ts = config.end_ts or run.end_ts

        if end_ts is None:
            end_ts = datetime.now(timezone.utc)
        if start_ts is None:
            raise ValueError("start_ts could not be resolved")
        if start_ts.replace(tzinfo=None) >= end_ts.replace(tzinfo=None):
            raise ValueError(
                f"Invalid time range: start_ts ({start_ts}) must be before "
                f"end_ts ({end_ts})"
            )
        return resolved_run_id, account_id, start_ts, end_ts

    def _load_multi_seed(
        self,
        config: MultiReplayConfig,
        run_id: str,
    ) -> tuple[
        Optional[WalletSeed],
        dict[
            str,
            tuple[
                Optional[PositionStateSeed],
                Optional[PositionStateSeed],
                Optional[GridStateSeed],
                Optional[list[ActiveOrderSeed]],
            ],
        ],
    ]:
        """Load account-level wallet seed and per-symbol position/grid/order seeds."""
        seed = config.seed
        empty = {
            strat.symbol: (None, None, None, None) for strat in config.strategies
        }
        if not seed.enabled:
            return None, empty
        if seed.at_ts is None or seed.account_id is None:
            raise ValueError("seed.enabled=True but at_ts/account_id are unset")

        with self._db.get_session() as db_session:
            self._seed_pre_check(db_session, run_id, seed.at_ts)
            wallet_seed = load_wallet_seed_full(
                db_session,
                run_id,
                seed.account_id,
                seed.at_ts,
                coin=seed.wallet_coin,
            )
            if seed.collateral_coins:
                if wallet_seed is None:
                    raise SeedDataQualityError(
                        "seed.collateral_coins requires a valid wallet seed"
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

            data = {}
            for strat in config.strategies:
                grid_seed = load_grid_state_from_snapshots(
                    db_session,
                    seed.account_id,
                    strat.strat_id,
                    strat.symbol,
                    seed.at_ts,
                    expected_step=strat.grid_step,
                    expected_count=strat.grid_count,
                )
                if grid_seed is None:
                    grid_seed = load_grid_state_from_active_snapshots(
                        db_session,
                        strat.strat_id,
                        strat.symbol,
                        seed.at_ts,
                        expected_step=strat.grid_step,
                        expected_count=strat.grid_count,
                    )
                if grid_seed is None:
                    raise SeedDataQualityError(
                        f"seed.enabled=True but no grid state found for "
                        f"strat_id={strat.strat_id!r} symbol={strat.symbol!r}"
                    )
                long_seed, short_seed = load_position_snapshots(
                    db_session,
                    run_id,
                    seed.account_id,
                    strat.symbol,
                    seed.at_ts,
                )
                order_seeds = load_active_orders(
                    db_session,
                    run_id,
                    seed.account_id,
                    strat.symbol,
                    seed.at_ts,
                )
                data[strat.symbol] = (long_seed, short_seed, grid_seed, order_seeds)

        if wallet_seed is None:
            logger.warning("multi replay wallet seed missing, using initial_balance")
        return wallet_seed, data

    @staticmethod
    def _build_shared_session(
        config: MultiReplayConfig,
        wallet_seed: Optional[WalletSeed],
    ) -> BacktestSession:
        """Build the one account-level BacktestSession."""
        initial_balance = (
            wallet_seed.total_available_balance
            if wallet_seed is not None else config.initial_balance
        )
        initial_equity = (
            wallet_seed.total_equity if wallet_seed is not None else initial_balance
        )
        return BacktestSession(
            initial_balance=initial_balance,
            initial_equity=initial_equity,
            collateral_balances=(
                wallet_seed.coin_balances if wallet_seed is not None else {}
            ),
            collateral_seed_marks=(
                wallet_seed.seed_marks if wallet_seed is not None else {}
            ),
        )

    @staticmethod
    def _strategy_config(
        strat: MultiReplayStrategyConfig,
        tick_size: Decimal,
    ) -> BacktestStrategyConfig:
        """Project a multi strategy config into BacktestStrategyConfig."""
        return BacktestStrategyConfig(
            strat_id=strat.strat_id,
            symbol=strat.symbol,
            tick_size=tick_size,
            grid_count=strat.grid_count,
            grid_step=strat.grid_step,
            amount=strat.amount,
            max_margin=strat.max_margin,
            early_imbalance_multiplier=strat.early_imbalance_multiplier,
            commission_rate=strat.commission_rate,
            enable_risk_multipliers=strat.enable_risk_multipliers,
            min_liq_ratio=strat.min_liq_ratio,
            max_liq_ratio=strat.max_liq_ratio,
            min_total_margin=strat.min_total_margin,
            increase_same_position_on_low_margin=(
                strat.increase_same_position_on_low_margin
            ),
            leverage=strat.leverage,
        )

    def _event_follower(
        self,
        run_id: str,
        symbol: str,
        start_ts: datetime,
        end_ts: datetime,
        fill_mode: FillMode,
    ) -> Optional[EventFollower]:
        """Load a per-symbol EventFollower when requested."""
        if fill_mode != FillMode.EVENT_FOLLOWER:
            return None
        recorded_execs: list[RecordedExecution] = []
        skipped = 0
        with self._db.get_session() as db_session:
            repo = PrivateExecutionRepository(db_session)
            for ex in repo.get_by_run_range(run_id, start_ts, end_ts):
                if ex.symbol != symbol:
                    skipped += 1
                    continue
                recorded_execs.append(
                    RecordedExecution(
                        exec_id=ex.exec_id,
                        order_link_id=ex.order_link_id,
                        order_id=ex.order_id,
                        side=ex.side,
                        exec_price=ex.exec_price,
                        exec_qty=ex.exec_qty,
                        exec_fee=ex.exec_fee if ex.exec_fee is not None else Decimal("0"),
                        closed_pnl=(
                            ex.closed_pnl if ex.closed_pnl is not None else Decimal("0")
                        ),
                        exchange_ts=_strip_tz(ex.exchange_ts),
                    )
                )
        logger.info(
            "event_follower: loaded %d recorded executions for %s "
            "(%d other-symbol rows skipped)",
            len(recorded_execs), symbol, skipped,
        )
        return EventFollower(recorded_execs, symbol=symbol, start_ts=_strip_tz(start_ts))

    def _startup_mark_cache(
        self,
        config: MultiReplayConfig,
        start_ts: datetime,
        data_providers: Optional[dict[str, InMemoryDataProvider]],
    ) -> dict[str, Decimal]:
        """Seed last-price cache from at-or-before DB marks or in-memory first ticks."""
        marks = {strat.symbol: Decimal("0") for strat in config.strategies}
        if data_providers is not None:
            for symbol, provider in data_providers.items():
                events = getattr(provider, "_events", [])
                before = [
                    event for event in events
                    if event.exchange_ts.replace(tzinfo=None)
                    <= start_ts.replace(tzinfo=None)
                ]
                if before:
                    marks[symbol] = before[-1].last_price
                else:
                    logger.warning(
                        "%s: no at-or-before-start mark; idle-book unrealized/"
                        "IM/MM carries 0 until its first own tick (O2)", symbol)
            return marks

        with self._db.get_session() as session:
            for strat in config.strategies:
                row = (
                    session.query(TickerSnapshot.last_price)
                    .filter(TickerSnapshot.symbol == strat.symbol)
                    .filter(TickerSnapshot.exchange_ts <= _strip_tz(start_ts))
                    .order_by(desc(TickerSnapshot.exchange_ts))
                    .first()
                )
                if row is not None:
                    marks[strat.symbol] = row[0]
                else:
                    logger.warning(
                        "%s: no at-or-before-start ticker mark; idle-book "
                        "unrealized/IM/MM carries 0 until its first own tick "
                        "(O2)", strat.symbol)
        return marks

    def _collateral_feed(
        self,
        config: MultiReplayConfig,
        session: BacktestSession,
        start_ts: datetime,
        end_ts: datetime,
        wallet_seed: Optional[WalletSeed],
    ) -> Optional[CollateralMarkFeed]:
        """Build the account-level collateral mark feed when needed."""
        if not session.collateral_balances:
            return None
        symbol_for = {
            coin: config.seed.collateral_symbol_map.get(coin, f"{coin}USDT")
            for coin in session.collateral_balances
        }
        return CollateralMarkFeed(
            db=self._db,
            symbol_for=symbol_for,
            start_ts=start_ts,
            end_ts=end_ts,
            seed_at_ts=config.seed.at_ts if wallet_seed is not None else None,
        )

    @staticmethod
    def _account_sample(
        ts: datetime,
        session: BacktestSession,
        total_mm: Decimal,
    ) -> AccountCurveSample:
        """Build one emitted account-level reconcile sample."""
        total_margin_balance = session.total_equity
        account_mm_rate = (
            total_mm / total_margin_balance
            if total_margin_balance != 0 else Decimal("0")
        )
        return AccountCurveSample(
            exchange_ts=ts,
            total_equity=session.total_equity,
            total_margin_balance=total_margin_balance,
            account_mm_rate=account_mm_rate,
        )

    @staticmethod
    def _runner_unrealized(
        runner: BacktestRunner,
        last_prices: dict[str, Decimal],
    ) -> Decimal:
        """Return one runner's final unrealized at its cached own-symbol price."""
        price = last_prices.get(runner.symbol, Decimal("0"))
        if price <= 0:
            return Decimal("0")
        return (
            runner.long_tracker.calculate_unrealized_pnl(price)
            + runner.short_tracker.calculate_unrealized_pnl(price)
        )

    def _compare_symbol(
        self,
        strat: MultiReplayStrategyConfig,
        runner: BacktestRunner,
        session: BacktestSession,
        run_id: str,
        start_ts: datetime,
        end_ts: datetime,
        fill_mode: FillMode,
        final_unrealized: Decimal,
    ) -> MultiStrategyReplayResult:
        """Run per-symbol trade matching and metrics under a shared session."""
        with self._db.get_session() as db_session:
            live_loader = LiveTradeLoader(db_session)
            recorded_trades = live_loader.load(
                run_id=run_id,
                start_ts=start_ts,
                end_ts=end_ts,
                symbol=strat.symbol,
            )
        bt_loader = BacktestTradeLoader()
        simulated = [
            trade for trade in bt_loader.load_from_session(session.trades)
            if trade.symbol == strat.symbol
        ]
        matcher = TradeMatcher()
        match_result = matcher.match(recorded_trades, simulated)
        metrics = calculate_metrics(
            match_result,
            price_tolerance=self._multi_config.price_tolerance,
            qty_tolerance=self._multi_config.qty_tolerance,
        )
        return MultiStrategyReplayResult(
            strategy=strat,
            runner=runner,
            metrics=metrics,
            match_result=match_result,
            final_unrealized=final_unrealized,
        )

    @staticmethod
    def _warn_unmarked_collateral(
        session: BacktestSession,
        collateral_feed: Optional[CollateralMarkFeed],
        marked: set[str],
    ) -> None:
        """Warn for collateral coins that never received an in-window mark."""
        if collateral_feed is None:
            return
        for coin in sorted(set(session.collateral_balances) - marked):
            logger.warning(
                "collateral coin %s had no ticker rows in the replay window; "
                "used seed mark throughout",
                coin,
            )
