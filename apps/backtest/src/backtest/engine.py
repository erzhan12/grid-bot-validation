"""Backtest engine - main orchestrator for running backtests.

Coordinates data providers, runners, funding simulation, and result persistence.
"""

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterator, Optional

from grid_db import DatabaseFactory
from gridcore import DirectionType, SideType

from backtest.config import BacktestConfig, BacktestStrategyConfig, WindDownMode
from backtest.data_provider import HistoricalDataProvider, InMemoryDataProvider, DataRangeInfo
from backtest.fill_simulator import TradeThroughFillSimulator
from backtest.instrument_info import InstrumentInfoProvider, InstrumentInfo
from backtest.order_manager import BacktestOrderManager
from backtest.executor import BacktestExecutor
from backtest.position_tracker import BacktestPositionTracker
from backtest.risk_limit_info import RiskLimitProvider
from backtest.runner import BacktestRunner
from backtest.session import BacktestSession, BacktestMetrics, BacktestTrade


logger = logging.getLogger(__name__)


class FundingSimulator:
    """Simulates Bybit funding payments at 8-hour intervals.

    Funding times: 00:00, 08:00, 16:00 UTC.
    """

    FUNDING_HOURS = {0, 8, 16}

    def __init__(self, rate: Decimal = Decimal("0.0001")):
        """Initialize funding simulator.

        Args:
            rate: Funding rate per 8-hour period (0.0001 = 0.01%).
        """
        self.rate = rate
        self._last_funding_time: Optional[datetime] = None

    def reset(self) -> None:
        """Reset funding simulator state for a new run."""
        self._last_funding_time = None

    def should_apply_funding(self, current_time: datetime) -> bool:
        """Check if funding should be applied at this time.

        Funding is applied when:
        1. Current hour is a funding hour (0, 8, 16 UTC)
        2. We haven't already applied funding for this period

        Args:
            current_time: Current timestamp.

        Returns:
            True if funding should be applied.
        """
        hour = current_time.hour
        if hour not in self.FUNDING_HOURS:
            return False

        # Check if we already applied for this funding period
        if self._last_funding_time is not None:
            # Same funding period if within 8 hours and same funding hour
            time_diff = current_time - self._last_funding_time
            if time_diff < timedelta(hours=7) and self._last_funding_time.hour == hour:
                return False

        return True

    def mark_funding_applied(self, current_time: datetime) -> None:
        """Mark that funding has been applied."""
        self._last_funding_time = current_time


