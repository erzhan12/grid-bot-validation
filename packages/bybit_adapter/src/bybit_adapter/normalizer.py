"""Convert Bybit WebSocket messages to gridcore Event objects.

This module normalizes raw Bybit WebSocket JSON messages into structured gridcore
event objects that can be consumed by the strategy engine.

Bybit API Reference:
- Public Trade: https://bybit-exchange.github.io/docs/v5/websocket/public/trade
- Ticker: https://bybit-exchange.github.io/docs/v5/websocket/public/ticker
- Execution: https://bybit-exchange.github.io/docs/v5/websocket/private/execution
- Order: https://bybit-exchange.github.io/docs/v5/websocket/private/order
"""

from dataclasses import dataclass
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional
from uuid import UUID

from gridcore.events import (
    EventType,
    TickerEvent,
    PublicTradeEvent,
    ExecutionEvent,
    OrderUpdateEvent,
)


@dataclass
class NormalizerContext:
    """Context for normalizing private events with multi-tenant tags."""

    user_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    run_id: Optional[UUID] = None


class BybitNormalizer:
    """Converts raw Bybit WebSocket messages to gridcore Event objects.

    Responsibilities:
    - Parse Bybit JSON message format
    - Convert string values to appropriate types (Decimal, datetime)
    - Add local_ts timestamp for deterministic ordering
    - Attach multi-tenant tags (user_id, account_id, run_id) for private events
    """

    def __init__(self, context: Optional[NormalizerContext] = None):
        """Initialize normalizer with optional multi-tenant context.

        Args:
            context: Multi-tenant tags for private events. If None, events
                    will have None for user_id, account_id, run_id.
        """
        self._context = context or NormalizerContext()

    def normalize_ticker(self, message: dict) -> TickerEvent:
        """Convert tickers.{symbol} message to TickerEvent.

        Bybit ticker message format:
        {
            "topic": "tickers.BTCUSDT",
            "type": "snapshot",
            "data": {
                "symbol": "BTCUSDT",
                "lastPrice": "42500.50",
                "markPrice": "42501.00",
                "bid1Price": "42500.00",
                "ask1Price": "42501.00",
                "fundingRate": "0.0001",
                ...
            },
            "ts": 1704639600000
        }

        Args:
            message: Raw WebSocket message dict

        Returns:
            TickerEvent with normalized values
        """
        local_ts = datetime.now(UTC)
        data = message.get("data", {})

        # Extract symbol from topic or data
        topic = message.get("topic", "")
        symbol = data.get("symbol") or (topic.split(".")[-1] if "." in topic else "")

        # Extract timestamp - prefer message ts, fallback to local
        ts_ms = message.get("ts", int(local_ts.timestamp() * 1000))
        exchange_ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)

        return TickerEvent(
            event_type=EventType.TICKER,
            symbol=symbol,
            exchange_ts=exchange_ts,
            local_ts=local_ts,
            user_id=self._context.user_id,
            account_id=self._context.account_id,
            run_id=self._context.run_id,
            last_price=Decimal(data.get("lastPrice", "0")),
            mark_price=Decimal(data.get("markPrice", "0")),
            bid1_price=Decimal(data.get("bid1Price", "0")),
            ask1_price=Decimal(data.get("ask1Price", "0")),
            funding_rate=Decimal(data.get("fundingRate", "0")),
        )

    def normalize_public_trade(self, message: dict) -> list[PublicTradeEvent]:
        """Convert publicTrade.{symbol} message to list of PublicTradeEvent.

        A single message may contain multiple trades (up to 1024 per Bybit docs).

        Bybit public trade message format:
        {
            "topic": "publicTrade.BTCUSDT",
            "type": "snapshot",
            "ts": 1704639600000,
            "data": [
                {
                    "i": "trade-id-123",
                    "T": 1704639600000,
                    "p": "42500.50",
                    "v": "0.1",
                    "S": "Buy",
                    "s": "BTCUSDT",
                    "L": "PlusTick",
                    "BT": false
                },
                ...
            ]
        }

        Args:
            message: Raw WebSocket message dict

        Returns:
            List of PublicTradeEvent, one per trade in the message
        """
        local_ts = datetime.now(UTC)
        events = []

        # Extract symbol from topic
        topic = message.get("topic", "")
        topic_symbol = topic.split(".")[-1] if "." in topic else ""

        for trade in message.get("data", []):
            # Use trade timestamp if available, otherwise message timestamp
            trade_ts_ms = trade.get("T", message.get("ts", int(local_ts.timestamp() * 1000)))
            exchange_ts = datetime.fromtimestamp(trade_ts_ms / 1000, tz=UTC)

            # Symbol from trade data or topic
            symbol = trade.get("s", topic_symbol)

            event = PublicTradeEvent(
                event_type=EventType.PUBLIC_TRADE,
                symbol=symbol,
                exchange_ts=exchange_ts,
                local_ts=local_ts,
                # No multi-tenant tags for public events
                user_id=None,
                account_id=None,
                run_id=None,
                trade_id=str(trade.get("i", "")),
                side=trade.get("S", ""),  # "Buy" or "Sell"
                price=Decimal(trade.get("p", "0")),
                size=Decimal(trade.get("v", "0")),
            )
            events.append(event)

        return events

    def normalize_execution(self, message: dict) -> list[ExecutionEvent]:
        """Convert execution stream message to list of ExecutionEvent.

        Filters to only include:
        - category == "linear" (derivatives only)
        - execType == "Trade" (exclude funding, liquidation, etc.)

        Bybit execution message format:
        {
            "topic": "execution",
            "id": "msg-id",
            "creationTime": 1704639600000,
            "data": [
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "execId": "exec-uuid-123",
                    "orderId": "order-uuid-456",
                    "orderLinkId": "grid_btc_buy_42500",
                    "execPrice": "42500.50",
                    "execQty": "0.1",
                    "execFee": "0.425",
                    "execType": "Trade",
                    "execTime": "1704639600000",
                    "side": "Buy",
                    "leavesQty": "0",
                    "closedPnl": "10.50",
                    "isMaker": true,
                    ...
                },
                ...
            ]
        }

        Args:
            message: Raw WebSocket message dict

        Returns:
            List of ExecutionEvent, filtered by category=linear and execType=Trade
        """
        local_ts = datetime.now(UTC)
        events = []

        for exec_data in message.get("data", []):
            # Filter: only linear category and Trade exec type
            if exec_data.get("category") != "linear":
                continue
            if exec_data.get("execType") != "Trade":
                continue

            # Parse timestamp - execTime is a string in milliseconds
            exec_time_str = exec_data.get("execTime", "0")
            try:
                exec_ts_ms = int(exec_time_str)
            except ValueError:
                exec_ts_ms = int(local_ts.timestamp() * 1000)
            exchange_ts = datetime.fromtimestamp(exec_ts_ms / 1000, tz=UTC)

            # Parse closed PnL - may be "closedPnl" or "execPnl" depending on API version
            closed_pnl_str = exec_data.get("closedPnl") or exec_data.get("execPnl", "0")

            event = ExecutionEvent(
                event_type=EventType.EXECUTION,
                symbol=exec_data.get("symbol", ""),
                exchange_ts=exchange_ts,
                local_ts=local_ts,
                user_id=self._context.user_id,
                account_id=self._context.account_id,
                run_id=self._context.run_id,
                exec_id=exec_data.get("execId", ""),
                order_id=exec_data.get("orderId", ""),
                order_link_id=exec_data.get("orderLinkId", ""),
                side=exec_data.get("side", ""),
                price=Decimal(exec_data.get("execPrice", "0")),
                qty=Decimal(exec_data.get("execQty", "0")),
                fee=Decimal(exec_data.get("execFee", "0")),
                closed_pnl=Decimal(closed_pnl_str),
            )
            events.append(event)

        return events

    def normalize_order(self, message: dict) -> list[OrderUpdateEvent]:
        """Convert order stream message to list of OrderUpdateEvent.

        Filters to only include:
        - category == "linear" (derivatives only)
        - orderType == "Limit" (only limit orders for grid strategy)

        Bybit order message format:
        {
            "topic": "order",
            "id": "msg-id",
            "creationTime": 1704639600000,
            "data": [
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "orderId": "order-uuid-456",
                    "orderLinkId": "grid_btc_buy_42500",
                    "orderType": "Limit",
                    "orderStatus": "New",
                    "side": "Buy",
                    "price": "42500.00",
                    "qty": "0.1",
                    "leavesQty": "0.1",
                    "updatedTime": "1704639600000",
                    ...
                },
                ...
            ]
        }

        Args:
            message: Raw WebSocket message dict

        Returns:
            List of OrderUpdateEvent, filtered by category=linear and orderType=Limit
        """
        local_ts = datetime.now(UTC)
        events = []

        for order_data in message.get("data", []):
            # Filter: only linear category and Limit orders
            if order_data.get("category") != "linear":
                continue
            if order_data.get("orderType") != "Limit":
                continue

            # Parse timestamp - updatedTime is a string in milliseconds
            updated_time_str = order_data.get("updatedTime", "0")
            try:
                updated_ts_ms = int(updated_time_str)
            except ValueError:
                updated_ts_ms = int(local_ts.timestamp() * 1000)
            exchange_ts = datetime.fromtimestamp(updated_ts_ms / 1000, tz=UTC)

            event = OrderUpdateEvent(
                event_type=EventType.ORDER_UPDATE,
                symbol=order_data.get("symbol", ""),
                exchange_ts=exchange_ts,
                local_ts=local_ts,
                user_id=self._context.user_id,
                account_id=self._context.account_id,
                run_id=self._context.run_id,
                order_id=order_data.get("orderId", ""),
                order_link_id=order_data.get("orderLinkId", ""),
                status=order_data.get("orderStatus", ""),
                side=order_data.get("side", ""),
                price=Decimal(order_data.get("price", "0")),
                qty=Decimal(order_data.get("qty", "0")),
                leaves_qty=Decimal(order_data.get("leavesQty", "0")),
            )
            events.append(event)

        return events

    def set_context(self, context: NormalizerContext) -> None:
        """Update the multi-tenant context for private events.

        Args:
            context: New context with user_id, account_id, run_id
        """
        self._context = context

    def update_run_id(self, run_id: Optional[UUID]) -> None:
        """Update just the run_id in the context.

        Useful when a new run starts but account/user remain the same.

        Args:
            run_id: New run_id to use for subsequent events
        """
        self._context = NormalizerContext(
            user_id=self._context.user_id,
            account_id=self._context.account_id,
            run_id=run_id,
        )
