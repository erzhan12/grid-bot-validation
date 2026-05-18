"""Backtest runner that wraps GridEngine with simulation context.

The runner is responsible for:
- Managing a GridEngine instance
- Processing ticks (check fills, update grid, execute intents)
- Tracking positions and PnL
"""

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

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
    apply_early_imbalance,
)
from gridcore.instrument_info import InstrumentInfo
from gridcore.pnl import (
    calc_position_value,
    calc_margin_ratio,
    calc_maintenance_margin,
    calc_unrealised_pnl,
    MMTiers,
    MM_TIERS,
    MM_TIERS_DEFAULT,
    _find_matching_tier,
)
from grid_db.models import PositionSnapshot

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

    # 0043: short-only safe-cap multiplier. When the raw formula output
    # exceeds `_SHORT_ONLY_LIQ_CAP_MULTIPLIER × S_entry`, the helper returns
    # 0 instead. Defensive — see TODO(0043, Phase 4) in
    # `_estimate_pair_liq_prices` and the matching Open Decisions entry
    # in docs/features/0043_PLAN.md.
    _SHORT_ONLY_LIQ_CAP_MULTIPLIER: Decimal = Decimal("2")

    def __init__(
        self,
        strategy_config: BacktestStrategyConfig,
        executor: BacktestExecutor,
        session: BacktestSession,
        long_tracker: Optional[BacktestPositionTracker] = None,
        short_tracker: Optional[BacktestPositionTracker] = None,
        anchor_price: Optional[float] = None,
        instrument_info: Optional[InstrumentInfo] = None,
        restored_grid: Optional[list[dict]] = None,
        seeded_active_orders: Optional[list] = None,
    ):
        """Initialize backtest runner.

        Args:
            strategy_config: Strategy configuration.
            executor: Backtest executor for intent execution.
            session: Session for recording results.
            long_tracker: Position tracker for long direction.
            short_tracker: Position tracker for short direction.
            anchor_price: Optional anchor price for grid initialization.
                Ignored when ``restored_grid`` is provided (matches live).
            instrument_info: Optional instrument info for qty re-rounding
                after risk multiplier application.
            restored_grid: Optional serialized grid (list of {side, price})
                passed through to ``GridEngine.__init__(restored_grid=...)``.
                Used by replay (feature 0029) to seed the grid from the
                shared ``GridStateStore`` JSON file. When provided,
                ``anchor_price`` is ignored — mirrors live runner's
                ``_load_grid_state()`` → ``GridEngine`` flow.
            seeded_active_orders: Optional list of ``ActiveOrderSeed``
                objects (replay feature 0029). When non-empty, the order
                manager is pre-loaded with these as active orders via
                ``BacktestOrderManager.seed_active_orders``. The orders
                participate in fill checks on the very first tick.
        """
        self._config = strategy_config
        self._executor = executor
        self._session = session
        self._instrument_info = instrument_info

        # Create GridEngine. When restored_grid is provided, anchor_price is
        # ignored (matches live: a restored grid has its own structure and
        # WAIT center; anchor is only used as the fresh-build origin).
        grid_config = GridConfig(
            grid_count=strategy_config.grid_count,
            grid_step=strategy_config.grid_step,
        )
        self._engine = GridEngine(
            symbol=strategy_config.symbol,
            tick_size=strategy_config.tick_size,
            config=grid_config,
            strat_id=strategy_config.strat_id,
            anchor_price=anchor_price if restored_grid is None else None,
            restored_grid=restored_grid,
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
        self._mm_tiers: Optional[MMTiers] = self._load_mm_tiers(
            strategy_config.symbol, strategy_config.risk_limits_cache_path
        )
        # Feature 0045 — hedge-aware IM/MM helper inputs.
        self._taker_fee_rate: Decimal = strategy_config.taker_fee_rate
        self._hedge_smaller_buffer_factor: Decimal = (
            strategy_config.hedge_smaller_buffer_factor
        )

        if self._enable_risk:
            risk_config = RiskConfig(
                min_liq_ratio=strategy_config.min_liq_ratio,
                max_liq_ratio=strategy_config.max_liq_ratio,
                max_margin=strategy_config.max_margin,
                min_total_margin=strategy_config.min_total_margin,
            )
            self._long_position, self._short_position = Position.create_linked_pair(risk_config)

            # 0029 seeding: when trackers were pre-seeded (via seed_state)
            # before the runner was constructed, copy size + liquidation_price
            # into the gridcore.Position instances as well. Without this, the
            # first tick's early_imbalance gate (D-3 from feature 0028) and
            # _apply_*_position_rules margin/liq branches see Position.size=0
            # and liquidation_price=0 — i.e., the seeded snapshot is invisible
            # until the first fill triggers _update_risk_multipliers.
            self._copy_seeded_state_to_positions()

            # Compose risk multiplier with existing qty_calculator so that
            # base qty (from amount pattern + rounding) is computed first,
            # then scaled by the risk multiplier.
            self._base_qty_calculator = self._executor.qty_calculator
            self._executor.qty_calculator = self._apply_risk_to_qty
        else:
            self._long_position = None
            self._short_position = None

        # 0029 seeding: register active orders pre-existing on the live exchange
        # at seed.at_ts. Done after Grid setup so the order manager is fully
        # initialised; subsequent process_tick calls run fill_simulator over
        # these orders alongside any newly-placed ones.
        if seeded_active_orders:
            self._executor.order_manager.seed_active_orders(seeded_active_orders)

        # Last price seen (needed for multiplier recalculation)
        self._last_price: Optional[Decimal] = None

        # 0034: track Bybit-style mark separately so the emitted
        # PositionSnapshot.mark_price is semantically the mark price (matches
        # the recorder), not last_price. Falls back to event.price at the
        # emission site when no ticker mark is available.
        self._last_mark_price: Optional[Decimal] = None

        # Track whether grid has been built
        self._grid_built = False

        # 0034: position telemetry parity emission hook. Replay engine wires
        # a synchronous writer here. Standalone backtest leaves it None and
        # emits nothing — keeps existing call sites unchanged.
        self.position_snapshot_callback: Optional[
            Callable[[PositionSnapshot], None]
        ] = None

    def _copy_seeded_state_to_positions(self) -> None:
        """Mirror seeded tracker state into gridcore.Position instances.

        Called only when ``enable_risk_multipliers=True``. Detects a seeded
        tracker by a non-zero ``state.size`` OR non-zero
        ``state.liquidation_price`` (a fresh tracker has both at zero).
        Without this copy, the very first tick's early_imbalance gate
        (feature 0028 D-3) and ``_apply_*_position_rules`` margin/liq
        branches read ``Position.size=0`` / ``liquidation_price=0`` and
        the seeded snapshot has no effect until the first fill triggers
        ``_update_risk_multipliers``.

        Uses the same liq_price the tracker holds, so the gridcore side
        sees Bybit's snapshot value until the next risk update overwrites
        with ``_estimate_liquidation_price`` (see feature 0029 plan,
        "Edge cases — liquidation_price divergence after first risk update").
        """
        long_state = self._long_tracker.state
        if long_state.size != 0 or long_state.liquidation_price != 0:
            self._long_position.size = long_state.size
            self._long_position.liquidation_price = long_state.liquidation_price

        short_state = self._short_tracker.state
        if short_state.size != 0 or short_state.liquidation_price != 0:
            self._short_position.size = short_state.size
            self._short_position.liquidation_price = short_state.liquidation_price

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

    # Default cache location: project_root/conf/risk_limits_cache.json
    # __file__ = apps/backtest/src/backtest/runner.py → 5x parent = project root
    _DEFAULT_CACHE_PATH = Path(__file__).parent.parent.parent.parent.parent / "conf" / "risk_limits_cache.json"

    @staticmethod
    def _load_mm_tiers(symbol: str, cache_path: Optional[str]) -> Optional[MMTiers]:
        """Load tiered MMR from cache file, falling back to hardcoded defaults.

        Resolution order:
        1. Explicit ``cache_path`` if provided.
        2. Auto-discover ``conf/risk_limits_cache.json`` relative to project root.
        3. Hardcoded tier tables in ``gridcore.pnl``.

        Args:
            symbol: Trading pair (e.g. "BTCUSDT").
            cache_path: Path to risk_limits_cache.json, or None for auto-discover.

        Returns:
            MMTiers list for the symbol, or None if nothing available.
        """
        import json

        # Resolve cache path: explicit > auto-discover
        paths_to_try = []
        if cache_path is not None:
            paths_to_try.append(Path(cache_path))
        else:
            paths_to_try.append(BacktestRunner._DEFAULT_CACHE_PATH)

        for path in paths_to_try:
            if not path.exists():
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                symbol_data = data.get(symbol)
                if symbol_data and "tiers" in symbol_data:
                    raw_tiers = symbol_data["tiers"]
                    converted: MMTiers = []
                    for t in raw_tiers:
                        max_val = Decimal(t["max_value"]) if t["max_value"] != "Infinity" else Decimal("Infinity")
                        mmr_rate = Decimal(t["mmr_rate"])
                        deduction = Decimal(t["deduction"])
                        imr_rate = Decimal(t.get("imr_rate", "0"))
                        converted.append((max_val, mmr_rate, deduction, imr_rate))
                    logger.info(
                        "%s: loaded %d MMR tiers from %s",
                        symbol, len(converted), path,
                    )
                    return converted
                else:
                    logger.info(
                        "%s: symbol not found in cache %s, trying next source",
                        symbol, path,
                    )
            except (json.JSONDecodeError, KeyError, ValueError, ArithmeticError, TypeError) as exc:
                logger.warning(
                    "Failed to load risk limits from %s: %s, using hardcoded defaults",
                    path, exc,
                )

        # Fall back to hardcoded tier tables
        tiers = MM_TIERS.get(symbol, MM_TIERS_DEFAULT)
        logger.info("%s: using hardcoded MMR tiers (%d tiers)", symbol, len(tiers))
        return tiers

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
        # 0034: cache the ticker mark so backtest snapshots store the mark
        # in PositionSnapshot.mark_price (matches recorder semantics).
        self._last_mark_price = event.mark_price
        intents: list[PlaceLimitIntent | CancelIntent] = []

        # Check for fills
        fills = self._executor.order_manager.check_fills(
            market=event,
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
                # Gate close orders: skip if no position to close or position
                # is already fully covered by pending close orders.
                # Matches BBU2 _is_good_to_place() pattern.
                if intent.reduce_only and not self._should_place_close(intent):
                    continue
                result = self._executor.execute_place(
                    intent,
                    timestamp=event.exchange_ts,
                    wallet_balance=self._session.current_balance,
                )
                logger.debug(
                    "%s: Place %s %s @ %s, direction=%s, reduce_only=%s, placed=%s",
                    self.strat_id, intent.side, intent.qty, intent.price,
                    intent.direction, intent.reduce_only, result.success,
                )
            elif isinstance(intent, CancelIntent):
                self._executor.execute_cancel(intent, timestamp=event.exchange_ts)
                logger.debug(
                    "%s: Cancel order %s, reason=%s",
                    self.strat_id, intent.order_id, intent.reason,
                )

        return intents

    def _should_place_close(self, intent: PlaceLimitIntent) -> bool:
        """Check whether a reduce_only (close) order should be placed.

        Returns False when:
        - No position exists for this direction (nothing to close).
        - The position size is already fully covered by pending close orders
          plus the new order's resolved qty.

        Resolves intent qty via the executor's qty_calculator before
        checking, matching live runner _is_good_to_place() pattern.
        """
        tracker = (
            self._long_tracker
            if intent.direction == DirectionType.LONG
            else self._short_tracker
        )
        pos_size = tracker.state.size
        if pos_size == 0:
            return False

        pending_qty = self._get_pending_close_qty(intent.direction)
        if pending_qty > pos_size:
            close_orders = [
                f"{o.order_id}: {o.qty}"
                for o in self._executor.order_manager.active_orders.values()
                if o.direction == intent.direction and o.reduce_only
            ]
            logger.warning(
                "%s: Over-hedged %s close orders: pending_qty=%s > pos_size=%s "
                "(possible logic error in order tracking). Active close orders: [%s]",
                self.strat_id, intent.direction, pending_qty, pos_size,
                ", ".join(close_orders),
            )
        intent_qty = self._resolve_intent_qty(intent)
        return pos_size > (pending_qty + intent_qty)

    def _get_pending_close_qty(self, direction: str) -> Decimal:
        """Sum qty of active reduce_only orders for a direction.

        O(n) scan over active_orders — acceptable because order count is
        bounded by grid_count (typically 50-100).
        """
        if not self._executor.order_manager.active_orders:
            return Decimal("0")
        total = Decimal("0")
        for order in self._executor.order_manager.active_orders.values():
            if order.direction == direction and order.reduce_only:
                total += order.qty
        return total

    def _resolve_intent_qty(self, intent: PlaceLimitIntent) -> Decimal:
        """Resolve qty that executor.execute_place would compute for an intent.

        Engine emits qty=0; the executor's qty_calculator resolves it.
        This mirrors that resolution so _should_place_close can include
        the new order's qty in its gate check.
        """
        if self._executor.qty_calculator is not None:
            return self._executor.qty_calculator(intent, self._session.current_balance)
        return intent.qty

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

        # Refresh margin fields so the log below shows current IM/MM
        tracker._update_margin()

        # 0043: refresh session balance/equity from post-fill tracker state.
        # The engine's per-tick `session.update_equity` runs AFTER this
        # function returns; without this refresh, the pair-liq formula,
        # the emitted parity snapshot, and risk multipliers would all
        # consume last-tick balance values against post-fill positions.
        mark_for_unrealized = (
            self._last_mark_price
            if self._last_mark_price is not None
            else event.price
        )
        fresh_unrealized = (
            self._long_tracker.calculate_unrealized_pnl(mark_for_unrealized)
            + self._short_tracker.calculate_unrealized_pnl(mark_for_unrealized)
        )
        self._session.refresh_balances(fresh_unrealized)

        # 0043 perf: compute the pair liq ONCE for this fill and pass the
        # cached pair down to the three downstream consumers (log,
        # _update_risk_multipliers, _emit_position_snapshot) instead of
        # recomputing in each.
        liq_long, liq_short = self._estimate_pair_liq_prices(
            self._long_tracker.state,
            self._short_tracker.state,
            self._session.total_equity,
        )

        # Compute liq_ratio for logging (liq_price / last_price).
        liq_ratio = 0.0
        if (
            self._enable_risk
            and self._last_price is not None
            and tracker.state.size > 0
        ):
            liq_price = liq_long if direction == DirectionType.LONG else liq_short
            liq_ratio = float(liq_price / self._last_price) if self._last_price else 0.0

        margin = (
            float(tracker.state.position_value / self._session.current_balance)
            if self._session.current_balance > 0
            else 0.0
        )
        logger.debug(
            f"{self.strat_id}: Fill {event.side} {event.qty} @ {event.price}, "
            f"realized_pnl={realized_pnl:.2f}, direction={direction}, "
            f"pos_size={tracker.state.size}, "
            f"liq_ratio={liq_ratio:.4f}, margin={margin:.4f}, "
            f"imr={tracker.state.imr_rate:.4f}, mmr={tracker.state.mmr_rate:.4f}"
        )

        # Recalculate risk multipliers after position change.
        # Use ticker last_price (not fill price) — fill price is the order
        # limit price, but liq_ratio checks need the current market price.
        if (
            self._enable_risk
            and self._last_price is not None
            and self._long_position is not None
            and self._short_position is not None
        ):
            self._update_risk_multipliers(
                float(self._last_price),
                liq_long=liq_long,
                liq_short=liq_short,
            )

        # 0034: emit a parity-checkable position snapshot for the just-mutated
        # direction. Done AFTER _update_risk_multipliers so any multiplier-
        # driven liq_price update is reflected in the same row.
        # Use the ticker mark (matches Bybit/recorder semantics for the
        # mark_price column), not last_price. Fall back to event.price only
        # when no ticker mark has been seen yet.
        if self.position_snapshot_callback is not None:
            mark_price = (
                self._last_mark_price
                if self._last_mark_price is not None
                else event.price
            )
            snap = self._emit_position_snapshot(
                direction, event.exchange_ts, mark_price,
                liq_long=liq_long, liq_short=liq_short,
            )
            self.position_snapshot_callback(snap)

    def _emit_position_snapshot(
        self,
        direction: str,
        timestamp: datetime,
        mark_price: Decimal,
        liq_long: Optional[Decimal] = None,
        liq_short: Optional[Decimal] = None,
    ) -> PositionSnapshot:
        """Build a parity-checkable position snapshot for the just-mutated direction.

        ``direction`` (LONG/SHORT) — NOT fill side. In hedge mode a close-long
        fill arrives as Sell but the snapshot row stays side='Buy'. Passing
        fill side here would flip the snapshot's side column on every close
        fill and break pairing.

        Margin amounts come from ``calc_initial_margin`` / ``calc_maintenance_margin``
        (both return tuples — first element is the amount). Unrealized PnL is
        computed via ``calc_unrealised_pnl`` so the short-side sign is correct
        (the naive ``(mark-entry)*size`` formula is wrong for shorts).

        ``run_id`` / ``account_id`` are NOT set here — the caller (writer)
        owns those fields per the run context.

        ``liq_long`` / ``liq_short`` (0043 perf): when both are provided the
        pair-liq compute is skipped — `_process_fill` precomputes the pair
        once per fill and passes the cached pair down. Other callers
        (e.g. ``engine._wind_down``) pass ``None`` and the helper computes
        inline.
        """
        tracker = (
            self._long_tracker
            if direction == DirectionType.LONG
            else self._short_tracker
        )
        snap_side = "Buy" if direction == DirectionType.LONG else "Sell"

        size = tracker.state.size
        entry_price = tracker.state.avg_entry_price

        if size > 0 and entry_price > 0:
            unrealised = calc_unrealised_pnl(direction, entry_price, mark_price, size)
            # 0045: hedge-aware IM/MM helper replaces per-leg
            # calc_initial_margin / calc_maintenance_margin on the
            # snapshot path. Single consumer per emit (one snapshot per
            # call), so inline call — no precompute / kwargs threading.
            im_long, mm_long, im_short, mm_short = self._estimate_pair_im_mm(
                self._long_tracker.state, self._short_tracker.state, mark_price,
            )
            if direction == DirectionType.LONG:
                position_im, position_mm = im_long, mm_long
            else:
                position_im, position_mm = im_short, mm_short
            # 0043: prefer caller-provided pair; fall back to compute.
            if liq_long is None or liq_short is None:
                liq_long, liq_short = self._estimate_pair_liq_prices(
                    self._long_tracker.state,
                    self._short_tracker.state,
                    self._session.total_equity,
                )
            liq_price = liq_long if direction == DirectionType.LONG else liq_short
        else:
            unrealised = Decimal("0")
            position_im = Decimal("0")
            position_mm = Decimal("0")
            liq_price = Decimal("0")

        return PositionSnapshot(
            # run_id / account_id / source filled by the writer.
            symbol=self.symbol,
            exchange_ts=timestamp,
            local_ts=timestamp,
            side=snap_side,
            size=size,
            entry_price=entry_price,
            liq_price=liq_price,
            unrealised_pnl=unrealised,
            mark_price=mark_price,
            position_im=position_im,
            position_mm=position_mm,
            cum_realised_pnl=tracker.state.cum_realised_pnl,
        )

    def _build_position_state(
        self,
        tracker: BacktestPositionTracker,
        wallet_balance: Decimal,
        direction: str,
        liq_price: Decimal,
    ) -> PositionState:
        """Build gridcore PositionState from backtest tracker state.

        Args:
            tracker: Position tracker with current size/entry.
            wallet_balance: Current wallet balance for margin ratio.
            direction: 'long' or 'short'.
            liq_price: Liquidation price for this leg, already computed
                by the caller via the pair-aware ``_estimate_pair_liq_prices``
                (feature 0043). Centralising the call keeps the codebase
                on a single liq formula and avoids recomputing the pair
                twice within one ``_update_risk_multipliers`` pass.

        Returns:
            PositionState for risk calculation.
        """
        size = tracker.state.size
        entry_price = tracker.state.avg_entry_price

        if size > 0 and entry_price > 0:
            position_value = calc_position_value(size, entry_price)
            if wallet_balance > 0:
                margin = calc_margin_ratio(position_value, wallet_balance)
            else:
                logger.error(
                    "%s: wallet_balance is zero but %s position has value %s "
                    "— critical state inconsistency",
                    self.strat_id, direction, position_value,
                )
                raise ValueError(
                    f"wallet_balance is zero with {direction} position value {position_value}"
                )
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

    def _estimate_pair_im_mm(
        self,
        long_state,
        short_state,
        mark_price: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Hedge-aware pair ``positionIM`` / ``positionMM`` per leg.

        Returns ``(im_long, mm_long, im_short, mm_short)``. Mirrors the
        shape of ``_estimate_pair_liq_prices``: one input pair → both
        legs' values, in the order long-first, short-second.

        Derived from Bybit help-center docs ("Initial Margin USDT
        Contract", "Maintenance Margin USDT Contract") plus empirical
        validation against 10 paired live snapshots in feature 0045
        Phase 1 (max ``|Δ|`` 0.004 USDT, well below the plan's 0.1 USDT
        acceptance threshold for MM).

        Three non-obvious choices:

        1. **`positionIM` and `positionMM` include the fee-to-close.**
           Bybit's API returns these fields with the taker fee baked in
           (per the linked help articles). The pre-0045 backtest path
           used ``calc_initial_margin`` / ``calc_maintenance_margin``
           which omit that fee — that omission was a ~0.23 USDT
           single-leg gap. The new helper bakes the fee in so single-leg
           values also align with live.
        2. **Dominant-leg MM uses ONLY the unhedged portion at full
           tier MMR, minus the tier deduction.** The hedged portion of
           the dominant leg's notional contributes zero to that leg's
           published MM in hedge mode: Bybit cross-credits the hedged
           size to the smaller leg. The ``deduction`` term carries
           through (looked up on the leg's own full pv, matching
           Bybit's per-leg ``riskLimitValue``) so the per-tier MM
           formula stays continuous at tier boundaries.
           ``L_MM = max((L − S) × mark × MMR_tier − deduction_tier, 0)
                  + fee_to_close_long``.
        3. **Smaller leg has no per-MMR-on-PV term; only a hedge
           buffer + fee.** When fully hedged (``S ≤ L`` for short-
           smaller) Bybit publishes ``smaller_IM == smaller_MM ==
           fee_to_close_smaller + hedged_size × |L_entry − S_entry| ×
           MMR × C``, where ``C`` is an empirical Bybit-internal hedge
           factor (≈ 5.657 for LTCUSDT @ 10x). Single-leg case
           (opposite leg zero) collapses naturally — there is no
           "smaller" leg to apply the buffer to.

        Args:
            long_state: Tracker state with ``size`` and ``avg_entry_price``.
            short_state: Tracker state with ``size`` and ``avg_entry_price``.
            mark_price: Current mark price (Bybit hedge formulas are
                mark-based since 2025-09-02).

        Returns:
            ``(im_long, mm_long, im_short, mm_short)`` as Decimals. Zero
            entries on legs with ``size == 0``.
        """
        L_size = long_state.size
        S_size = short_state.size
        L_entry = long_state.avg_entry_price
        S_entry = short_state.avg_entry_price

        zero = Decimal("0")
        if L_size <= zero and S_size <= zero:
            return zero, zero, zero, zero

        lev = Decimal(str(self._leverage))
        inv_lev = Decimal("1") / lev
        taker = self._taker_fee_rate
        hedge_C = self._hedge_smaller_buffer_factor

        # Fee-to-close — formulas per Bybit docs (use avg entry price).
        L_fee = L_size * L_entry * (Decimal("1") - inv_lev) * taker if L_size > zero else zero
        S_fee = S_size * S_entry * (Decimal("1") + inv_lev) * taker if S_size > zero else zero

        # Tier looked up on each leg's own mark-PV (Bybit assigns
        # ``riskLimitValue`` per-leg on the full leg notional). Each
        # tier contributes a ``(mmr_rate, deduction)`` pair; Bybit's
        # documented MM formula is ``pv × mmr − deduction``, kept
        # continuous at tier boundaries by the deduction. We re-apply
        # that exact shape against the unhedged portion of the
        # dominant leg.
        L_pv_mark = L_size * mark_price
        S_pv_mark = S_size * mark_price
        mmr_long, deduction_long = self._tier_mmr_and_deduction(L_pv_mark)
        mmr_short, deduction_short = self._tier_mmr_and_deduction(S_pv_mark)

        unhedged_long = max(L_size - S_size, zero)
        unhedged_short = max(S_size - L_size, zero)
        hedged_size = min(L_size, S_size) if (L_size > zero and S_size > zero) else zero
        entry_diff = abs(L_entry - S_entry) if hedged_size > zero else zero

        if L_size >= S_size:
            # Long-dominant (or equal) regime: long is the heavier leg.
            im_long = L_pv_mark / lev + L_fee if L_size > zero else zero
            if L_size > zero:
                mm_long_base = max(
                    unhedged_long * mark_price * mmr_long - deduction_long, zero,
                )
                mm_long = mm_long_base + L_fee
            else:
                mm_long = zero
            if S_size > zero:
                # Smaller leg uses the dominant leg's tier MMR for the
                # hedged-buffer term (Bybit applies a single MMR per
                # paired position — the smaller leg sees the dominant
                # tier, not its own).
                buffer_short = mmr_long * hedged_size * entry_diff * hedge_C
                im_short = mm_short = S_fee + buffer_short
            else:
                im_short = mm_short = zero
            return im_long, mm_long, im_short, mm_short

        # Short-dominant regime (symmetric).
        im_short = S_pv_mark / lev + S_fee
        mm_short_base = max(
            unhedged_short * mark_price * mmr_short - deduction_short, zero,
        )
        mm_short = mm_short_base + S_fee
        if L_size > zero:
            buffer_long = mmr_short * hedged_size * entry_diff * hedge_C
            im_long = mm_long = L_fee + buffer_long
        else:
            im_long = mm_long = zero
        return im_long, mm_long, im_short, mm_short

    def _tier_mmr_and_deduction(self, pv: Decimal) -> tuple[Decimal, Decimal]:
        """Return ``(mmr_rate, deduction)`` for ``pv`` from the loaded tier table.

        Mirrors the per-tier lookup ``calc_maintenance_margin`` does
        internally, but exposes ``deduction`` separately so the 0045
        helper can apply ``unhedged_pv × mmr − deduction`` while still
        using the leg's full-pv tier. Returns ``(0, 0)`` when no tier
        matches or when no tier table is loaded.
        """
        zero = Decimal("0")
        if pv <= zero or self._mm_tiers is None:
            return zero, zero
        tier = _find_matching_tier(pv, self._mm_tiers)
        if tier is None:
            return zero, zero
        _max_val, mmr, deduction, _imr = tier
        return mmr, deduction

    def _estimate_pair_liq_prices(
        self,
        long_state,
        short_state,
        total_equity: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Hedge-aware pair liquidation prices for (long, short) legs.

        Implements the formula derived in feature 0043 against 13 paired
        mainnet snapshots (max |Δ| 0.60 USDT vs live ``liqPrice``); see
        ``docs/features/0043_PLAN.md`` Phase 2 for the validation table.

        Three non-obvious choices:

        1. Pair-shaped: a single input pair yields both legs' liq.
        2. ``total_equity`` is the pool input, not ``totalAvailableBalance``
           (the latter under-shoots by 30-45 USDT).
        3. ``mm_total = calc_maintenance_margin(L_pv + S_pv, …)`` — full
           tier-MMR on combined notional, NOT the sum of per-leg
           ``positionMM`` (Bybit publishes the smaller leg's MM with a
           hedge discount but reverts to full MMR for liq calc).

        Args:
            long_state: Tracker state with ``size`` and ``avg_entry_price``.
            short_state: Tracker state with ``size`` and ``avg_entry_price``.
            total_equity: UTA ``totalEquity`` baseline (0042 wallet field).

        Returns:
            ``(liq_long, liq_short)``. The over-hedged leg returns ``0`` by
            construction (Bybit reports ``NULL`` in that case). The
            dominant leg's negative result is clamped to ``0`` (covered by
            equity, no real liq risk). For fully-hedged or zero positions
            both legs return ``0``.
        """
        L_size = long_state.size
        L_entry = long_state.avg_entry_price
        S_size = short_state.size
        S_entry = short_state.avg_entry_price

        if L_size <= 0 and S_size <= 0:
            return Decimal("0"), Decimal("0")

        L_pv = L_size * L_entry if L_size > 0 else Decimal("0")
        S_pv = S_size * S_entry if S_size > 0 else Decimal("0")
        combined_pv = L_pv + S_pv

        if self._mm_tiers is not None and combined_pv > 0:
            mm_total, _ = calc_maintenance_margin(
                combined_pv, self.symbol, tiers=self._mm_tiers
            )
        else:
            mm_total = combined_pv * Decimal(str(self._mmr))

        q_net = L_size - S_size
        pool = total_equity - mm_total

        # Underwater account guard: total_equity has dropped below the
        # combined MM requirement. The position is already past the
        # liquidation threshold; raw formula output would be geometrically
        # nonsensical (e.g. long-liq above entry). Emit entry prices as a
        # "liquidation imminent" signal so the comparator sees an obviously
        # distressed state instead of garbage.
        if pool <= 0:
            logger.warning(
                "%s: pool exhausted (total_equity=%s, mm_total=%s); account "
                "at or beyond liquidation threshold — emitting entry prices "
                "as liq signal",
                self.strat_id, total_equity, mm_total,
            )
            liq_long_out = L_entry if L_size > 0 else Decimal("0")
            liq_short_out = S_entry if S_size > 0 else Decimal("0")
            return liq_long_out, liq_short_out

        if q_net > 0:
            # Net long: liq_long = entry - pool/q_net.
            # Negative result means equity fully covers the long; clamp to 0
            # to match Bybit returning NULL/0 in the safe regime.
            liq_long = L_entry - pool / q_net
            return max(liq_long, Decimal("0")), Decimal("0")
        if q_net < 0:
            q_abs = -q_net
            liq_short = S_entry + pool / q_abs
            # 0043 derivation: Bybit returns the raw liq even when it is
            # far above market in HEDGED configurations — validation row
            # L=2.2/S=3.1 has live liq_short=171 at S_entry=58 (above 2×).
            # No cap there.
            #
            # TODO(0043, Phase 4): for SHORT-ONLY (L_size == 0) we lack
            # mainnet evidence on whether Bybit emits the raw value or
            # returns 0 when liq is far above market. We preserve the
            # pre-0043 safe cap defensively. Drop this branch once the
            # 0034 comparator on a short-only run confirms one direction
            # — both options are documented in docs/features/0043_PLAN.md
            # Open Decisions.
            short_only_cap = S_entry * self._SHORT_ONLY_LIQ_CAP_MULTIPLIER
            if L_size <= 0 and liq_short > short_only_cap:
                return Decimal("0"), Decimal("0")
            return Decimal("0"), liq_short
        # Fully hedged: q_net == 0 — both legs offset, no liq risk.
        return Decimal("0"), Decimal("0")

    def _update_risk_multipliers(
        self,
        last_price: float,
        liq_long: Optional[Decimal] = None,
        liq_short: Optional[Decimal] = None,
    ) -> None:
        """Recalculate risk multipliers from current position state.

        Mirrors live bot pattern: reset both, calculate long first, then short.

        ``liq_long`` / ``liq_short`` (0043 perf): caller may pass a
        precomputed pair so we don't repeat ``_estimate_pair_liq_prices``
        twice within one ``_process_fill`` call (once here, once in the
        emitted snapshot). When ``None``, computed inline.
        """
        wallet_balance = self._session.current_balance

        # 0043: compute pair liq once for this tick (or reuse precomputed),
        # hand each leg's value to _build_position_state. Single formula.
        if liq_long is None or liq_short is None:
            liq_long, liq_short = self._estimate_pair_liq_prices(
                self._long_tracker.state,
                self._short_tracker.state,
                self._session.total_equity,
            )

        long_state = self._build_position_state(
            self._long_tracker, wallet_balance, DirectionType.LONG, liq_long
        )
        short_state = self._build_position_state(
            self._short_tracker, wallet_balance, DirectionType.SHORT, liq_short
        )

        # Cache size-based position_ratio + liq_price on both Position
        # instances so consumers (e.g. early_imbalance_multiplier in
        # _apply_risk_to_qty) read consistent values even when one side
        # is empty and calculate_amount_multiplier is skipped below.
        long_size = float(long_state.size) if long_state.size else 0.0
        short_size = float(short_state.size) if short_state.size else 0.0
        if short_size > 0:
            position_ratio = long_size / short_size
        elif long_size > 0:
            position_ratio = float("inf")
        else:
            position_ratio = 1.0
        self._long_position.position_ratio = position_ratio
        self._short_position.position_ratio = position_ratio
        self._long_position.size = long_state.size
        self._short_position.size = short_state.size
        self._long_position.liquidation_price = long_state.liquidation_price
        self._short_position.liquidation_price = short_state.liquidation_price

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
        amount pattern + rounding, then scales by the risk multiplier,
        then re-rounds to qty_step (matching live _resolve_qty pattern).
        """
        # Compute base qty using the original calculator (amount/rounding)
        if self._base_qty_calculator is not None:
            base_qty = self._base_qty_calculator(intent, wallet_balance)
        else:
            base_qty = intent.qty

        multiplier = self.get_amount_multiplier(intent.direction, intent.side)
        result = base_qty * Decimal(str(multiplier))

        # bbu2 early-imbalance multiplier — see gridcore.qty.apply_early_imbalance
        # for full semantic. Outer guard skips when risk module is disabled
        # (Position instances are None in that mode — backtest convention).
        if self._enable_risk:
            result = apply_early_imbalance(
                result,
                self._long_position,
                self._short_position,
                self._config.early_imbalance_multiplier,
            )

        # Re-round after multiplier to ensure qty aligns with exchange qty_step
        # (matches live runner _resolve_qty lines 526-527)
        if self._instrument_info is not None and result > 0:
            result = self._instrument_info.round_qty(result)

        return result

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

    def get_total_im(self) -> Decimal:
        """Get combined initial margin from both directions."""
        return self._long_tracker.state.initial_margin + self._short_tracker.state.initial_margin

    def get_total_mm(self) -> Decimal:
        """Get combined maintenance margin from both directions."""
        return self._long_tracker.state.maintenance_margin + self._short_tracker.state.maintenance_margin

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
