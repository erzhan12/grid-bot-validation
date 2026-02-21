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
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from datetime import datetime, timezone

from grid_db import DatabaseFactory, Run, RunRepository

from gridcore import DirectionType

from backtest.config import BacktestStrategyConfig, WindDownMode
from backtest.data_provider import HistoricalDataProvider, InMemoryDataProvider
from backtest.engine import FundingSimulator
from backtest.executor import BacktestExecutor
from backtest.fill_simulator import TradeThroughFillSimulator
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

from replay.config import ReplayConfig


logger = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    """Result of a replay run."""

    session: BacktestSession
    metrics: ValidationMetrics
    match_result: MatchResult


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

        # 1. Resolve run_id and time range
        run_id, start_ts, end_ts = self._resolve_run(config)

        logger.info(
            f"Replay: symbol={config.symbol}, run_id={run_id}, "
            f"range={start_ts} to {end_ts}"
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
            long_koef=config.strategy.long_koef,
            commission_rate=config.strategy.commission_rate,
        )

        session = BacktestSession(initial_balance=config.initial_balance)
        runner = self._init_runner(strategy_config, session)

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

        return ReplayResult(
            session=session,
            metrics=metrics,
            match_result=match_result,
        )

    def _resolve_run(self, config: ReplayConfig):
        """Resolve run_id and time range from config or database.

        Returns:
            Tuple of (run_id, start_ts, end_ts).
        """
        run_id = config.run_id

        if run_id is None:
            # Auto-discover latest recording run
            with self._db.get_session() as session:
                repo = RunRepository(session)
                run = repo.get_latest_by_type("recording")
                if run is None:
                    from urllib.parse import urlparse
                    raw_url = self._db.settings.get_database_url()
                    _p = urlparse(raw_url)
                    safe_url = _p.scheme + "://..." + _p.path if _p.password else raw_url
                    raise ValueError(
                        f"No recording runs found in database ({safe_url}). "
                        "Ensure recorder has completed at least one run."
                    )
                # Extract values while still in session scope
                run_id = run.run_id
                run_start = run.start_ts
                run_end = run.end_ts

            logger.info(f"Auto-discovered recording run: {run_id}")

            start_ts = config.start_ts or run_start
            end_ts = config.end_ts or run_end
        else:
            start_ts = config.start_ts
            end_ts = config.end_ts
            # Fetch Run row if either timestamp is missing
            if start_ts is None or end_ts is None:
                with self._db.get_session() as session:
                    run = session.get(Run, run_id)
                    if run is None:
                        raise ValueError(f"Run '{run_id}' not found in database")
                    if start_ts is None:
                        start_ts = run.start_ts
                    if end_ts is None:
                        end_ts = run.end_ts

        # Handle active runs (end_ts still None → use utcnow)
        if end_ts is None:
            end_ts = datetime.now(timezone.utc)
            logger.info(f"Run still active, using now as end_ts: {end_ts}")

        if start_ts is None:
            raise ValueError("start_ts could not be resolved")

        return run_id, start_ts, end_ts

    def _init_runner(
        self,
        strategy_config: BacktestStrategyConfig,
        session: BacktestSession,
    ) -> BacktestRunner:
        """Initialize a BacktestRunner for the replay."""
        instrument_info = self._instrument_provider.get(strategy_config.symbol)
        logger.info(
            f"Instrument {strategy_config.symbol}: "
            f"qty_step={instrument_info.qty_step}, tick_size={instrument_info.tick_size}"
        )

        fill_simulator = TradeThroughFillSimulator()
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

        return BacktestRunner(
            strategy_config=strategy_config,
            executor=executor,
            session=session,
            long_tracker=long_tracker,
            short_tracker=short_tracker,
        )

    def _create_qty_calculator(self, config, instrument_info):
        """Create qty calculator from amount pattern.

        Mirrors BacktestEngine._create_qty_calculator().
        """
        amount_str = config.amount

        if amount_str.startswith("x"):
            fraction = Decimal(amount_str[1:])

            def qty_from_fraction(intent, wallet_balance):
                if intent.price <= 0:
                    return Decimal("0")
                raw_qty = wallet_balance * fraction / intent.price
                return instrument_info.round_qty(raw_qty)

            return qty_from_fraction

        elif amount_str.startswith("b"):
            base_qty = Decimal(amount_str[1:])

            def qty_fixed_base(intent, wallet_balance):
                return instrument_info.round_qty(base_qty)

            return qty_fixed_base

        else:
            usdt_amount = Decimal(amount_str)

            def qty_from_usdt(intent, wallet_balance):
                if intent.price <= 0:
                    return Decimal("0")
                raw_qty = usdt_amount / intent.price
                return instrument_info.round_qty(raw_qty)

            return qty_from_usdt

    def _wind_down(
        self,
        runner: BacktestRunner,
        session: BacktestSession,
        last_price: Decimal,
        last_timestamp,
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
