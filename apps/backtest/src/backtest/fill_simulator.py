"""Trade-through fill simulator for backtest.

Implements the fill logic: orders fill when price crosses their limit price.

Also home to the event_follower fill source (feature 0072):
``RecordedExecution`` + ``EventFollower`` replay the recorded live
``private_executions`` stream instead of inferring fills from the ticker.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from gridcore import SideType, TickerEvent, extract_client_order_prefix

if TYPE_CHECKING:
    from backtest.order_manager import SimulatedOrder


logger = logging.getLogger(__name__)


class FillMode(StrEnum):
    """Supported simulated limit-order fill modes."""

    STRICT_CROSS = "strict_cross"
    TRADE_THROUGH_AT_LIMIT = "trade_through_at_limit"
    BOOK_TOUCH = "book_touch"
    LAST_CROSS = "last_cross"
    # Fills sourced from recorded live private_executions (feature 0072).
    # Dispatched as a pre-tick injection in BacktestRunner.process_fills —
    # never reaches TradeThroughFillSimulator._should_fill.
    EVENT_FOLLOWER = "event_follower"


@dataclass(frozen=True)
class FillResult:
    """Result of a fill check."""

    should_fill: bool
    fill_price: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    """Internal normalized market data used for fill checks."""

    last_price: Decimal
    bid1_price: Decimal | None = None
    ask1_price: Decimal | None = None
    symbol: str | None = None


class TradeThroughFillSimulator:
    """Configurable fill model for simulated limit orders.

    Supported modes:
    - strict_cross: conservative default; BUY below limit, SELL above limit.
      Exact limit touches do not fill because queue position and volume are
      unknown.
    - trade_through_at_limit: last-price model that includes exact limit
      touches.
    - book_touch: L1-aware parity mode; BUY when ask touches/crosses limit,
      SELL when bid touches/crosses limit, with last-price-at-limit fallback
      only when L1 data is unavailable.
    - last_cross: transition-based mode; BUY when prev_last > limit and
      curr_last <= limit (strict inequality on prev), SELL when prev_last <
      limit and curr_last >= limit. Requires a prior tick — first observation
      of a symbol never fills. Bare-Decimal input never fills under
      LAST_CROSS (no symbol/exchange_ts available to key the per-tick state).

    Per-tick state for LAST_CROSS (other modes are stateless and unaffected):
    - ``_prev_last_price``: committed prior-tick last_price per symbol,
      written exactly once per tick by ``advance_market``.
    - ``_tick_prev_last``: read slot consulted by
      ``_should_fill_last_cross``; populated from the committed slot at the
      start of each tick.
    - ``_tick_token``: per-symbol idempotency guard keyed on
      ``(symbol, exchange_ts, local_ts)``.

    The caller (``BacktestOrderManager.check_fills``) must invoke
    ``advance_market(market)`` exactly once per TickerEvent for ``last_cross``
    to function correctly. ``advance_market`` runs unconditionally today
    regardless of mode; state writes are harmless for the other three modes
    because they never read the slots. Mode-gating (early-return when
    ``self._mode is not FillMode.LAST_CROSS``) is an optional future
    optimization, deferred until benchmarks show measurable cost.

    Fill price is always the limit price.
    """

    def __init__(self, mode: FillMode = FillMode.STRICT_CROSS):
        try:
            self._mode = FillMode(mode)
        except ValueError as exc:
            valid = ", ".join(m.value for m in FillMode)
            raise ValueError(
                f"Invalid fill mode {mode!r}. Valid modes: {valid}"
            ) from exc
        # Per-symbol state for LAST_CROSS (see class docstring).
        self._prev_last_price: dict[str, Decimal] = {}
        self._tick_prev_last: dict[str, Decimal | None] = {}
        self._tick_token: dict[str, tuple] = {}

    @property
    def mode(self) -> FillMode:
        """Fill mode used by this simulator."""
        return self._mode

    def check_fill(
        self,
        order: "SimulatedOrder",
        market: TickerEvent | Decimal,
    ) -> FillResult:
        """Check if order should fill based on the configured fill mode.

        Args:
            order: The order to check
            market: TickerEvent for L1-aware modes, or legacy bare Decimal.
                Bare Decimal is retained for backward compatibility; prefer
                TickerEvent for new callers.

        Returns:
            FillResult with should_fill flag and fill_price
        """
        snapshot = self._to_snapshot(market)
        should_fill = self._should_fill(order.side, order.price, snapshot)
        fill_price = order.price if should_fill else Decimal("0")
        return FillResult(should_fill=should_fill, fill_price=fill_price)

    def advance_market(self, market: TickerEvent) -> None:
        """Advance per-tick state for the LAST_CROSS mode.

        Must be called once per TickerEvent by the caller (typically
        ``BacktestOrderManager.check_fills``) before the per-order loop.
        Stash/commit ordering keeps the read slot pointing at the prior tick
        for the entire per-order loop on the current tick.

        Idempotent for the same ``(symbol, exchange_ts, local_ts)`` token:
        repeated calls within a tick are no-ops. State writes are gated by
        ``curr_last > 0`` and ``symbol is not None`` — invalid market data
        leaves both slots untouched.
        """
        symbol = market.symbol
        curr_last = market.last_price
        if curr_last <= 0 or symbol is None:
            return
        token = (symbol, market.exchange_ts, market.local_ts)
        if self._tick_token.get(symbol) == token:
            return
        # Token mismatch: first call for this tick. Stash the committed
        # prior-tick value into the read slot BEFORE overwriting the
        # committed slot — otherwise _should_fill_last_cross would observe
        # the just-committed curr_last and never see a transition.
        self._tick_prev_last[symbol] = self._prev_last_price.get(symbol)
        self._prev_last_price[symbol] = curr_last
        self._tick_token[symbol] = token

    def _to_snapshot(self, market: TickerEvent | Decimal) -> MarketSnapshot:
        """Normalize supported market inputs to an internal snapshot."""
        if isinstance(market, TickerEvent):
            return MarketSnapshot(
                last_price=market.last_price,
                bid1_price=self._normalize_l1_price(market.bid1_price),
                ask1_price=self._normalize_l1_price(market.ask1_price),
                symbol=market.symbol,
            )

        return MarketSnapshot(last_price=market)

    def _normalize_l1_price(self, price: Decimal) -> Decimal | None:
        """Treat non-positive L1 defaults as missing book data."""
        return price if price > 0 else None

    def _should_fill(
        self,
        side: str,
        limit_price: Decimal,
        snapshot: MarketSnapshot,
    ) -> bool:
        """Determine if order should fill based on configured mode.

        Modes:
        - strict_cross: BUY below limit, SELL above limit
        - trade_through_at_limit: BUY at/below, SELL at/above
        - last_cross: transition-based; BUY when prev_last > limit and
          curr_last <= limit (strict on prev); SELL symmetric. Requires
          a prior tick stashed via ``advance_market``.
        - book_touch: BUY ask at/below, SELL bid at/above; falls back to
          trade_through_at_limit when L1 data is unavailable

        Args:
            side: 'Buy' or 'Sell'
            limit_price: Order's limit price
            snapshot: Normalized market snapshot

        Returns:
            True if order should fill
        """
        match self._mode:
            case FillMode.STRICT_CROSS:
                return self._should_fill_strict_cross(
                    side, limit_price, snapshot.last_price,
                )
            case FillMode.TRADE_THROUGH_AT_LIMIT:
                return self._should_fill_at_limit(
                    side, limit_price, snapshot.last_price,
                )
            case FillMode.BOOK_TOUCH:
                return self._should_fill_book_touch(side, limit_price, snapshot)
            case FillMode.LAST_CROSS:
                return self._should_fill_last_cross(side, limit_price, snapshot)
            case FillMode.EVENT_FOLLOWER:
                # event_follower fills are injected from recorded
                # private_executions in BacktestRunner.process_fills; the
                # per-order simulator check must never be consulted.
                raise ValueError(
                    "event_follower mode must not reach the per-order fill "
                    "check; fills are injected from recorded executions"
                )

        raise ValueError(f"Unsupported fill mode: {self._mode}")

    def _should_fill_strict_cross(
        self,
        side: str,
        limit_price: Decimal,
        current_price: Decimal,
    ) -> bool:
        """Strict conservative fill check."""
        if current_price <= 0:
            return False
        if side == SideType.BUY:
            # Buy limit fills when price crosses below limit
            return current_price < limit_price
        else:  # Sell
            # Sell limit fills when price crosses above limit
            return current_price > limit_price

    def _should_fill_at_limit(
        self,
        side: str,
        limit_price: Decimal,
        current_price: Decimal,
    ) -> bool:
        """Trade-through fill check that includes exact limit touches."""
        if current_price <= 0:
            return False
        if side == SideType.BUY:
            return current_price <= limit_price
        else:  # Sell
            return current_price >= limit_price

    def _should_fill_book_touch(
        self,
        side: str,
        limit_price: Decimal,
        snapshot: MarketSnapshot,
    ) -> bool:
        """L1-aware fill check with legacy last-price fallback."""
        if side == SideType.BUY:
            if snapshot.ask1_price is None:
                return self._should_fill_at_limit(side, limit_price, snapshot.last_price)
            return snapshot.ask1_price <= limit_price
        elif side == SideType.SELL:
            if snapshot.bid1_price is None:
                return self._should_fill_at_limit(side, limit_price, snapshot.last_price)
            return snapshot.bid1_price >= limit_price
        else:
            raise ValueError(f"Invalid order side: {side!r}")

    def _should_fill_last_cross(
        self,
        side: str,
        limit_price: Decimal,
        snapshot: MarketSnapshot,
    ) -> bool:
        """Transition-based fill check using last_price crossings.

        Reads the stashed prior-tick last_price from ``_tick_prev_last``
        (populated by ``advance_market``); never reads ``_prev_last_price``
        directly and never writes to any state slot. A ``None`` read-slot
        value (no prior tick yet) cannot fire a fill.
        """
        # Validate side up-front so malformed orders raise regardless of
        # warm/cold state (mirrors _should_fill_book_touch).
        if side != SideType.BUY and side != SideType.SELL:
            raise ValueError(f"Invalid order side: {side!r}")
        symbol = snapshot.symbol
        curr_last = snapshot.last_price
        if curr_last <= 0 or symbol is None:
            return False
        prev_last = self._tick_prev_last.get(symbol)
        if prev_last is None:
            return False
        if side == SideType.BUY:
            return prev_last > limit_price and curr_last <= limit_price
        # SELL (side already validated above).
        return prev_last < limit_price and curr_last >= limit_price

    def get_fill_price(self, order: "SimulatedOrder") -> Decimal:
        """Get fill price for an order.

        Always returns the limit price (conservative assumption).
        In reality, fills might get better prices, but we assume worst case.

        Args:
            order: The order being filled

        Returns:
            Fill price (always the limit price)
        """
        return order.price


def _to_naive_utc(ts: datetime) -> datetime:
    """Normalize a tz-aware datetime to naive UTC; pass naive through.

    Recorded executions are naive UTC (SQLite strips tz at load); ticker
    events may carry tzinfo. Same convention as the loaders'
    ``_normalize_ts`` / ``_strip_tz`` — comparisons inside the follower are
    always naive-vs-naive.
    """
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


@dataclass(frozen=True)
class RecordedExecution:
    """One recorded live execution, materialized from ``PrivateExecution``.

    Plain frozen dataclass — the follower NEVER holds raw ORM rows. The
    replay engine converts each ``PrivateExecution`` to a
    ``RecordedExecution`` inside the DB session context (avoids
    ``DetachedInstanceError`` on lazy attribute access after the session
    closes; cf. feature 0038). ``exchange_ts`` is naive UTC (normalized at
    conversion time with the same tz-strip helper the loaders use).
    ``exec_fee`` / ``closed_pnl`` default to ``Decimal("0")`` when the DB
    column is NULL.
    """

    exec_id: str
    order_link_id: str | None
    order_id: str
    side: str  # 'Buy' or 'Sell'
    exec_price: Decimal
    exec_qty: Decimal
    exec_fee: Decimal
    closed_pnl: Decimal
    exchange_ts: datetime


@dataclass(frozen=True)
class MatchResult:
    """Result of matching a recorded execution against active replay orders.

    Two distinct order-id namespaces — never conflate:

    - ``replay_order_id``: the matched ``SimulatedOrder.order_id``
      (``sim_*``, or exchange id for seeded orders) used to locate the
      order in ``active_orders`` for ``apply_recorded_fill``.
    - ``recorded_order_id``: the live Bybit ``order_id`` from the recorded
      execution, used (with ``matcher_key``) as the rollup/aggregation key
      mirroring ``LiveTradeLoader`` grouping.

    ``matcher_key`` is ``extract_client_order_prefix(order_link_id) or
    recorded_order_id`` — the exact key the post-run matcher uses, stamped
    onto the produced ``BacktestTrade.client_order_id``.
    """

    replay_order_id: str
    matcher_key: str
    recorded_order_id: str


class EventFollower:
    """Replays the recorded live execution stream as the replay fill source.

    Constructed with the full window's executions, sorted
    ``(exchange_ts, exec_id)`` by
    ``PrivateExecutionRepository.get_by_run_range`` (the single sort site —
    no re-sort here). Holds a forward-only ``exchange_ts``-advancing cursor;
    ``drain`` consumes the stream tick by tick and ``match`` selects the
    active replay order for one execution (key-faithful: deterministic
    ``client_order_id`` prefix first, documented fallbacks after).
    """

    def __init__(
        self,
        executions: list[RecordedExecution],
        symbol: str,
        start_ts: datetime,
    ):
        self._executions = executions
        self._symbol = symbol
        # Left edge for the very first drain window. The drain window is
        # (prev_ts, tick_ts] (open on the left) while get_by_run_range loads
        # exchange_ts >= start_ts inclusive — initializing prev_ts to
        # start_ts itself would silently exclude executions at the window
        # left edge from every drain (spurious live_only). The microsecond
        # offset aligns the first drain with the inclusive load boundary.
        self.initial_prev_ts = start_ts - timedelta(microseconds=1)
        self._cursor = 0
        self._last_tick_ts: datetime | None = None
        # Last index in the stream per recorded (Bybit) order_id — lets
        # trigger-2 (last-in-stream flush) check the unconsumed tail in O(1).
        self._last_index_by_order_id: dict[str, int] = {}
        for i, ex in enumerate(executions):
            self._last_index_by_order_id[ex.order_id] = i
        # Diagnostics: rows with no order_link_id (pre-hotfix fallback rows)
        # and fallback-matched rows, so a window full of them is visible.
        self.no_link_id_count = 0
        self.fallback_order_id_count = 0
        self.fallback_price_count = 0

    @property
    def remaining(self) -> int:
        """Count of executions not yet drained."""
        return len(self._executions) - self._cursor

    def drain(self, prev_ts: datetime, tick_ts: datetime) -> list[RecordedExecution]:
        """Return all executions in ``(prev_ts, tick_ts]``, advancing cursor.

        Forward-only: ``tick_ts`` must be non-decreasing across calls
        (mirrors ``CollateralMarkFeed.mark_at``'s monotonicity guard —
        replay ticks must be ordered by exchange_ts).
        """
        prev_ts = _to_naive_utc(prev_ts)
        tick_ts = _to_naive_utc(tick_ts)
        if self._last_tick_ts is not None and tick_ts < self._last_tick_ts:
            raise ValueError(
                f"EventFollower.drain: non-monotonic tick_ts {tick_ts} "
                f"precedes previous {self._last_tick_ts}; the forward-only "
                f"cursor cannot rewind (replay ticks must be ordered by "
                f"exchange_ts)."
            )
        self._last_tick_ts = tick_ts

        drained: list[RecordedExecution] = []
        while self._cursor < len(self._executions):
            ex = self._executions[self._cursor]
            if ex.exchange_ts > tick_ts:
                break
            self._cursor += 1
            if ex.exchange_ts <= prev_ts:
                # Defensive: window is open on the left; with sequential
                # prev_ts = previous tick_ts this only skips rows older
                # than the very first drain window.
                continue
            drained.append(ex)
        return drained

    def has_pending_for_order(self, recorded_order_id: str) -> bool:
        """True if undrained executions remain for this recorded order_id.

        Used by the trigger-2 (last-in-stream) flush check: a rollup buffer
        may only flush once no further execution for its recorded order
        remains in the unconsumed tail.
        """
        last_idx = self._last_index_by_order_id.get(recorded_order_id)
        return last_idx is not None and last_idx >= self._cursor

    def match(
        self,
        execution: RecordedExecution,
        active_orders: dict[str, "SimulatedOrder"],
    ) -> MatchResult | None:
        """Select the active replay order for one recorded execution.

        Key-faithful selection (feature 0072 design):

        1. Primary: ``extract_client_order_prefix(order_link_id)`` — the
           deterministic grid identity — equals the active order's
           ``client_order_id``. Side/price sanity violations log WARN but
           the id match is authoritative.
        2. ``order_link_id`` is None (pre-hotfix rows): fall back to
           ``order_id`` against ``SimulatedOrder.order_id`` (covers seeded
           orders keyed by exchange id), then same-side closest-price as a
           deterministic last resort.

        Returns None when no active order matches — the execution stays
        ``live_only`` (intent-set divergence; the backtest never invents a
        fill).
        """
        key = extract_client_order_prefix(execution.order_link_id)

        if key is not None:
            for order in active_orders.values():
                if order.client_order_id == key:
                    self._warn_on_sanity_violation(execution, order)
                    return MatchResult(
                        replay_order_id=order.order_id,
                        matcher_key=key,
                        recorded_order_id=execution.order_id,
                    )
            return None

        # No order_link_id: rare pre-orderLinkId-hotfix rows. Mirror
        # LiveTradeLoader's `client_id = link_prefix or ex.order_id`.
        self.no_link_id_count += 1
        matcher_key = execution.order_id

        order = active_orders.get(execution.order_id)
        if order is not None:
            self.fallback_order_id_count += 1
            self._warn_on_sanity_violation(execution, order)
            return MatchResult(
                replay_order_id=order.order_id,
                matcher_key=matcher_key,
                recorded_order_id=execution.order_id,
            )

        # Last resort: same side, closest price. Deterministic tie-break on
        # (price distance, order_id).
        candidates = [
            o for o in active_orders.values() if o.side == execution.side
        ]
        if not candidates:
            return None
        best = min(
            candidates,
            key=lambda o: (abs(o.price - execution.exec_price), o.order_id),
        )
        self.fallback_price_count += 1
        logger.warning(
            "EventFollower: matched exec %s (order_id=%s, no order_link_id) "
            "to replay order %s by side/closest-price (limit=%s, exec=%s)",
            execution.exec_id, execution.order_id, best.order_id,
            best.price, execution.exec_price,
        )
        return MatchResult(
            replay_order_id=best.order_id,
            matcher_key=matcher_key,
            recorded_order_id=execution.order_id,
        )

    def _warn_on_sanity_violation(
        self,
        execution: RecordedExecution,
        order: "SimulatedOrder",
    ) -> None:
        """WARN when side/price disagree with the matched order.

        The deterministic id is authoritative — a violation is logged and
        the match stands. Buy limits fill at-or-below limit; Sell limits
        at-or-above.
        """
        if order.side != execution.side:
            logger.warning(
                "EventFollower: side mismatch on matched id %s: order side=%s "
                "vs exec side=%s (exec_id=%s) — id match kept",
                order.client_order_id, order.side, execution.side,
                execution.exec_id,
            )
            return
        if order.side == SideType.BUY:
            price_ok = order.price >= execution.exec_price
        else:
            price_ok = order.price <= execution.exec_price
        if not price_ok:
            logger.warning(
                "EventFollower: price sanity violation on matched id %s: "
                "%s limit=%s vs exec_price=%s (exec_id=%s) — id match kept",
                order.client_order_id, order.side, order.price,
                execution.exec_price, execution.exec_id,
            )
