"""REST API client for Bybit gap reconciliation.

This module provides REST API methods to fetch historical data for gap filling
when WebSocket connections are lost and reconnected.

Reference:
- Market Recent Trade: https://bybit-exchange.github.io/docs/v5/market/recent-trade
- Execution List: https://bybit-exchange.github.io/docs/v5/order/execution
- Order History: https://bybit-exchange.github.io/docs/v5/order/order-list
- Position List: https://bybit-exchange.github.io/docs/v5/position
- Wallet Balance: https://bybit-exchange.github.io/docs/v5/account/wallet-balance
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import logging

from pybit.unified_trading import HTTP


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

    _session: Optional[HTTP] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        """Initialize HTTP session."""
        self._session = HTTP(
            testnet=self.testnet,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )

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

        response = self._session.get_wallet_balance(accountType=account_type)
        self._check_response(response, "get_wallet_balance")

        result = response.get("result", {})
        logger.debug("Fetched wallet balance")
        return result

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
