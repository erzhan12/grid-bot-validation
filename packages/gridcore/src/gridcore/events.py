"""
Normalized event models for grid trading strategy.

These events represent market data and order updates that the strategy logic
consumes. All events are immutable (frozen dataclasses) to ensure predictable
behavior in backtesting and live trading.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID


class EventType(Enum):
    """Types of events the strategy can process."""
    TICKER = "ticker"
    PUBLIC_TRADE = "public_trade"
    EXECUTION = "execution"
    ORDER_UPDATE = "order_update"


@dataclass(frozen=True)
class Event:
    """
    Base event model for all strategy events.

    All events are sorted by exchange_ts (primary) and local_ts (tiebreaker)
    to ensure deterministic ordering in backtesting.
    """
    event_type: EventType
    symbol: str
    exchange_ts: datetime  # Primary sort key
    local_ts: datetime     # Tiebreaker for deterministic ordering

    # Multi-tenant tags (optional, for run tracking)
    user_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    run_id: Optional[UUID] = None


@dataclass(frozen=True)
class TickerEvent(Event):
    """
    Ticker event from WebSocket tickers.{symbol} stream.

    Reference: bbu2-master/bybit_api_usdt.py:94-99 (handle_ticker)
    Extracted from message['data'] fields.
    """
    last_price: Decimal = Decimal('0')
    mark_price: Decimal = Decimal('0')
    bid1_price: Decimal = Decimal('0')
    ask1_price: Decimal = Decimal('0')
    funding_rate: Decimal = Decimal('0')

    def __post_init__(self):
        # Validate event_type
        if self.event_type != EventType.TICKER:
            raise ValueError(f"TickerEvent must have event_type=TICKER, got {self.event_type}")


@dataclass(frozen=True)
class PublicTradeEvent(Event):
    """
    Public trade event from WebSocket publicTrade.{symbol} stream.

    Represents actual trades happening on the exchange.
    """
    trade_id: str = ""
    side: str = ""  # 'Buy' or 'Sell'
    price: Decimal = Decimal('0')
    size: Decimal = Decimal('0')

    def __post_init__(self):
        if self.event_type != EventType.PUBLIC_TRADE:
            raise ValueError(f"PublicTradeEvent must have event_type=PUBLIC_TRADE, got {self.event_type}")


@dataclass(frozen=True)
class ExecutionEvent(Event):
    """
    Execution event from private WebSocket execution stream.

    Represents our own order executions (fills).
    Reference: bbu2-master/bybit_api_usdt.py handle_execution
    """
    exec_id: str = ""
    order_id: str = ""
    order_link_id: str = ""  # Client order ID
    side: str = ""  # 'Buy' or 'Sell'
    price: Decimal = Decimal('0')
    qty: Decimal = Decimal('0')
    fee: Decimal = Decimal('0')
    closed_pnl: Decimal = Decimal('0')
    closed_size: Decimal = Decimal('0')  # Qty closed by this execution (Bybit closedSize)
    leaves_qty: Decimal = Decimal('0')  # Remaining unfilled quantity (Bybit leavesQty)

    def __post_init__(self):
        if self.event_type != EventType.EXECUTION:
            raise ValueError(f"ExecutionEvent must have event_type=EXECUTION, got {self.event_type}")


@dataclass(frozen=True)
class OrderUpdateEvent(Event):
    """
    Order update event from private WebSocket order stream.

    Represents status changes to our orders (placed, filled, cancelled, etc.).
    Reference: bbu2-master/bybit_api_usdt.py handle_order
    """
    order_id: str = ""
    order_link_id: str = ""  # Client order ID
    status: str = ""  # 'New', 'PartiallyFilled', 'Filled', 'Cancelled', etc.
    side: str = ""  # 'Buy' or 'Sell'
    price: Decimal = Decimal('0')
    qty: Decimal = Decimal('0')
    leaves_qty: Decimal = Decimal('0')  # Remaining unfilled quantity

    def __post_init__(self):
        if self.event_type != EventType.ORDER_UPDATE:
            raise ValueError(f"OrderUpdateEvent must have event_type=ORDER_UPDATE, got {self.event_type}")
