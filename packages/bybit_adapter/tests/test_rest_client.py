"""Tests for BybitRestClient."""

import logging
import pytest
from unittest.mock import MagicMock, patch

from bybit_adapter.rest_client import BybitRestClient


@pytest.fixture
def mock_session():
    """Mock pybit HTTP session."""
    return MagicMock()


@pytest.fixture
def client(mock_session):
    """BybitRestClient with mocked HTTP session."""
    with patch("bybit_adapter.rest_client.HTTP", return_value=mock_session):
        c = BybitRestClient(api_key="test_key", api_secret="test_secret", testnet=True)
    return c


def _ok_response(result):
    """Build a successful API response."""
    return {"retCode": 0, "retMsg": "OK", "result": result}


def _error_response(code=10001, msg="Parameter error"):
    """Build an error API response."""
    return {"retCode": code, "retMsg": msg, "result": {}}


# ---------------------------------------------------------------------------
# get_recent_trades
# ---------------------------------------------------------------------------


class TestGetRecentTrades:
    def test_returns_trade_list(self, client, mock_session):
        trades = [
            {"execId": "1", "symbol": "BTCUSDT", "price": "100000", "size": "0.1", "side": "Buy"},
            {"execId": "2", "symbol": "BTCUSDT", "price": "100001", "size": "0.2", "side": "Sell"},
        ]
        mock_session.get_public_trade_history.return_value = _ok_response({"list": trades})

        result = client.get_recent_trades("BTCUSDT", limit=100)

        assert result == trades
        mock_session.get_public_trade_history.assert_called_once_with(
            category="linear", symbol="BTCUSDT", limit=100
        )

    def test_clamps_limit_to_1000(self, client, mock_session):
        mock_session.get_public_trade_history.return_value = _ok_response({"list": []})

        client.get_recent_trades("BTCUSDT", limit=5000)

        mock_session.get_public_trade_history.assert_called_once_with(
            category="linear", symbol="BTCUSDT", limit=1000
        )

    def test_empty_result(self, client, mock_session):
        mock_session.get_public_trade_history.return_value = _ok_response({"list": []})

        result = client.get_recent_trades("BTCUSDT")

        assert result == []

    def test_api_error_raises(self, client, mock_session):
        mock_session.get_public_trade_history.return_value = _error_response(10001, "Invalid symbol")

        with pytest.raises(Exception, match="Invalid symbol"):
            client.get_recent_trades("INVALID")


# ---------------------------------------------------------------------------
# get_executions
# ---------------------------------------------------------------------------


class TestGetExecutions:
    def test_returns_executions_and_cursor(self, client, mock_session):
        execs = [{"execId": "e1", "orderId": "o1", "execPrice": "100000"}]
        mock_session.get_executions.return_value = _ok_response(
            {"list": execs, "nextPageCursor": "cursor123"}
        )

        result, cursor = client.get_executions(symbol="BTCUSDT")

        assert result == execs
        assert cursor == "cursor123"

    def test_no_cursor_returns_none(self, client, mock_session):
        mock_session.get_executions.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        result, cursor = client.get_executions(symbol="BTCUSDT")

        assert result == []
        assert cursor is None

    def test_passes_optional_params(self, client, mock_session):
        mock_session.get_executions.return_value = _ok_response({"list": [], "nextPageCursor": ""})

        client.get_executions(
            symbol="ETHUSDT",
            start_time=1000000,
            end_time=2000000,
            cursor="prev_cursor",
        )

        mock_session.get_executions.assert_called_once_with(
            category="linear",
            limit=100,
            symbol="ETHUSDT",
            startTime=1000000,
            endTime=2000000,
            cursor="prev_cursor",
        )

    def test_clamps_limit_to_100(self, client, mock_session):
        mock_session.get_executions.return_value = _ok_response({"list": [], "nextPageCursor": ""})

        client.get_executions(limit=500)

        call_kwargs = mock_session.get_executions.call_args[1]
        assert call_kwargs["limit"] == 100

    def test_no_symbol_omits_param(self, client, mock_session):
        mock_session.get_executions.return_value = _ok_response({"list": [], "nextPageCursor": ""})

        client.get_executions()

        call_kwargs = mock_session.get_executions.call_args[1]
        assert "symbol" not in call_kwargs

    def test_api_error_raises(self, client, mock_session):
        mock_session.get_executions.return_value = _error_response(10002, "Auth failed")

        with pytest.raises(Exception, match="Auth failed"):
            client.get_executions(symbol="BTCUSDT")