class BacktestEngine:
    """Main orchestrator for running backtests.

    Coordinates:
    - Data provider (historical data)
    - Strategy runners (GridEngine wrappers)
    - Funding simulation
    - Result storage and metrics

    Example:
        config = load_config("backtest.yaml")
        db = DatabaseFactory(settings)

        engine = BacktestEngine(config, db)
        session = engine.run(
            symbol="BTCUSDT",
            start_ts=datetime(2025, 1, 1),
            end_ts=datetime(2025, 1, 31),
        )

        print(session.get_summary())
    """

    def __init__(
        self,
        config: BacktestConfig,
        db: Optional[DatabaseFactory] = None,
    ):
        """Initialize backtest engine.

        Args:
            config: Backtest configuration.
            db: Database factory (optional for in-memory backtests).
        """
        self._config = config
        self._db = db
        self._instrument_provider = InstrumentInfoProvider(
            cache_ttl=timedelta(hours=config.instrument_cache_ttl_hours),
        )
        self._risk_limit_provider = RiskLimitProvider()

        # Session and runners (created per run)
        self._session: Optional[BacktestSession] = None
        self._runners: dict[str, BacktestRunner] = {}
        self._last_prices: dict[str, Decimal] = {}  # symbol -> last price
        self._last_timestamp: Optional[datetime] = None

        # Funding simulator
        self._funding_simulator: Optional[FundingSimulator] = None
        if config.enable_funding:
            self._funding_simulator = FundingSimulator(rate=config.funding_rate)

    def run(
        self,
        symbol: str,
        start_ts: datetime,
        end_ts: datetime,
        data_provider: Optional[InMemoryDataProvider] = None,
    ) -> BacktestSession:
        """Run backtest for a symbol.

        Args:
            symbol: Trading symbol.
            start_ts: Start timestamp.
            end_ts: End timestamp.
            data_provider: Optional in-memory data provider (for testing).

        Returns:
            BacktestSession with results.
        """
        # Reset state for clean run
        self._runners = {}
        self._last_prices = {}
        self._last_timestamp = None
        if self._funding_simulator:
            self._funding_simulator.reset()

        # Create session
        self._session = BacktestSession(
            initial_balance=self._config.initial_balance,
        )

        # Get strategies for this symbol
        strategies = self._config.get_strategies_for_symbol(symbol)
        if not strategies:
            logger.warning(f"No strategies configured for symbol {symbol}")
            return self._session

        # Create runners for each strategy
        for strategy_config in strategies:
            self._init_runner(strategy_config)

        # Create data provider
        if data_provider is not None:
            provider = data_provider
        elif self._db is not None:
            provider = HistoricalDataProvider(
                db=self._db,
                symbol=symbol,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        else:
            raise ValueError("Either db or data_provider must be provided")

        # Log data range
        range_info = provider.get_data_range_info()
        logger.info(
            f"Backtest data range: {range_info.start_ts} to {range_info.end_ts} "
            f"({range_info.total_records} records)"
        )

        # Main backtest loop
        tick_count = 0
        for tick in provider:
            self._process_tick(tick)
            tick_count += 1

            if tick_count % 10000 == 0:
                logger.info(f"Processed {tick_count} ticks...")

        logger.info(f"Backtest complete: {tick_count} ticks processed")

        # Wind down at end
        self._wind_down()

        # Finalize session
        final_unrealized = self._calculate_total_unrealized()
        self._session.finalize(final_unrealized)

        return self._session

    def run_multiple_symbols(
        self,
        symbols: list[str],
        start_ts: datetime,
        end_ts: datetime,
    ) -> dict[str, BacktestSession]:
        """Run backtest for multiple symbols.

        Each symbol runs independently with its own session.

        Args:
            symbols: List of trading symbols.
            start_ts: Start timestamp.
            end_ts: End timestamp.

        Returns:
            Dict mapping symbol to session.
        """
        results: dict[str, BacktestSession] = {}

        for symbol in symbols:
            logger.info(f"Running backtest for {symbol}...")
            session = self.run(symbol, start_ts, end_ts)
            results[symbol] = session

        return results

    def _init_runner(self, strategy_config: BacktestStrategyConfig) -> None:
        """Initialize a runner for a strategy."""
        # Fetch instrument info (from API or cache)
        instrument_info = self._instrument_provider.get(strategy_config.symbol)
        logger.info(
            f"Instrument {strategy_config.symbol}: "
            f"qty_step={instrument_info.qty_step}, tick_size={instrument_info.tick_size}"
        )

        # Create fill simulator and order manager
        fill_simulator = TradeThroughFillSimulator()
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=strategy_config.commission_rate,
        )

        # Create executor with qty calculator (includes rounding)
        qty_calculator = self._create_qty_calculator(strategy_config, instrument_info)
        executor = BacktestExecutor(
            order_manager=order_manager,
            qty_calculator=qty_calculator,
        )

        # Fetch risk limit tiers for margin calculations
        tiers = self._risk_limit_provider.get(strategy_config.symbol)
        logger.info(
            f"Risk limit tiers for {strategy_config.symbol}: "
            f"{len(tiers)} tiers, leverage={strategy_config.leverage}"
        )

        # Create position trackers
        long_tracker = BacktestPositionTracker(
            direction=DirectionType.LONG,
            commission_rate=strategy_config.commission_rate,
            leverage=strategy_config.leverage,
            tiers=tiers,
            symbol=strategy_config.symbol,
        )
        short_tracker = BacktestPositionTracker(
            direction=DirectionType.SHORT,
            commission_rate=strategy_config.commission_rate,
            leverage=strategy_config.leverage,
            tiers=tiers,
            symbol=strategy_config.symbol,
        )

        # Create runner
        runner = BacktestRunner(
            strategy_config=strategy_config,
            executor=executor,
            session=self._session,
            long_tracker=long_tracker,
            short_tracker=short_tracker,
        )

        self._runners[strategy_config.strat_id] = runner
        logger.info(f"Initialized runner for strategy {strategy_config.strat_id}")

    def _create_qty_calculator(
        self, config: BacktestStrategyConfig, instrument_info: InstrumentInfo
    ):
        """Create qty calculator function based on config amount pattern.

        Amount formats:
        - "100" or "100.0": Fixed USDT amount
        - "x0.001": Fraction of wallet balance (0.1%)
        - "b0.001": BTC equivalent amount

        Quantities are rounded up to instrument's qty_step (matching bbu2 behavior).

        Args:
            config: Strategy configuration.
            instrument_info: Instrument info with qty_step for rounding.

        Returns:
            Callable that takes (intent, wallet_balance) and returns qty.
        """
        amount_str = config.amount

        if amount_str.startswith("x"):
            # Wallet fraction
            fraction = Decimal(amount_str[1:])

            def qty_from_fraction(intent, wallet_balance):
                # qty = wallet * fraction / price
                if intent.price <= 0:
                    return Decimal("0")
                raw_qty = wallet_balance * fraction / intent.price
                return instrument_info.round_qty(raw_qty)

            return qty_from_fraction

        elif amount_str.startswith("b"):
            # BTC equivalent (fixed size in base currency)
            base_qty = Decimal(amount_str[1:])

            def qty_fixed_base(intent, wallet_balance):
                return instrument_info.round_qty(base_qty)

            return qty_fixed_base

        else:
            # Fixed USDT amount
            usdt_amount = Decimal(amount_str)

            def qty_from_usdt(intent, wallet_balance):
                if intent.price <= 0:
                    return Decimal("0")
                raw_qty = usdt_amount / intent.price
                return instrument_info.round_qty(raw_qty)

            return qty_from_usdt

    def _process_tick(self, tick) -> None:
        """Process tick for all runners with proper equity timing.

        Order of operations:
        1. Apply funding (if applicable)
        2. Process fills for all runners (updates realized PnL)
        3. Update equity (reflects fills, calculates fresh unrealized)
        4. Execute tick intents for all runners (uses updated balance)
        """
        # Track last price and timestamp for wind-down
        self._last_prices[tick.symbol] = tick.last_price
        self._last_timestamp = tick.exchange_ts

        # 1. Process funding first (if applicable)
        if self._funding_simulator and self._funding_simulator.should_apply_funding(tick.exchange_ts):
            self._apply_funding(tick)
            self._funding_simulator.mark_funding_applied(tick.exchange_ts)

        # 2. Phase 1: Process fills for all runners (updates realized PnL in session)
        for runner in self._runners.values():
            if runner.symbol == tick.symbol:
                runner.process_fills(tick)

        # 3. Update equity AFTER fills (reflects realized PnL from fills)
        # Aggregates unrealized PnL and margin from ALL runners for multi-strategy support
        total_unrealized = self._calculate_unrealized_at_price(tick.symbol, tick.last_price)
        total_im, total_mm = self._calculate_total_margin(tick.symbol)
        self._session.update_equity(tick.exchange_ts, total_unrealized, total_im, total_mm)

        # 4. Phase 2: Execute tick intents for all runners (uses updated balance)
        for runner in self._runners.values():
            if runner.symbol == tick.symbol:
                runner.execute_tick(tick)

    def _apply_funding(self, tick) -> None:
        """Apply funding payment to all runners."""
        rate = self._funding_simulator.rate

        for runner in self._runners.values():
            if runner.symbol == tick.symbol:
                funding = runner.apply_funding(rate, tick.last_price)
                if funding != 0:
                    logger.debug(
                        f"{runner.strat_id}: Funding payment {funding:.4f} "
                        f"(rate={rate}, price={tick.last_price})"
                    )

    def _wind_down(self) -> None:
        """Handle end-of-backtest wind down.

        Depending on config.wind_down_mode:
        - WindDownMode.LEAVE_OPEN: Leave positions open, report unrealized PnL
        - WindDownMode.CLOSE_ALL: Force close all positions at last price
        """
        if self._config.wind_down_mode == WindDownMode.CLOSE_ALL:
            logger.info("Wind down: closing all positions")
            for runner in self._runners.values():
                last_price = self._last_prices.get(runner.symbol)
                if last_price is None:
                    continue

                # Close long position
                if runner.long_tracker.has_position:
                    self._force_close_position(
                        runner=runner,
                        tracker=runner.long_tracker,
                        direction=DirectionType.LONG,
                        close_side=SideType.SELL,
                        price=last_price,
                    )

                # Close short position
                if runner.short_tracker.has_position:
                    self._force_close_position(
                        runner=runner,
                        tracker=runner.short_tracker,
                        direction=DirectionType.SHORT,
                        close_side=SideType.BUY,
                        price=last_price,
                    )

    def _force_close_position(
        self,
        runner: BacktestRunner,
        tracker: BacktestPositionTracker,
        direction: str,
        close_side: str,
        price: Decimal,
    ) -> None:
        """Force close a position at given price.

        Args:
            runner: The runner owning the position.
            tracker: Position tracker to close.
            direction: 'long' or 'short'.
            close_side: 'Buy' or 'Sell' (opposite of position direction).
            price: Price to close at.
        """
        size = tracker.state.size
        if size <= 0:
            return

        logger.info(
            f"{runner.strat_id}: Force closing {direction} position: "
            f"size={size} @ {price}"
        )

        # Process the closing fill
        realized_pnl = tracker.process_fill(
            side=close_side,
            qty=size,
            price=price,
        )

        # Recalculate unrealized PnL (will be 0 since position is closed)
        tracker.calculate_unrealized_pnl(price)

        # Record the trade
        trade = BacktestTrade(
            trade_id=f"close_{uuid.uuid4().hex[:8]}",
            symbol=runner.symbol,
            side=close_side,
            price=price,
            qty=size,
            direction=direction,
            timestamp=self._last_timestamp,
            order_id=f"wind_down_{direction}",
            client_order_id=f"wind_down_{direction}",
            realized_pnl=realized_pnl,
            commission=size * price * tracker.commission_rate,
            strat_id=runner.strat_id,
        )
        self._session.record_trade(trade)

    def _calculate_unrealized_at_price(self, symbol: str, price: Decimal) -> Decimal:
        """Calculate total unrealized PnL for symbol at given price.

        This recalculates unrealized PnL at current price rather than using
        cached state, ensuring accuracy for multi-strategy runs.
        """
        total = Decimal("0")
        for runner in self._runners.values():
            if runner.symbol == symbol:
                total += runner.long_tracker.calculate_unrealized_pnl(price)
                total += runner.short_tracker.calculate_unrealized_pnl(price)
        return total

    def _calculate_total_margin(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Calculate total IM and MM across all runners for a symbol.

        Called after unrealized PnL is calculated (which also updates margin).
        """
        total_im = Decimal("0")
        total_mm = Decimal("0")
        for runner in self._runners.values():
            if runner.symbol == symbol:
                total_im += runner.get_total_im()
                total_mm += runner.get_total_mm()
        return total_im, total_mm

    def _calculate_total_unrealized(self) -> Decimal:
        """Calculate total unrealized PnL across all runners using last prices."""
        total = Decimal("0")
        for runner in self._runners.values():
            price = self._last_prices.get(runner.symbol)
            if price is not None:
                total += runner.long_tracker.calculate_unrealized_pnl(price)
                total += runner.short_tracker.calculate_unrealized_pnl(price)
        return total

    @property
    def session(self) -> Optional[BacktestSession]:
        """Current session (if running)."""
        return self._session

    @property
    def runners(self) -> dict[str, BacktestRunner]:
        """Active runners."""
        return self._runners
