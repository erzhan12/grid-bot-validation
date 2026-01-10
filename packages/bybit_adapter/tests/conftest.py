"""Test fixtures for bybit_adapter tests."""

import pytest
from uuid import uuid4


@pytest.fixture
def sample_ticker_message():
    """Sample Bybit ticker WebSocket message."""
    return {
        "topic": "tickers.BTCUSDT",
        "type": "snapshot",
        "ts": 1704639600000,
        "data": {
            "symbol": "BTCUSDT",
            "lastPrice": "42500.50",
            "markPrice": "42501.00",
            "bid1Price": "42500.00",
            "ask1Price": "42501.00",
            "fundingRate": "0.0001",
            "openInterest": "100000",
        },
    }


@pytest.fixture
def sample_public_trade_message():
    """Sample Bybit publicTrade WebSocket message."""
    return {
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
                "BT": False,
            },
            {
                "i": "trade-id-124",
                "T": 1704639600001,
                "p": "42500.60",
                "v": "0.05",
                "S": "Sell",
                "s": "BTCUSDT",
                "L": "MinusTick",
                "BT": False,
            },
        ],
    }


@pytest.fixture
def sample_execution_message():
    """Sample Bybit execution WebSocket message."""
    return {
        "topic": "execution",
        "id": "msg-id-123",
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
                "isMaker": True,
            },
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "execId": "exec-uuid-124",
                "orderId": "order-uuid-457",
                "orderLinkId": "",
                "execPrice": "42501.00",
                "execQty": "0.05",
                "execFee": "0.21",
                "execType": "Funding",  # Should be filtered out
                "execTime": "1704639600100",
                "side": "Buy",
                "leavesQty": "0",
                "closedPnl": "0",
                "isMaker": False,
            },
            {
                "category": "spot",  # Should be filtered out (not linear)
                "symbol": "BTCUSDT",
                "execId": "exec-uuid-125",
                "orderId": "order-uuid-458",
                "execPrice": "42500.00",
                "execQty": "0.1",
                "execFee": "0.1",
                "execType": "Trade",
                "execTime": "1704639600200",
                "side": "Sell",
            },
        ],
    }


@pytest.fixture
def sample_order_message():
    """Sample Bybit order WebSocket message."""
    return {
        "topic": "order",
        "id": "msg-id-456",
        "creationTime": 1704639600000,
        "data": [
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "orderId": "order-uuid-100",
                "orderLinkId": "grid_btc_buy_42000",
                "orderType": "Limit",
                "orderStatus": "New",
                "side": "Buy",
                "price": "42000.00",
                "qty": "0.1",
                "leavesQty": "0.1",
                "updatedTime": "1704639600000",
            },
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "orderId": "order-uuid-101",
                "orderLinkId": "market_order",
                "orderType": "Market",  # Should be filtered out
                "orderStatus": "Filled",
                "side": "Buy",
                "price": "0",
                "qty": "0.1",
                "leavesQty": "0",
                "updatedTime": "1704639600100",
            },
        ],
    }


@pytest.fixture
def sample_user_id():
    """Sample user ID for multi-tenant context."""
    return uuid4()


@pytest.fixture
def sample_account_id():
    """Sample account ID for multi-tenant context."""
    return uuid4()


@pytest.fixture
def sample_run_id():
    """Sample run ID for multi-tenant context."""
    return uuid4()
