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
    Position,
    PositionState,
    RiskConfig,
)
from gridcore.pnl import calc_position_value

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

        # Risk multiplier support
        self._leverage = strategy_config.leverage
        self._mmr = strategy_config.maintenance_margin_rate
        self._enable_risk = strategy_config.enable_risk_multipliers

        if self._enable_risk:
            risk_config = RiskConfig(
                min_liq_ratio=strategy_config.min_liq_ratio,
                max_liq_ratio=strategy_config.max_liq_ratio,
                max_margin=strategy_config.max_margin,
                min_total_margin=strategy_config.min_total_margin,
            )
            self._long_position, self._short_position = Position.create_linked_pair(risk_config)

            # Compose risk multiplier with existing qty_calculator so that
            # base qty (from amount pattern + rounding) is computed first,
            # then scaled by the risk multiplier.
            self._base_qty_calculator = self._executor.qty_calculator
            self._executor.qty_calculator = self._apply_risk_to_qty
        else:
            self._long_position = None
            self._short_position = None

        # Last price seen (needed for multiplier recalculation)
        self._last_price: Optional[Decimal] = None

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
        self._last_price = event.last_price
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

        # Recalculate risk multipliers after position change.
        # Use ticker last_price (not fill price) — fill price is the order
        # limit price, but liq_ratio checks need the current market price.
        if self._enable_risk and self._last_price is not None:
            self._update_risk_multipliers(float(self._last_price))

    def _build_position_state(
        self,
        tracker: BacktestPositionTracker,
        wallet_balance: Decimal,
        direction: str,
    ) -> PositionState:
        """Build gridcore PositionState from backtest tracker state.

        Args:
            tracker: Position tracker with current size/entry.
            wallet_balance: Current wallet balance for margin ratio.
            direction: 'long' or 'short'.

        Returns:
            PositionState for risk calculation.
        """
        size = tracker.state.size
        entry_price = tracker.state.avg_entry_price

        if size > 0 and entry_price > 0:
            position_value = calc_position_value(size, entry_price)
            margin = position_value / wallet_balance if wallet_balance > 0 else Decimal("0")
            liq_price = self._estimate_liquidation_price(entry_price, direction)
        else:
            position_value = Decimal("0")
            margin = Decimal("0")
            liq_price = Decimal("0")

        return PositionState(
            direction=direction,
            size=size,
            entry_price=entry_price if entry_price > 0 else None,
            margin=margin,
            liquidation_price=liq_price,
            leverage=self._leverage,
            position_value=position_value,
        )

    def _estimate_liquidation_price(self, entry_price: Decimal, direction: str) -> Decimal:
        """Estimate liquidation price from entry price, leverage, and MMR.

        Simplified formula for linear USDT perpetuals (isolated margin):
        - Long:  liq = entry * (1 - 1/leverage + mmr)
        - Short: liq = entry * (1 + 1/leverage - mmr)
        """
        inv_leverage = Decimal(1) / Decimal(self._leverage)
        mmr = Decimal(str(self._mmr))

        if direction == DirectionType.LONG:
            return entry_price * (1 - inv_leverage + mmr)
        else:
            return entry_price * (1 + inv_leverage - mmr)

    def _update_risk_multipliers(self, last_price: float) -> None:
        """Recalculate risk multipliers from current position state.

        Mirrors live bot pattern: reset both, calculate long first, then short.
        """
        wallet_balance = self._session.current_balance

        long_state = self._build_position_state(
            self._long_tracker, wallet_balance, DirectionType.LONG
        )
        short_state = self._build_position_state(
            self._short_tracker, wallet_balance, DirectionType.SHORT
        )

        # Reset then calculate (bbu2 pattern — cross-position effects preserved)
        self._long_position.reset_amount_multiplier()
        self._short_position.reset_amount_multiplier()

        if long_state.size > 0:
            self._long_position.calculate_amount_multiplier(
                long_state, short_state, last_price
            )

        if short_state.size > 0:
            self._short_position.calculate_amount_multiplier(
                short_state, long_state, last_price
            )

        long_mult = self._long_position.get_amount_multiplier()
        short_mult = self._short_position.get_amount_multiplier()
        logger.debug(
            "%s: Risk update - long_mult=Buy:%.2f/Sell:%.2f, "
            "short_mult=Buy:%.2f/Sell:%.2f",
            self.strat_id,
            long_mult['Buy'], long_mult['Sell'],
            short_mult['Buy'], short_mult['Sell'],
        )

    def get_amount_multiplier(self, direction: str, side: str) -> float:
        """Get current risk multiplier for a direction and side.

        Args:
            direction: 'long' or 'short'.
            side: 'Buy' or 'Sell'.

        Returns:
            Multiplier value (1.0 if risk disabled).
        """
        if not self._enable_risk:
            return 1.0
        if direction == DirectionType.LONG:
            return self._long_position.get_amount_multiplier()[side]
        else:
            return self._short_position.get_amount_multiplier()[side]

    def _apply_risk_to_qty(self, intent: PlaceLimitIntent, wallet_balance: Decimal) -> Decimal:
        """qty_calculator callback for BacktestExecutor.

        Composes with the base qty_calculator: first computes base qty from
        amount pattern + rounding, then scales by the risk multiplier.
        """
        # Compute base qty using the original calculator (amount/rounding)
        if self._base_qty_calculator is not None:
            base_qty = self._base_qty_calculator(intent, wallet_balance)
        else:
            base_qty = intent.qty

        multiplier = self.get_amount_multiplier(intent.direction, intent.side)
        return base_qty * Decimal(str(multiplier))

    def _infer_direction(self, side: str) -> str:
        """Infer direction from side when order lookup fails.

        This fallback should NOT trigger in normal operation — every fill
        in backtest comes from an order placed by BacktestExecutor, so
        get_order_by_client_id() should always find it. If this runs,
        it indicates an order tracking gap (e.g., mismatched client_order_id).

        Heuristic based on current position state:
        - Selling while holding long → closing long
        - Buying while holding short → closing short
        - Otherwise → opening new position (Buy=long, Sell=short)
        """
        logger.warning(
            "%s: Direction inference fallback used for side=%s "
            "(order not found — possible order tracking gap)",
            self.strat_id, side,
        )
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
