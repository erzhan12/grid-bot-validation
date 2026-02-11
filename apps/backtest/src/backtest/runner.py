"""Backtest runner that wraps GridEngine with simulation context.

The runner is responsible for:
- Managing a GridEngine instance
- Processing ticks (check fills, update grid, execute intents)
- Tracking positions and PnL
"""

import logging
from decimal import Decimal
from typing import Optional

from gridcore import (
    GridEngine,
    GridConfig,
    TickerEvent,
    ExecutionEvent,
    PlaceLimitIntent,
    CancelIntent,
    DirectionType,
    SideType,
)

from backtest.config import BacktestStrategyConfig
from backtest.executor import BacktestExecutor
from backtest.order_manager import BacktestOrderManager
from backtest.position_tracker import BacktestPositionTracker
from backtest.session import BacktestSession, BacktestTrade


logger = logging.getLogger(__name__)


class BacktestRunner:
    """Runs a single strategy in backtest mode.

    Wraps GridEngine and handles:
    - Processing ticks (price data)
    - Checking for order fills
    - Executing intents
    - Tracking positions and PnL

    Example:
        config = BacktestStrategyConfig(...)
        executor = BacktestExecutor(order_manager)

        runner = BacktestRunner(
            strategy_config=config,
            executor=executor,
            session=session,
        )

        for tick in data_provider:
            runner.process_tick(tick)
    """

    def __init__(
        self,
        strategy_config: BacktestStrategyConfig,
        executor: BacktestExecutor,
        session: BacktestSession,
        long_tracker: Optional[BacktestPositionTracker] = None,
        short_tracker: Optional[BacktestPositionTracker] = None,
        anchor_price: Optional[float] = None,
    ):
        """Initialize backtest runner.

        Args:
            strategy_config: Strategy configuration.
            executor: Backtest executor for intent execution.
            session: Session for recording results.
            long_tracker: Position tracker for long direction.
            short_tracker: Position tracker for short direction.
            anchor_price: Optional anchor price for grid initialization.
        """
        self._config = strategy_config
        self._executor = executor
        self._session = session

        # Create GridEngine
        grid_config = GridConfig(
            grid_count=strategy_config.grid_count,
            grid_step=strategy_config.grid_step,
        )
        self._engine = GridEngine(
            symbol=strategy_config.symbol,
            tick_size=strategy_config.tick_size,
            config=grid_config,
            strat_id=strategy_config.strat_id,
            anchor_price=anchor_price,
        )

        # Position trackers (create if not provided)
        self._long_tracker = long_tracker or BacktestPositionTracker(
            direction=DirectionType.LONG,
            commission_rate=strategy_config.commission_rate,
        )
        self._short_tracker = short_tracker or BacktestPositionTracker(
            direction=DirectionType.SHORT,
            commission_rate=strategy_config.commission_rate,
        )

        # Track whether grid has been built
        self._grid_built = False

    @property
    def strat_id(self) -> str:
        """Strategy identifier."""
        return self._config.strat_id

    @property
    def symbol(self) -> str:
        """Trading symbol."""
        return self._config.symbol

    @property
    def engine(self) -> GridEngine:
        """Underlying GridEngine."""
        return self._engine

    @property
    def order_manager(self) -> BacktestOrderManager:
        """Order manager from executor."""
        return self._executor.order_manager

    @property
    def long_tracker(self) -> BacktestPositionTracker:
        """Long position tracker."""
        return self._long_tracker

    @property
    def short_tracker(self) -> BacktestPositionTracker:
        """Short position tracker."""
        return self._short_tracker

    def process_tick(self, event: TickerEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """Process one tick of market data (legacy single-phase method).

        For proper equity timing, use process_fills() + execute_tick() instead.
        This method is kept for backward compatibility with tests.
        """
        fill_intents = self.process_fills(event)
        tick_intents = self.execute_tick(event)
        return fill_intents + tick_intents

    def process_fills(self, event: TickerEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """Phase 1: Check and process order fills.

        This should be called BEFORE equity update so fills are reflected
        in realized PnL before balance is recalculated.

        Args:
            event: Ticker event with current price.

        Returns:
            List of intents generated from fills.
        """
        intents: list[PlaceLimitIntent | CancelIntent] = []

        # Check for fills
        fills = self._executor.order_manager.check_fills(
            current_price=event.last_price,
            timestamp=event.exchange_ts,
            symbol=event.symbol,
        )

        # Process fills
        for fill_event in fills:
            self._process_fill(fill_event)
            # Pass fill to engine (updates grid state)
            fill_intents = self._engine.on_event(fill_event)
            intents.extend(fill_intents)

        return intents

    def execute_tick(self, event: TickerEvent) -> list[PlaceLimitIntent | CancelIntent]:
        """Phase 2: Get intents from engine and execute them.

        This should be called AFTER equity update so intent execution
        uses the latest wallet balance.

        Args:
            event: Ticker event with current price.

        Returns:
            List of intents generated from tick.
        """
        intents: list[PlaceLimitIntent | CancelIntent] = []

        # Get intents from engine for current price
        limit_orders = self._executor.order_manager.get_limit_orders()
        tick_intents = self._engine.on_event(event, limit_orders)
        intents.extend(tick_intents)

        # Mark grid as built after first tick
        if not self._grid_built and len(self._engine.grid.grid) > 0:
            self._grid_built = True

        # Execute intents (wallet_balance now reflects fills from phase 1)
        for intent in intents:
            if isinstance(intent, PlaceLimitIntent):
                self._executor.execute_place(
                    intent,
                    timestamp=event.exchange_ts,
                    wallet_balance=self._session.current_balance,
                )
            elif isinstance(intent, CancelIntent):
                self._executor.execute_cancel(intent, timestamp=event.exchange_ts)

        return intents

    def _process_fill(self, event: ExecutionEvent) -> None:
        """Process a fill event and update positions.

        Args:
            event: Execution event from order fill.
        """
        # Determine direction from the order
        order = self._executor.order_manager.get_order_by_client_id(event.order_link_id)
        direction = order.direction if order else self._infer_direction(event.side)

        # Get appropriate tracker
        tracker = self._long_tracker if direction == DirectionType.LONG else self._short_tracker

        # Process fill and get realized PnL
        realized_pnl = tracker.process_fill(
            side=event.side,
            qty=event.qty,
            price=event.price,
        )

        # Record trade in session
        trade = BacktestTrade(
            trade_id=event.exec_id,
            symbol=event.symbol,
            side=event.side,
            price=event.price,
            qty=event.qty,
            direction=direction,
            timestamp=event.exchange_ts,
            order_id=event.order_id,
            client_order_id=event.order_link_id,
            realized_pnl=realized_pnl,
            commission=event.fee,
            strat_id=self.strat_id,
        )
        self._session.record_trade(trade)

        logger.debug(
            f"{self.strat_id}: Fill {event.side} {event.qty} @ {event.price}, "
            f"realized_pnl={realized_pnl:.2f}, direction={direction}"
        )

    def _infer_direction(self, side: str) -> str:
        """Infer direction from side when order info not available.

        This is a fallback - normally we get direction from the order.

        Buy side = opening long or closing short
        Sell side = opening short or closing long

        Without more context, we guess based on current positions.
        """
        # If we have a long position and selling, probably closing long
        if side == SideType.SELL and self._long_tracker.has_position:
            return DirectionType.LONG
        # If we have a short position and buying, probably closing short
        if side == SideType.BUY and self._short_tracker.has_position:
            return DirectionType.SHORT
        # Otherwise, opening new position
        return DirectionType.LONG if side == SideType.BUY else DirectionType.SHORT

    def _calculate_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Calculate total unrealized PnL across both directions."""
        long_pnl = self._long_tracker.calculate_unrealized_pnl(current_price)
        short_pnl = self._short_tracker.calculate_unrealized_pnl(current_price)
        return long_pnl + short_pnl

    def apply_funding(self, rate: Decimal, current_price: Decimal) -> Decimal:
        """Apply funding payment to positions.

        Args:
            rate: Funding rate.
            current_price: Current price for notional calculation.

        Returns:
            Total funding payment (negative = paid, positive = received).
        """
        long_funding = self._long_tracker.apply_funding(rate, current_price)
        short_funding = self._short_tracker.apply_funding(rate, current_price)
        total_funding = long_funding + short_funding

        self._session.record_funding(total_funding)
        return total_funding

    def get_total_pnl(self) -> Decimal:
        """Get combined PnL from both directions."""
        return self._long_tracker.get_total_pnl() + self._short_tracker.get_total_pnl()