# ---------------------------------------------------------------------------
# get_executions_all (pagination)
# ---------------------------------------------------------------------------


class TestGetExecutionsAll:
    def test_single_page(self, client, mock_session):
        execs = [{"execId": "e1"}]
        mock_session.get_executions.return_value = _ok_response(
            {"list": execs, "nextPageCursor": ""}
        )

        result = client.get_executions_all(symbol="BTCUSDT")

        assert result == execs
        assert mock_session.get_executions.call_count == 1

    def test_multi_page_follows_cursor(self, client, mock_session):
        page1 = [{"execId": "e1"}]
        page2 = [{"execId": "e2"}]
        mock_session.get_executions.side_effect = [
            _ok_response({"list": page1, "nextPageCursor": "cursor2"}),
            _ok_response({"list": page2, "nextPageCursor": ""}),
        ]

        result = client.get_executions_all(symbol="BTCUSDT")

        assert result == [{"execId": "e1"}, {"execId": "e2"}]
        assert mock_session.get_executions.call_count == 2

    def test_stops_at_max_pages(self, client, mock_session):
        mock_session.get_executions.return_value = _ok_response(
            {"list": [{"execId": "e"}], "nextPageCursor": "more"}
        )

        result = client.get_executions_all(symbol="BTCUSDT", max_pages=3)

        assert len(result) == 3
        assert mock_session.get_executions.call_count == 3

    def test_passes_time_range(self, client, mock_session):
        mock_session.get_executions.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        client.get_executions_all(symbol="BTCUSDT", start_time=100, end_time=200)

        call_kwargs = mock_session.get_executions.call_args[1]
        assert call_kwargs["startTime"] == 100
        assert call_kwargs["endTime"] == 200


# ---------------------------------------------------------------------------
# get_order_history
# ---------------------------------------------------------------------------


class TestGetOrderHistory:
    def test_returns_orders_and_cursor(self, client, mock_session):
        orders = [{"orderId": "o1", "orderStatus": "Filled"}]
        mock_session.get_order_history.return_value = _ok_response(
            {"list": orders, "nextPageCursor": "next1"}
        )

        result, cursor = client.get_order_history(symbol="BTCUSDT")

        assert result == orders
        assert cursor == "next1"

    def test_no_cursor_returns_none(self, client, mock_session):
        mock_session.get_order_history.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        result, cursor = client.get_order_history()

        assert result == []
        assert cursor is None

    def test_clamps_limit_to_50(self, client, mock_session):
        mock_session.get_order_history.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        client.get_order_history(limit=200)

        call_kwargs = mock_session.get_order_history.call_args[1]
        assert call_kwargs["limit"] == 50

    def test_passes_optional_params(self, client, mock_session):
        mock_session.get_order_history.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        client.get_order_history(
            symbol="ETHUSDT", start_time=100, end_time=200, cursor="c1"
        )

        call_kwargs = mock_session.get_order_history.call_args[1]
        assert call_kwargs["symbol"] == "ETHUSDT"
        assert call_kwargs["startTime"] == 100
        assert call_kwargs["endTime"] == 200
        assert call_kwargs["cursor"] == "c1"


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------


class TestGetPositions:
    def test_returns_positions(self, client, mock_session):
        positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}]
        mock_session.get_positions.return_value = _ok_response({"list": positions})

        result = client.get_positions(symbol="BTCUSDT")

        assert result == positions
        mock_session.get_positions.assert_called_once_with(
            category="linear", settleCoin="USDT", symbol="BTCUSDT"
        )

    def test_no_symbol_fetches_all(self, client, mock_session):
        mock_session.get_positions.return_value = _ok_response({"list": []})

        client.get_positions()

        call_kwargs = mock_session.get_positions.call_args[1]
        assert "symbol" not in call_kwargs
        assert call_kwargs["settleCoin"] == "USDT"

    def test_api_error_raises(self, client, mock_session):
        mock_session.get_positions.return_value = _error_response(10003, "Forbidden")

        with pytest.raises(Exception, match="Forbidden"):
            client.get_positions()


