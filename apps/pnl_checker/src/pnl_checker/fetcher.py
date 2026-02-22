"""Fetch position, ticker, wallet, and funding data from Bybit REST API.

Read-only: no orders placed, no bot execution.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from bybit_adapter.rest_client import BybitRestClient

logger = logging.getLogger(__name__)


@dataclass
class PositionData:
    """Raw position data from Bybit for one side (long or short)."""

    symbol: str
    side: str  # "Buy" (long) or "Sell" (short)
    size: Decimal
    avg_price: Decimal
    mark_price: Decimal
    liq_price: Decimal
    leverage: Decimal
    position_value: Decimal
    position_im: Decimal  # Initial margin
    position_mm: Decimal  # Maintenance margin
    unrealised_pnl: Decimal
    cur_realised_pnl: Decimal
    cum_realised_pnl: Decimal
    position_idx: int  # 1=long, 2=short in hedge mode

    @property
    def direction(self) -> str:
        return "long" if self.side == "Buy" else "short"


@dataclass
class TickerData:
    """Ticker data for a symbol."""

    symbol: str
    last_price: Decimal
    mark_price: Decimal
    funding_rate: Decimal


@dataclass
class WalletData:
    """Account-level wallet data."""

    total_equity: Decimal
    total_wallet_balance: Decimal
    total_margin_balance: Decimal
    total_available_balance: Decimal
    total_perp_upl: Decimal
    total_initial_margin: Decimal
    total_maintenance_margin: Decimal
    # USDT coin-level
    usdt_wallet_balance: Decimal
    usdt_unrealised_pnl: Decimal
    usdt_cum_realised_pnl: Decimal


@dataclass
class FundingData:
    """Cumulative funding data for a symbol."""

    symbol: str
    cumulative_funding: Decimal  # Sum of all funding payments
    transaction_count: int
    fetch_error: str | None = None
    truncated: bool = False


@dataclass
class SymbolFetchResult:
    """All fetched data for a single symbol."""

    symbol: str
    positions: list[PositionData]  # Up to 2 (long + short) in hedge mode
    ticker: TickerData
    funding: FundingData


@dataclass
class FetchResult:
    """All fetched data across all symbols."""

    symbols: list[SymbolFetchResult] = field(default_factory=list)
    wallet: Optional[WalletData] = None


class BybitFetcher:
    """Fetches live data from Bybit REST API for PnL validation."""

    def __init__(self, client: BybitRestClient, funding_max_pages: int = 20):
        self._client = client
        self._funding_max_pages = funding_max_pages

    def fetch_all(self, symbols: list[str]) -> FetchResult:
        """Fetch all data needed for PnL validation.

        Args:
            symbols: List of trading pair symbols (e.g., ["BTCUSDT", "ETHUSDT"])

        Returns:
            FetchResult with positions, tickers, wallet, and funding data
        """
        result = FetchResult()

        # Fetch wallet balance (account-level)
        try:
            result.wallet = self._fetch_wallet()
        except Exception as e:
            logger.warning(f"Failed to fetch wallet balance: {e}")

        # Fetch per-symbol data
        for symbol in symbols:
            symbol_result = self._fetch_symbol(symbol)
            if symbol_result.positions:  # Only include symbols with open positions
                result.symbols.append(symbol_result)
            else:
                logger.info(f"No open positions for {symbol}, skipping")

        return result

    def _fetch_symbol(self, symbol: str) -> SymbolFetchResult:
        """Fetch all data for a single symbol."""
        positions = self._fetch_positions(symbol)
        ticker = self._fetch_ticker(symbol)
        funding = self._fetch_funding(symbol)

        return SymbolFetchResult(
            symbol=symbol,
            positions=positions,
            ticker=ticker,
            funding=funding,
        )

    def _fetch_positions(self, symbol: str) -> list[PositionData]:
        """Fetch open positions for a symbol (hedge mode: up to 2)."""
        raw_positions = self._client.get_positions(symbol=symbol)
        positions = []

        for pos in raw_positions:
            size = Decimal(pos.get("size", "0"))
            if size == 0:
                continue

            positions.append(PositionData(
                symbol=pos.get("symbol", symbol),
                side=pos.get("side", ""),
                size=size,
                avg_price=Decimal(pos.get("avgPrice", "0")),
                mark_price=Decimal(pos.get("markPrice", "0")),
                liq_price=Decimal(pos.get("liqPrice", "0") or "0"),
                leverage=Decimal(pos.get("leverage", "1")),
                position_value=Decimal(pos.get("positionValue", "0")),
                position_im=Decimal(pos.get("positionIM", "0")),
                position_mm=Decimal(pos.get("positionMM", "0")),
                unrealised_pnl=Decimal(pos.get("unrealisedPnl", "0")),
                cur_realised_pnl=Decimal(pos.get("curRealisedPnl", "0")),
                cum_realised_pnl=Decimal(pos.get("cumRealisedPnl", "0")),
                position_idx=int(pos.get("positionIdx", 0)),
            ))

        logger.info(f"Fetched {len(positions)} open positions for {symbol}")
        return positions

    def _fetch_ticker(self, symbol: str) -> TickerData:
        """Fetch current ticker data for a symbol."""
        raw = self._client.get_tickers(symbol=symbol)

        return TickerData(
            symbol=symbol,
            last_price=Decimal(raw.get("lastPrice", "0")),
            mark_price=Decimal(raw.get("markPrice", "0")),
            funding_rate=Decimal(raw.get("fundingRate", "0")),
        )

    def _fetch_funding(self, symbol: str) -> FundingData:
        """Fetch cumulative funding fees from transaction log."""
        try:
            transactions, truncated = self._client.get_transaction_log_all(
                symbol=symbol,
                type="SETTLEMENT",
                max_pages=self._funding_max_pages,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch funding for {symbol}: {e}")
            return FundingData(
                symbol=symbol,
                cumulative_funding=Decimal("0"),
                transaction_count=0,
                fetch_error=str(e),
            )

        cumulative = Decimal("0")
        for tx in transactions:
            funding_str = tx.get("funding", "0")
            if funding_str:
                cumulative += Decimal(funding_str)

        logger.info(f"Fetched {len(transactions)} funding records for {symbol}, cumulative={cumulative}")
        return FundingData(
            symbol=symbol,
            cumulative_funding=cumulative,
            transaction_count=len(transactions),
            truncated=truncated,
        )

    def _fetch_wallet(self) -> WalletData:
        """Fetch account wallet balance."""
        raw = self._client.get_wallet_balance(account_type="UNIFIED")

        # Account-level fields
        account_list = raw.get("list", [])
        if not account_list:
            raise Exception("No wallet data returned")

        account = account_list[0]

        # Find USDT coin data
        usdt_data = {}
        for coin in account.get("coin", []):
            if coin.get("coin") == "USDT":
                usdt_data = coin
                break

        return WalletData(
            total_equity=Decimal(account.get("totalEquity", "0")),
            total_wallet_balance=Decimal(account.get("totalWalletBalance", "0")),
            total_margin_balance=Decimal(account.get("totalMarginBalance", "0")),
            total_available_balance=Decimal(account.get("totalAvailableBalance", "0")),
            total_perp_upl=Decimal(account.get("totalPerpUPL", "0")),
            total_initial_margin=Decimal(account.get("totalInitialMargin", "0")),
            total_maintenance_margin=Decimal(account.get("totalMaintenanceMargin", "0")),
            usdt_wallet_balance=Decimal(usdt_data.get("walletBalance", "0")),
            usdt_unrealised_pnl=Decimal(usdt_data.get("unrealisedPnl", "0")),
            usdt_cum_realised_pnl=Decimal(usdt_data.get("cumRealisedPnl", "0")),
        )
