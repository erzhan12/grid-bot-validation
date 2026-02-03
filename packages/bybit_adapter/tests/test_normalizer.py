"""Tests for BybitNormalizer event conversion."""

import pytest
from datetime import datetime, UTC
from decimal import Decimal
from uuid import uuid4

from bybit_adapter.normalizer import BybitNormalizer, NormalizerContext
from gridcore.events import EventType


class TestNormalizeTicker:
    """Tests for ticker event normalization."""

    def test_normalize_ticker_basic(self, sample_ticker_message):
        """Test basic ticker normalization."""
        normalizer = BybitNormalizer()
        event = normalizer.normalize_ticker(sample_ticker_message)

        assert event.event_type == EventType.TICKER
        assert event.symbol == "BTCUSDT"
        assert event.last_price == Decimal("42500.50")
        assert event.mark_price == Decimal("42501.00")
        assert event.bid1_price == Decimal("42500.00")
        assert event.ask1_price == Decimal("42501.00")
        assert event.funding_rate == Decimal("0.0001")

    def test_normalize_ticker_extracts_symbol_from_topic(self):
        """Test symbol extraction from topic when not in data."""
        message = {
            "topic": "tickers.ETHUSDT",
            "ts": 1704639600000,
            "data": {
                "lastPrice": "2500.00",
                "markPrice": "2500.50",
                "bid1Price": "2499.00",
                "ask1Price": "2501.00",
                "fundingRate": "0.0002",
            },
        }
        normalizer = BybitNormalizer()
        event = normalizer.normalize_ticker(message)

        assert event.symbol == "ETHUSDT"

    def test_normalize_ticker_with_context(
        self, sample_ticker_message, sample_user_id, sample_account_id, sample_run_id
    ):
        """Test ticker normalization with multi-tenant context."""
        context = NormalizerContext(
            user_id=sample_user_id,
            account_id=sample_account_id,
            run_id=sample_run_id,
        )
        normalizer = BybitNormalizer(context=context)
        event = normalizer.normalize_ticker(sample_ticker_message)

        assert event.user_id == sample_user_id
        assert event.account_id == sample_account_id
        assert event.run_id == sample_run_id

    def test_normalize_ticker_timestamp_conversion(self, sample_ticker_message):
        """Test that timestamps are correctly converted."""
        normalizer = BybitNormalizer()
        event = normalizer.normalize_ticker(sample_ticker_message)

        expected_ts = datetime.fromtimestamp(1704639600000 / 1000, tz=UTC)
        assert event.exchange_ts == expected_ts
        assert event.local_ts.tzinfo == UTC

    def test_normalize_ticker_missing_fields(self):
        """Test handling of missing fields with defaults."""
        message = {
            "topic": "tickers.BTCUSDT",
            "ts": 1704639600000,
            "data": {},
        }
        normalizer = BybitNormalizer()
        event = normalizer.normalize_ticker(message)

        assert event.last_price == Decimal("0")
        assert event.mark_price == Decimal("0")