# ---------------------------------------------------------------------------
# get_wallet_balance
# ---------------------------------------------------------------------------


class TestGetWalletBalance:
    def test_returns_balance(self, client, mock_session):
        balance = {"list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]}
        mock_session.get_wallet_balance.return_value = _ok_response(balance)

        result = client.get_wallet_balance()

        assert result == balance
        mock_session.get_wallet_balance.assert_called_once_with(accountType="UNIFIED")

    def test_custom_account_type(self, client, mock_session):
        mock_session.get_wallet_balance.return_value = _ok_response({})

        client.get_wallet_balance(account_type="CONTRACT")

        mock_session.get_wallet_balance.assert_called_once_with(accountType="CONTRACT")

    def test_api_error_raises(self, client, mock_session):
        mock_session.get_wallet_balance.return_value = _error_response(10001, "Auth failed")

        with pytest.raises(Exception, match="Auth failed"):
            client.get_wallet_balance()


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def test_places_limit_order(self, client, mock_session):
        mock_session.place_order.return_value = _ok_response(
            {"orderId": "order-123", "orderLinkId": "link-456"}
        )

        result = client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            order_type="Limit",
            qty="0.001",
            price="100000",
            order_link_id="link-456",
        )

        assert result["orderId"] == "order-123"
        call_kwargs = mock_session.place_order.call_args[1]
        assert call_kwargs["symbol"] == "BTCUSDT"
        assert call_kwargs["side"] == "Buy"
        assert call_kwargs["orderType"] == "Limit"
        assert call_kwargs["qty"] == "0.001"
        assert call_kwargs["price"] == "100000"
        assert call_kwargs["orderLinkId"] == "link-456"
        assert call_kwargs["reduceOnly"] is False
        assert call_kwargs["positionIdx"] == 0

    def test_places_market_order_no_price(self, client, mock_session):
        mock_session.place_order.return_value = _ok_response({"orderId": "o1"})

        client.place_order(symbol="BTCUSDT", side="Sell", order_type="Market", qty="0.1")

        call_kwargs = mock_session.place_order.call_args[1]
        assert "price" not in call_kwargs

    def test_reduce_only_flag(self, client, mock_session):
        mock_session.place_order.return_value = _ok_response({"orderId": "o1"})

        client.place_order(
            symbol="BTCUSDT", side="Sell", order_type="Limit",
            qty="0.1", price="100000", reduce_only=True,
        )

        call_kwargs = mock_session.place_order.call_args[1]
        assert call_kwargs["reduceOnly"] is True

    def test_api_error_raises(self, client, mock_session):
        mock_session.place_order.return_value = _error_response(110001, "Insufficient balance")

        with pytest.raises(Exception, match="Insufficient balance"):
            client.place_order(
                symbol="BTCUSDT", side="Buy", order_type="Limit",
                qty="100", price="100000",
            )


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_cancel_by_order_id(self, client, mock_session):
        mock_session.cancel_order.return_value = _ok_response({"orderId": "o1"})

        result = client.cancel_order(symbol="BTCUSDT", order_id="o1")

        assert result is True
        call_kwargs = mock_session.cancel_order.call_args[1]
        assert call_kwargs["orderId"] == "o1"
        assert "orderLinkId" not in call_kwargs

    def test_cancel_by_order_link_id(self, client, mock_session):
        mock_session.cancel_order.return_value = _ok_response({"orderId": "o1"})

        result = client.cancel_order(symbol="BTCUSDT", order_link_id="link1")

        assert result is True
        call_kwargs = mock_session.cancel_order.call_args[1]
        assert call_kwargs["orderLinkId"] == "link1"
        assert "orderId" not in call_kwargs

    def test_cancel_by_both_ids(self, client, mock_session):
        mock_session.cancel_order.return_value = _ok_response({"orderId": "o1"})

        result = client.cancel_order(symbol="BTCUSDT", order_id="o1", order_link_id="link1")

        assert result is True
        call_kwargs = mock_session.cancel_order.call_args[1]
        assert call_kwargs["orderId"] == "o1"
        assert call_kwargs["orderLinkId"] == "link1"

    def test_requires_at_least_one_id(self, client, mock_session):
        with pytest.raises(ValueError, match="Either order_id or order_link_id"):
            client.cancel_order(symbol="BTCUSDT")

    def test_returns_false_on_exception(self, client, mock_session):
        mock_session.cancel_order.side_effect = Exception("Order already filled")

        result = client.cancel_order(symbol="BTCUSDT", order_id="o1")

        assert result is False

    def test_returns_false_on_api_error(self, client, mock_session):
        mock_session.cancel_order.return_value = _error_response(110001, "Order not found")

        result = client.cancel_order(symbol="BTCUSDT", order_id="o1")

        assert result is False


