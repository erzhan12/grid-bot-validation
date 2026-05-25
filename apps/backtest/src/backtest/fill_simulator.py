"""Trade-through fill simulator for backtest.

Implements the fill logic: orders fill when price crosses their limit price.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from gridcore import SideType, TickerEvent

if TYPE_CHECKING:
    from backtest.order_manager import SimulatedOrder


class FillMode(StrEnum):
    """Supported simulated limit-order fill modes."""

    STRICT_CROSS = "strict_cross"
    TRADE_THROUGH_AT_LIMIT = "trade_through_at_limit"
    BOOK_TOUCH = "book_touch"
    LAST_CROSS = "last_cross"


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