class TestNormalizePublicTrade:
    """Tests for public trade event normalization."""

    def test_normalize_public_trade_returns_list(self, sample_public_trade_message):
        """Test that public trade returns a list of events."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_public_trade(sample_public_trade_message)

        assert isinstance(events, list)
        assert len(events) == 2

    def test_normalize_public_trade_fields(self, sample_public_trade_message):
        """Test public trade field extraction."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_public_trade(sample_public_trade_message)

        event = events[0]
        assert event.event_type == EventType.PUBLIC_TRADE
        assert event.symbol == "BTCUSDT"
        assert event.trade_id == "trade-id-123"
        assert event.side == "Buy"
        assert event.price == Decimal("42500.50")
        assert event.size == Decimal("0.1")

    def test_normalize_public_trade_no_multi_tenant_tags(self, sample_public_trade_message):
        """Test that public trades don't have multi-tenant tags."""
        context = NormalizerContext(
            user_id=uuid4(),
            account_id=uuid4(),
            run_id=uuid4(),
        )
        normalizer = BybitNormalizer(context=context)
        events = normalizer.normalize_public_trade(sample_public_trade_message)

        # Public events should NOT have multi-tenant tags
        for event in events:
            assert event.user_id is None
            assert event.account_id is None
            assert event.run_id is None

    def test_normalize_public_trade_empty_data(self):
        """Test handling of empty trade data."""
        message = {
            "topic": "publicTrade.BTCUSDT",
            "ts": 1704639600000,
            "data": [],
        }
        normalizer = BybitNormalizer()
        events = normalizer.normalize_public_trade(message)

        assert events == []

    def test_normalize_public_trade_uses_individual_timestamps(self, sample_public_trade_message):
        """Test that each trade uses its own timestamp."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_public_trade(sample_public_trade_message)

        assert events[0].exchange_ts == datetime.fromtimestamp(1704639600000 / 1000, tz=UTC)
        assert events[1].exchange_ts == datetime.fromtimestamp(1704639600001 / 1000, tz=UTC)


class TestNormalizeExecution:
    """Tests for execution event normalization."""

    def test_normalize_execution_filters_category(self, sample_execution_message):
        """Test that only linear category executions are included."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_execution(sample_execution_message)

        # Should only include the linear + Trade execution (first one)
        assert len(events) == 1
        assert events[0].exec_id == "exec-uuid-123"

    def test_normalize_execution_filters_exec_type(self, sample_execution_message):
        """Test that only Trade exec type is included."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_execution(sample_execution_message)

        # Funding type should be filtered out
        for event in events:
            assert event.exec_id != "exec-uuid-124"  # This was Funding type

    def test_normalize_execution_fields(self, sample_execution_message):
        """Test execution field extraction."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_execution(sample_execution_message)

        event = events[0]
        assert event.event_type == EventType.EXECUTION
        assert event.symbol == "BTCUSDT"
        assert event.exec_id == "exec-uuid-123"
        assert event.order_id == "order-uuid-456"
        assert event.order_link_id == "grid_btc_buy_42500"
        assert event.side == "Buy"
        assert event.price == Decimal("42500.50")
        assert event.qty == Decimal("0.1")
        assert event.fee == Decimal("0.425")
        assert event.closed_pnl == Decimal("10.50")
        assert event.closed_size == Decimal("0.1")
        assert event.leaves_qty == Decimal("0")

    def test_normalize_execution_closed_size_default(self):
        """Test closed_size defaults to 0 when closedSize not in message."""
        message = {
            "topic": "execution",
            "data": [
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "execId": "exec-1",
                    "orderId": "order-1",
                    "orderLinkId": "link-1",
                    "execPrice": "50000.0",
                    "execQty": "0.1",
                    "execFee": "0.5",
                    "execType": "Trade",
                    "execTime": "1704639600000",
                    "side": "Buy",
                    "closedPnl": "0",
                    # No closedSize field
                },
            ],
        }
        normalizer = BybitNormalizer()
        events = normalizer.normalize_execution(message)

        assert len(events) == 1
        assert events[0].closed_size == Decimal("0")

    def test_normalize_execution_with_context(
        self, sample_execution_message, sample_user_id, sample_account_id, sample_run_id
    ):
        """Test execution normalization with multi-tenant context."""
        context = NormalizerContext(
            user_id=sample_user_id,
            account_id=sample_account_id,
            run_id=sample_run_id,
        )
        normalizer = BybitNormalizer(context=context)
        events = normalizer.normalize_execution(sample_execution_message)

        assert events[0].user_id == sample_user_id
        assert events[0].account_id == sample_account_id
        assert events[0].run_id == sample_run_id

    def test_normalize_execution_empty_data(self):
        """Test handling of empty execution data."""
        message = {
            "topic": "execution",
            "data": [],
        }
        normalizer = BybitNormalizer()
        events = normalizer.normalize_execution(message)

        assert events == []


class TestNormalizeOrder:
    """Tests for order update event normalization."""

    def test_normalize_order_filters_order_type(self, sample_order_message):
        """Test that only Limit orders are included."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_order(sample_order_message)

        # Should only include the Limit order (first one)
        assert len(events) == 1
        assert events[0].order_id == "order-uuid-100"

    def test_normalize_order_fields(self, sample_order_message):
        """Test order update field extraction."""
        normalizer = BybitNormalizer()
        events = normalizer.normalize_order(sample_order_message)

        event = events[0]
        assert event.event_type == EventType.ORDER_UPDATE
        assert event.symbol == "BTCUSDT"
        assert event.order_id == "order-uuid-100"
        assert event.order_link_id == "grid_btc_buy_42000"
        assert event.status == "New"
        assert event.side == "Buy"
        assert event.price == Decimal("42000.00")
        assert event.qty == Decimal("0.1")
        assert event.leaves_qty == Decimal("0.1")

    def test_normalize_order_with_context(
        self, sample_order_message, sample_user_id, sample_account_id, sample_run_id
    ):
        """Test order normalization with multi-tenant context."""
        context = NormalizerContext(
            user_id=sample_user_id,
            account_id=sample_account_id,
            run_id=sample_run_id,
        )
        normalizer = BybitNormalizer(context=context)
        events = normalizer.normalize_order(sample_order_message)

        assert events[0].user_id == sample_user_id
        assert events[0].account_id == sample_account_id
        assert events[0].run_id == sample_run_id


class TestNormalizerContextUpdate:
    """Tests for normalizer context management."""

    def test_set_context(self, sample_ticker_message):
        """Test setting a new context."""
        normalizer = BybitNormalizer()

        new_user_id = uuid4()
        new_context = NormalizerContext(user_id=new_user_id)
        normalizer.set_context(new_context)

        event = normalizer.normalize_ticker(sample_ticker_message)
        assert event.user_id == new_user_id

    def test_update_run_id(self, sample_ticker_message):
        """Test updating just the run_id."""
        user_id = uuid4()
        account_id = uuid4()
        initial_context = NormalizerContext(user_id=user_id, account_id=account_id)
        normalizer = BybitNormalizer(context=initial_context)

        new_run_id = uuid4()
        normalizer.update_run_id(new_run_id)

        event = normalizer.normalize_ticker(sample_ticker_message)
        assert event.user_id == user_id  # Unchanged
        assert event.account_id == account_id  # Unchanged
        assert event.run_id == new_run_id  # Updated