# ---------------------------------------------------------------------------
# cancel_all_orders
# ---------------------------------------------------------------------------


class TestCancelAllOrders:
    def test_returns_cancelled_count(self, client, mock_session):
        mock_session.cancel_all_orders.return_value = _ok_response(
            {"list": [{"orderId": "o1"}, {"orderId": "o2"}]}
        )

        result = client.cancel_all_orders(symbol="BTCUSDT")

        assert result == 2
        mock_session.cancel_all_orders.assert_called_once_with(
            category="linear", symbol="BTCUSDT"
        )

    def test_returns_zero_when_none_cancelled(self, client, mock_session):
        mock_session.cancel_all_orders.return_value = _ok_response({"list": []})

        result = client.cancel_all_orders(symbol="BTCUSDT")

        assert result == 0


# ---------------------------------------------------------------------------
# get_open_orders (with pagination)
# ---------------------------------------------------------------------------


class TestGetOpenOrders:
    def test_returns_filtered_orders(self, client, mock_session):
        orders = [
            {"orderId": "o1", "orderType": "Limit"},
            {"orderId": "o2", "orderType": "Market"},
        ]
        mock_session.get_open_orders.return_value = _ok_response(
            {"list": orders, "nextPageCursor": ""}
        )

        result = client.get_open_orders(symbol="BTCUSDT")

        assert len(result) == 1
        assert result[0]["orderId"] == "o1"

    def test_paginates_until_no_cursor(self, client, mock_session):
        page1 = [{"orderId": "o1", "orderType": "Limit"}]
        page2 = [{"orderId": "o2", "orderType": "Limit"}]
        mock_session.get_open_orders.side_effect = [
            _ok_response({"list": page1, "nextPageCursor": "c2"}),
            _ok_response({"list": page2, "nextPageCursor": ""}),
        ]

        result = client.get_open_orders(symbol="BTCUSDT")

        assert len(result) == 2
        assert mock_session.get_open_orders.call_count == 2

    def test_stops_on_empty_page(self, client, mock_session):
        mock_session.get_open_orders.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        result = client.get_open_orders(symbol="BTCUSDT")

        assert result == []
        assert mock_session.get_open_orders.call_count == 1

    def test_custom_order_type_filter(self, client, mock_session):
        orders = [
            {"orderId": "o1", "orderType": "Market"},
            {"orderId": "o2", "orderType": "Limit"},
        ]
        mock_session.get_open_orders.return_value = _ok_response(
            {"list": orders, "nextPageCursor": ""}
        )

        result = client.get_open_orders(symbol="BTCUSDT", order_type="Market")

        assert len(result) == 1
        assert result[0]["orderId"] == "o1"

    def test_no_symbol_fetches_all(self, client, mock_session):
        mock_session.get_open_orders.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        client.get_open_orders()

        call_kwargs = mock_session.get_open_orders.call_args[1]
        assert "symbol" not in call_kwargs


# ---------------------------------------------------------------------------
# _check_response
# ---------------------------------------------------------------------------


