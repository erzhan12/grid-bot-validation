"""REST API client for Bybit trading and gap reconciliation.

This module provides REST API methods for:
- Order management (place, cancel, query)
- Historical data fetching for gap filling

Reference:
- Place Order: https://bybit-exchange.github.io/docs/v5/order/create-order
- Cancel Order: https://bybit-exchange.github.io/docs/v5/order/cancel-order
- Open Orders: https://bybit-exchange.github.io/docs/v5/order/open-order
- Market Recent Trade: https://bybit-exchange.github.io/docs/v5/market/recent-trade
- Execution List: https://bybit-exchange.github.io/docs/v5/order/execution
- Order History: https://bybit-exchange.github.io/docs/v5/order/order-list
- Position List: https://bybit-exchange.github.io/docs/v5/position
- Wallet Balance: https://bybit-exchange.github.io/docs/v5/account/wallet-balance
- Risk Limit: https://bybit-exchange.github.io/docs/v5/market/risk-limit
"""

import time
from dataclasses import dataclass, field
from typing import Optional
import logging

from pybit.unified_trading import HTTP

from bybit_adapter.rate_limiter import RateLimiter, RateLimitConfig, RequestType


logger = logging.getLogger(__name__)


@dataclass
class BybitRestClient:
    """REST API client for Bybit gap reconciliation.

    Provides methods to fetch historical data for filling gaps when
    WebSocket connections are lost. Used by the reconciler to ensure
    data completeness.

    Responsibilities:
    - Fetch public trades for gap filling
    - Fetch private executions for gap filling
    - Fetch order history
    - Fetch position snapshots
    - Fetch wallet balance
    - Handle pagination for large result sets

    Example:
        client = BybitRestClient(
            api_key="xxx",
            api_secret="yyy",
            testnet=True,
        )

        # Fetch recent trades
        trades = client.get_recent_trades("BTCUSDT", limit=1000)

        # Fetch executions in time range
        executions = client.get_executions(
            start_time=1704639600000,
            end_time=1704640000000,
        )
    """

    api_key: str
    api_secret: str
    testnet: bool = True
    rate_limit_config: RateLimitConfig = field(default_factory=lambda: RateLimitConfig(query_rate=10))

    _session: Optional[HTTP] = field(default=None, init=False, repr=False)
    _rate_limiter: RateLimiter = field(default=None, init=False, repr=False)

    def __post_init__(self):
        """Initialize HTTP session and rate limiter."""
        self._session = HTTP(
            testnet=self.testnet,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        self._rate_limiter = RateLimiter(config=self.rate_limit_config)

    def get_rate_limit_status(self) -> dict[str, int | float]:
        """Return current rate limit status for debugging/monitoring.

        Returns:
            Dict with available capacity per request type and backoff remaining.
        """
        return {
            "query_available": self._rate_limiter.get_available_capacity("query"),
            "order_available": self._rate_limiter.get_available_capacity("order"),
            "backoff_remaining": self._rate_limiter.get_backoff_remaining(),
        }

    def _wait_for_rate_limit(self, request_type: RequestType = "query") -> None:
        """Block until a request slot is available, then record the request."""
        wait = self._rate_limiter.wait_time(request_type)
        if wait > 0:
            logger.debug(f"Rate limit: waiting {wait:.3f}s before {request_type} request")
            time.sleep(wait)
        self._rate_limiter.record_request(request_type)

    def get_recent_trades(
        self,
        symbol: str,
        limit: int = 1000,
    ) -> list[dict]:
        """Fetch recent public trades for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            limit: Maximum number of trades to return (max 1000)

        Returns:
            List of trade dicts with keys: execId, symbol, price, size, side, time

        Raises:
            Exception: If API call fails
        """
        logger.debug(f"Fetching recent trades for {symbol}, limit={limit}")
        self._wait_for_rate_limit("query")

        response = self._session.get_public_trade_history(
            category="linear",
            symbol=symbol,
            limit=min(limit, 1000),
        )

        self._check_response(response, "get_recent_trades")
        trades = response.get("result", {}).get("list", [])

        logger.debug(f"Fetched {len(trades)} trades for {symbol}")
        return trades

    def get_executions(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """Fetch private execution history.

        Args:
            symbol: Filter by symbol (optional)
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional)
            limit: Maximum results per page (max 100)
            cursor: Pagination cursor from previous call

        Returns:
            Tuple of (executions list, next_cursor or None)

        Raises:
            Exception: If API call fails
        """
        logger.debug(f"Fetching executions for {symbol}, start={start_time}, end={end_time}")
        self._wait_for_rate_limit("query")

        params = {
            "category": "linear",
            "limit": min(limit, 100),
        }
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if cursor:
            params["cursor"] = cursor

        response = self._session.get_executions(**params)
        self._check_response(response, "get_executions")

        result = response.get("result", {})
        executions = result.get("list", [])
        next_cursor = result.get("nextPageCursor")

        logger.debug(f"Fetched {len(executions)} executions, has_more={bool(next_cursor)}")
        return executions, next_cursor if next_cursor else None

    def get_executions_all(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        max_pages: int = 10,
    ) -> list[dict]:
        """Fetch all executions with automatic pagination.

        Args:
            symbol: Filter by symbol (optional)
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional)
            max_pages: Maximum number of pages to fetch (safety limit)

        Returns:
            List of all executions across pages

        Raises:
            Exception: If API call fails
        """
        all_executions = []
        cursor = None
        page = 0

        while page < max_pages:
            executions, cursor = self.get_executions(
                symbol=symbol,
                start_time=start_time,
                end_time=end_time,
                cursor=cursor,
            )
            all_executions.extend(executions)
            page += 1

            if not cursor:
                break

        logger.info(f"Fetched {len(all_executions)} total executions across {page} pages")
        return all_executions

    def get_order_history(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """Fetch order history.

        Args:
            symbol: Filter by symbol (optional)
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional)
            limit: Maximum results per page (max 50)
            cursor: Pagination cursor from previous call

        Returns:
            Tuple of (orders list, next_cursor or None)

        Raises:
            Exception: If API call fails
        """
        logger.debug(f"Fetching order history for {symbol}")
        self._wait_for_rate_limit("query")

        params = {
            "category": "linear",
            "limit": min(limit, 50),
        }
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if cursor:
            params["cursor"] = cursor

        response = self._session.get_order_history(**params)
        self._check_response(response, "get_order_history")

        result = response.get("result", {})
        orders = result.get("list", [])
        next_cursor = result.get("nextPageCursor")

        logger.debug(f"Fetched {len(orders)} orders, has_more={bool(next_cursor)}")
        return orders, next_cursor if next_cursor else None

    def get_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """Fetch current positions.

        Args:
            symbol: Filter by symbol (optional, returns all if not specified)

        Returns:
            List of position dicts

        Raises:
            Exception: If API call fails
        """
        logger.debug(f"Fetching positions for {symbol or 'all symbols'}")
        self._wait_for_rate_limit("query")

        params = {
            "category": "linear",
            "settleCoin": "USDT",
        }
        if symbol:
            params["symbol"] = symbol

        response = self._session.get_positions(**params)
        self._check_response(response, "get_positions")

        positions = response.get("result", {}).get("list", [])
        logger.debug(f"Fetched {len(positions)} positions")
        return positions

    def get_wallet_balance(self, account_type: str = "UNIFIED") -> dict:
        """Fetch wallet balance.

        Args:
            account_type: Account type (default "UNIFIED")

        Returns:
            Wallet balance dict with coin balances

        Raises:
            Exception: If API call fails
        """
        logger.debug(f"Fetching wallet balance for {account_type}")
        self._wait_for_rate_limit("query")

        response = self._session.get_wallet_balance(accountType=account_type)
        self._check_response(response, "get_wallet_balance")

        result = response.get("result", {})
        logger.debug("Fetched wallet balance")
        return result

    def get_account_info(self) -> dict:
        """Fetch account info (margin mode, etc.).

        Returns:
            Account info dict with marginMode, unifiedMarginStatus, etc.

        Raises:
            Exception: If API call fails
        """
        logger.debug("Fetching account info")
        self._wait_for_rate_limit("query")

        response = self._session.get_account_info()
        self._check_response(response, "get_account_info")

        result = response.get("result", {})
        logger.debug(f"Fetched account info: marginMode={result.get('marginMode')}")
        return result

    def get_risk_limit(self, symbol: str, category: str = "linear") -> list[dict]:
        """Fetch risk limit tiers for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            category: Product type (default "linear")

        Returns:
            List of tier dicts. Each dict contains:
              - ``riskLimitValue`` (str): Max position value for this tier (e.g. "200000")
              - ``maintenanceMargin`` (str): MMR rate as decimal string (e.g. "0.005")
              - ``mmDeduction`` (str): Deduction amount (e.g. "0", may be "" for tier 0)
              - ``initialMargin`` (str): IMR rate as decimal string (e.g. "0.01")

        Raises:
            Exception: If API call fails

        Reference:
            https://bybit-exchange.github.io/docs/v5/market/risk-limit
        """
        logger.debug(f"Fetching risk limit tiers for {symbol}")
        self._wait_for_rate_limit("query")

        response = self._session.get_risk_limit(category=category, symbol=symbol)
        self._check_response(response, "get_risk_limit")

        # Bybit V5 risk-limit endpoint returns nested structure:
        # {"result": {"list": [{"list": [tier_data, ...]}, ...]}}
        # where outer list contains one entry per symbol, each with an inner "list" of tiers.
        # We unwrap the first symbol's inner tier list since we query one symbol at a time.
        outer_list = response.get("result", {}).get("list", [])
        if outer_list and isinstance(outer_list[0], dict) and "list" in outer_list[0]:
            tiers = outer_list[0].get("list", [])
        else:
            tiers = outer_list
        logger.debug(f"Fetched {len(tiers)} risk limit tiers for {symbol}")
        return tiers

    # -------------------------------------------------------------------------
    # Order Management Methods
    # -------------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        qty: str,
        price: Optional[str] = None,
        reduce_only: bool = False,
        position_idx: int = 0,
        order_link_id: Optional[str] = None,
    ) -> dict:
        """Place a new order.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            side: Order side ("Buy" or "Sell")
            order_type: Order type ("Limit" or "Market")
            qty: Order quantity as string
            price: Limit price as string (required for Limit orders)
            reduce_only: Whether this is a reduce-only order
            position_idx: Position index for hedge mode (0=one-way, 1=buy-side, 2=sell-side)
            order_link_id: Custom order ID for tracking (client_order_id)

        Returns:
            Order response dict with keys: orderId, orderLinkId, etc.

        Raises:
            Exception: If API call fails

        Reference:
            bbu_reference/bbu2-master/bybit_api_usdt.py:315-329
        """
        logger.info(f"Placing {order_type} {side} order: {symbol} qty={qty} price={price}")
        self._wait_for_rate_limit("order")

        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "reduceOnly": reduce_only,
            "positionIdx": position_idx,
        }
        if price is not None:
            params["price"] = price
        if order_link_id is not None:
            params["orderLinkId"] = order_link_id

        response = self._session.place_order(**params)
        self._check_response(response, "place_order")

        result = response.get("result", {})
        order_id = result.get("orderId", "")
        logger.info(f"Order placed successfully: {order_id}")
        return result

    def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
    ) -> bool:
        """Cancel an existing order.

        Either order_id or order_link_id must be provided.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            order_id: Exchange order ID
            order_link_id: Custom order ID (client_order_id)

        Returns:
            True if cancellation succeeded

        Raises:
            Exception: If API call fails
            ValueError: If neither order_id nor order_link_id provided

        Reference:
            bbu_reference/bbu2-master/bybit_api_usdt.py:442-448
        """
        if order_id is None and order_link_id is None:
            raise ValueError("Either order_id or order_link_id must be provided")

        logger.info(f"Canceling order: {symbol} order_id={order_id} order_link_id={order_link_id}")
        self._wait_for_rate_limit("order")

        params = {
            "category": "linear",
            "symbol": symbol,
        }
        if order_id is not None:
            params["orderId"] = order_id
        if order_link_id is not None:
            params["orderLinkId"] = order_link_id

        try:
            response = self._session.cancel_order(**params)
            self._check_response(response, "cancel_order")
            logger.info("Order cancelled successfully")
            return True
        except Exception as e:
            err_msg = str(e).lower()
            # Expected: order already filled, cancelled, or not found
            if any(phrase in err_msg for phrase in ("not found", "not exist", "already filled", "already cancelled")):
                logger.warning(f"Cancel order failed (expected): {e}")
            else:
                logger.error(f"Cancel order failed (unexpected): {e}")
            return False

    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")

        Returns:
            Number of orders cancelled (from API response)

        Raises:
            Exception: If API call fails

        Reference:
            bbu_reference/bbu2-master/bybit_api_usdt.py:450-452
        """
        logger.info(f"Canceling all orders for {symbol}")
        self._wait_for_rate_limit("order")

        response = self._session.cancel_all_orders(
            category="linear",
            symbol=symbol,
        )
        self._check_response(response, "cancel_all_orders")

        # V5 API returns list of cancelled orders
        result = response.get("result", {})
        cancelled = result.get("list", [])
        logger.info(f"Cancelled {len(cancelled)} orders")
        return len(cancelled)

    def get_open_orders(
        self,
        symbol: Optional[str] = None,
        order_type: str = "Limit",
        limit: int = 50,
        max_pages: int = 10,
    ) -> list[dict]:
        """Fetch all open orders with pagination.

        Args:
            symbol: Filter by symbol (optional)
            order_type: Filter by order type (default "Limit")
            limit: Results per page (max 50)
            max_pages: Maximum number of pages to fetch (safety limit)

        Returns:
            List of open order dicts

        Raises:
            Exception: If API call fails

        Reference:
            bbu_reference/bbu2-master/bybit_api_usdt.py:380-404
        """
        logger.debug(f"Fetching open orders for {symbol or 'all symbols'}")

        all_orders = []
        # Note: _wait_for_rate_limit is called inside the loop before each page request
        cursor = None
        page = 0

        while page < max_pages:
            self._wait_for_rate_limit("query")
            params = {
                "category": "linear",
                "limit": min(limit, 50),
            }
            if symbol:
                params["symbol"] = symbol
            if cursor:
                params["cursor"] = cursor

            response = self._session.get_open_orders(**params)
            self._check_response(response, "get_open_orders")

            result = response.get("result", {})
            orders = result.get("list", [])

            if not orders:
                break

            # Filter by order type
            filtered = [o for o in orders if o.get("orderType") == order_type]
            all_orders.extend(filtered)

            cursor = result.get("nextPageCursor")
            page += 1
            if not cursor:
                break

        if page >= max_pages and cursor:
            logger.warning(f"get_open_orders reached max_pages={max_pages} with more data available")

        logger.debug(f"Fetched {len(all_orders)} open {order_type} orders across {page} pages")
        return all_orders

    def get_tickers(self, symbol: str) -> dict:
        """Fetch ticker data for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")

        Returns:
            Ticker dict with lastPrice, markPrice, fundingRate, etc.

        Raises:
            Exception: If API call fails

        Reference:
            https://bybit-exchange.github.io/docs/v5/market/tickers
        """
        logger.debug(f"Fetching tickers for {symbol}")
        self._wait_for_rate_limit("query")

        response = self._session.get_tickers(
            category="linear",
            symbol=symbol,
        )
        self._check_response(response, "get_tickers")

        tickers = response.get("result", {}).get("list", [])
        if not tickers:
            raise Exception(f"No ticker data returned for {symbol}")

        logger.debug(f"Fetched ticker for {symbol}")
        return tickers[0]

    def get_transaction_log(
        self,
        symbol: Optional[str] = None,
        type: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """Fetch transaction log (for funding fees, settlements, etc.).

        Args:
            symbol: Filter by symbol (optional)
            type: Transaction type filter (e.g., "SETTLEMENT" for funding)
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional)
            limit: Maximum results per page (max 50)
            cursor: Pagination cursor from previous call

        Returns:
            Tuple of (transactions list, next_cursor or None)

        Raises:
            Exception: If API call fails

        Reference:
            https://bybit-exchange.github.io/docs/v5/account/transaction-log
        """
        logger.debug(f"Fetching transaction log for {symbol}, type={type}")
        self._wait_for_rate_limit("query")

        params = {
            "accountType": "UNIFIED",
            "category": "linear",
            "limit": min(limit, 50),
        }
        if symbol:
            params["symbol"] = symbol
        if type:
            params["type"] = type
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if cursor:
            params["cursor"] = cursor

        response = self._session.get_transaction_log(**params)
        self._check_response(response, "get_transaction_log")

        result = response.get("result", {})
        transactions = result.get("list", [])
        next_cursor = result.get("nextPageCursor")

        logger.debug(f"Fetched {len(transactions)} transactions, has_more={bool(next_cursor)}")
        return transactions, next_cursor if next_cursor else None

    def get_transaction_log_all(
        self,
        symbol: Optional[str] = None,
        type: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        max_pages: int = 20,
    ) -> tuple[list[dict], bool]:
        """Fetch all transaction log entries with automatic pagination.

        Args:
            symbol: Filter by symbol (optional)
            type: Transaction type filter (e.g., "SETTLEMENT")
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional)
            max_pages: Maximum number of pages to fetch (safety limit)

        Returns:
            Tuple of (all transactions, truncated flag).
            truncated is True when max_pages was reached but more data exists.

        Note:
            If resumable pagination is needed in the future, consider
            returning the final cursor as a third element so callers
            can continue where this call left off.
        """
        all_transactions = []
        cursor = None
        page = 0

        while page < max_pages:
            transactions, cursor = self.get_transaction_log(
                symbol=symbol,
                type=type,
                start_time=start_time,
                end_time=end_time,
                cursor=cursor,
            )
            all_transactions.extend(transactions)
            page += 1

            if not cursor:
                break

        truncated = page >= max_pages and cursor is not None
        logger.info(f"Fetched {len(all_transactions)} total transactions across {page} pages (truncated={truncated})")
        return all_transactions, truncated

    def _check_response(self, response: dict, method: str) -> None:
        """Check API response for errors.

        Args:
            response: API response dict
            method: Method name for error logging

        Raises:
            Exception: If response indicates an error
        """
        ret_code = response.get("retCode", -1)
        if ret_code != 0:
            ret_msg = response.get("retMsg", "Unknown error")
            error_msg = f"Bybit API error in {method}: [{ret_code}] {ret_msg}"
            logger.error(error_msg)
            raise Exception(error_msg)