class TestCheckResponse:
    def test_no_error_on_success(self, client):
        client._check_response({"retCode": 0, "retMsg": "OK"}, "test_method")

    def test_raises_on_non_zero_ret_code(self, client):
        with pytest.raises(Exception, match="Some error"):
            client._check_response({"retCode": 10001, "retMsg": "Some error"}, "test_method")

    def test_raises_on_missing_ret_code(self, client):
        with pytest.raises(Exception, match="Unknown error"):
            client._check_response({}, "test_method")


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestInit:
    def test_creates_http_session(self):
        with patch("bybit_adapter.rest_client.HTTP") as mock_http:
            BybitRestClient(api_key="key1", api_secret="secret1", testnet=False)

            mock_http.assert_called_once_with(
                testnet=False, api_key="key1", api_secret="secret1"
            )

    def test_default_testnet_true(self):
        with patch("bybit_adapter.rest_client.HTTP") as mock_http:
            BybitRestClient(api_key="k", api_secret="s")

            mock_http.assert_called_once_with(testnet=True, api_key="k", api_secret="s")


# ---------------------------------------------------------------------------
# get_tickers
# ---------------------------------------------------------------------------


class TestGetTickers:
    def test_returns_first_ticker(self, client, mock_session):
        ticker = {"lastPrice": "100000", "markPrice": "100001", "fundingRate": "0.0001"}
        mock_session.get_tickers.return_value = _ok_response({"list": [ticker]})

        result = client.get_tickers(symbol="BTCUSDT")

        assert result == ticker
        mock_session.get_tickers.assert_called_once_with(
            category="linear", symbol="BTCUSDT"
        )

    def test_empty_list_raises(self, client, mock_session):
        mock_session.get_tickers.return_value = _ok_response({"list": []})

        with pytest.raises(Exception, match="No ticker data"):
            client.get_tickers(symbol="BTCUSDT")

    def test_api_error_raises(self, client, mock_session):
        mock_session.get_tickers.return_value = _error_response(10001, "Invalid symbol")

        with pytest.raises(Exception, match="Invalid symbol"):
            client.get_tickers(symbol="INVALID")


# ---------------------------------------------------------------------------
# get_transaction_log
# ---------------------------------------------------------------------------


class TestGetTransactionLog:
    def test_returns_transactions_and_cursor(self, client, mock_session):
        txns = [{"transactionTime": "1234", "funding": "-0.01"}]
        mock_session.get_transaction_log.return_value = _ok_response(
            {"list": txns, "nextPageCursor": "cursor456"}
        )

        result, cursor = client.get_transaction_log(symbol="BTCUSDT", type="SETTLEMENT")

        assert result == txns
        assert cursor == "cursor456"

    def test_no_cursor_returns_none(self, client, mock_session):
        mock_session.get_transaction_log.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        result, cursor = client.get_transaction_log(symbol="BTCUSDT")

        assert result == []
        assert cursor is None

    def test_passes_optional_params(self, client, mock_session):
        mock_session.get_transaction_log.return_value = _ok_response(
            {"list": [], "nextPageCursor": ""}
        )

        client.get_transaction_log(
            symbol="ETHUSDT", type="SETTLEMENT",
            start_time=1000, end_time=2000, cursor="c1",
        )

        call_kwargs = mock_session.get_transaction_log.call_args[1]
        assert call_kwargs["symbol"] == "ETHUSDT"
        assert call_kwargs["type"] == "SETTLEMENT"
        assert call_kwargs["startTime"] == 1000
        assert call_kwargs["endTime"] == 2000
        assert call_kwargs["cursor"] == "c1"

    def test_api_error_raises(self, client, mock_session):
        mock_session.get_transaction_log.return_value = _error_response(10002, "Auth failed")

        with pytest.raises(Exception, match="Auth failed"):
            client.get_transaction_log(symbol="BTCUSDT")


# ---------------------------------------------------------------------------
# get_transaction_log_all (pagination + truncated flag)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_risk_limit
# ---------------------------------------------------------------------------


class TestGetRiskLimit:
    def test_returns_tier_list(self, client, mock_session):
        tiers = [
            {"id": 1, "symbol": "BTCUSDT", "riskLimitValue": "2000000",
             "maintenanceMargin": "0.005", "mmDeduction": "0"},
            {"id": 2, "symbol": "BTCUSDT", "riskLimitValue": "4000000",
             "maintenanceMargin": "0.01", "mmDeduction": "10000"},
        ]
        # Bybit API returns nested structure: outer list with inner "list" key per symbol
        mock_session.get_risk_limit.return_value = _ok_response({"list": [{"list": tiers}]})

        result = client.get_risk_limit(symbol="BTCUSDT")

        assert result == tiers
        mock_session.get_risk_limit.assert_called_once_with(
            category="linear", symbol="BTCUSDT"
        )

    def test_api_error_raises(self, client, mock_session):
        mock_session.get_risk_limit.return_value = _error_response(10001, "Invalid symbol")

        with pytest.raises(Exception, match="Invalid symbol"):
            client.get_risk_limit(symbol="INVALID")

    def test_network_error_raises(self, client, mock_session):
        mock_session.get_risk_limit.side_effect = ConnectionError("Network unreachable")

        with pytest.raises(ConnectionError, match="Network unreachable"):
            client.get_risk_limit(symbol="BTCUSDT")

    def test_empty_tier_list(self, client, mock_session):
        mock_session.get_risk_limit.return_value = _ok_response({"list": []})

        result = client.get_risk_limit(symbol="BTCUSDT")

        assert result == []

    def test_custom_category(self, client, mock_session):
        mock_session.get_risk_limit.return_value = _ok_response({"list": []})

        client.get_risk_limit(symbol="BTCUSDT", category="inverse")

        mock_session.get_risk_limit.assert_called_once_with(
            category="inverse", symbol="BTCUSDT"
        )

    def test_unexpected_structure_raises_value_error(self, client, mock_session):
        """Flat list without nested 'list' key raises ValueError."""
        tiers = [
            {"id": 1, "symbol": "BTCUSDT", "riskLimitValue": "2000000"},
            {"id": 2, "symbol": "BTCUSDT", "riskLimitValue": "4000000"},
        ]
        mock_session.get_risk_limit.return_value = _ok_response({"list": tiers})

        with pytest.raises(ValueError, match="Unexpected risk limit API structure"):
            client.get_risk_limit(symbol="BTCUSDT")

    def test_non_list_structure_raises_value_error(self, client, mock_session):
        mock_session.get_risk_limit.return_value = _ok_response({"list": {"unexpected": "shape"}})

        with pytest.raises(ValueError, match="expected list but got dict"):
            client.get_risk_limit(symbol="BTCUSDT")

    def test_non_list_inner_list_raises_value_error(self, client, mock_session):
        mock_session.get_risk_limit.return_value = _ok_response({"list": [{"list": "bad-inner"}]})

        with pytest.raises(ValueError, match="inner list is str"):
            client.get_risk_limit(symbol="BTCUSDT")


class TestGetTransactionLogAll:
    def test_single_page(self, client, mock_session):
        txns = [{"funding": "-0.01"}]
        mock_session.get_transaction_log.return_value = _ok_response(
            {"list": txns, "nextPageCursor": ""}
        )

        result, truncated = client.get_transaction_log_all(symbol="BTCUSDT")

        assert result == txns
        assert truncated is False
        assert mock_session.get_transaction_log.call_count == 1

    def test_multi_page_follows_cursor(self, client, mock_session):
        page1 = [{"funding": "-0.01"}]
        page2 = [{"funding": "-0.02"}]
        mock_session.get_transaction_log.side_effect = [
            _ok_response({"list": page1, "nextPageCursor": "cursor2"}),
            _ok_response({"list": page2, "nextPageCursor": ""}),
        ]

        result, truncated = client.get_transaction_log_all(symbol="BTCUSDT")

        assert len(result) == 2
        assert truncated is False
        assert mock_session.get_transaction_log.call_count == 2

    def test_stops_at_max_pages_and_returns_truncated(self, client, mock_session):
        mock_session.get_transaction_log.return_value = _ok_response(
            {"list": [{"funding": "-0.01"}], "nextPageCursor": "more"}
        )

        result, truncated = client.get_transaction_log_all(
            symbol="BTCUSDT", max_pages=3
        )

        assert len(result) == 3
        assert truncated is True
        assert mock_session.get_transaction_log.call_count == 3
