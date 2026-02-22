"""Tests for pnl_checker fetcher."""

from decimal import Decimal
from unittest.mock import MagicMock

from pnl_checker.fetcher import BybitFetcher


class TestBybitFetcher:
    """Test BybitFetcher data fetching logic."""

    def _make_client(self):
        """Create a mocked BybitRestClient."""
        client = MagicMock()
        # Default: positions returns one long position
        client.get_positions.return_value = [
            {
                "symbol": "BTCUSDT",
                "side": "Buy",
                "size": "0.01",
                "avgPrice": "50000",
                "markPrice": "51000",
                "liqPrice": "45000",
                "leverage": "10",
                "positionValue": "510",
                "positionIM": "51",
                "positionMM": "5.1",
                "unrealisedPnl": "10",
                "curRealisedPnl": "0",
                "cumRealisedPnl": "0",
                "positionIdx": 1,
            }
        ]
        # Default: ticker
        client.get_tickers.return_value = {
            "lastPrice": "51000",
            "markPrice": "51000",
            "fundingRate": "0.0001",
        }
        # Default: wallet
        client.get_wallet_balance.return_value = {
            "list": [
                {
                    "totalEquity": "10000",
                    "totalWalletBalance": "9980",
                    "totalMarginBalance": "10000",
                    "totalAvailableBalance": "9900",
                    "totalPerpUPL": "20",
                    "totalInitialMargin": "102",
                    "totalMaintenanceMargin": "10.2",
                    "coin": [
                        {
                            "coin": "USDT",
                            "walletBalance": "9980",
                            "unrealisedPnl": "20",
                            "cumRealisedPnl": "0",
                        }
                    ],
                }
            ]
        }
        # Default: funding transactions
        client.get_transaction_log_all.return_value = (
            [
                {"funding": "-0.01"},
                {"funding": "-0.02"},
            ],
            False,
        )
        return client

    def test_positions_fetched(self):
        client = self._make_client()
        fetcher = BybitFetcher(client)
        result = fetcher.fetch_all(["BTCUSDT"])

        assert len(result.symbols) == 1
        assert result.symbols[0].positions[0].symbol == "BTCUSDT"
        assert result.symbols[0].positions[0].size == Decimal("0.01")

    def test_empty_positions_skipped(self):
        client = self._make_client()
        client.get_positions.return_value = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0"}
        ]
        fetcher = BybitFetcher(client)
        result = fetcher.fetch_all(["BTCUSDT"])

        assert len(result.symbols) == 0

    def test_wallet_populated(self):
        client = self._make_client()
        fetcher = BybitFetcher(client)
        result = fetcher.fetch_all(["BTCUSDT"])

        assert result.wallet is not None
        assert result.wallet.total_equity == Decimal("10000")
        assert result.wallet.usdt_wallet_balance == Decimal("9980")

    def test_funding_summed(self):
        client = self._make_client()
        fetcher = BybitFetcher(client)
        result = fetcher.fetch_all(["BTCUSDT"])

        funding = result.symbols[0].funding
        assert funding.cumulative_funding == Decimal("-0.03")
        assert funding.transaction_count == 2

    def test_fetch_error_stored(self):
        client = self._make_client()
        client.get_transaction_log_all.side_effect = Exception("API timeout")
        fetcher = BybitFetcher(client)
        result = fetcher.fetch_all(["BTCUSDT"])

        funding = result.symbols[0].funding
        assert funding.fetch_error == "API timeout"
        assert funding.cumulative_funding == Decimal("0")

    def test_truncated_flag_set(self):
        client = self._make_client()
        client.get_transaction_log_all.return_value = (
            [{"funding": "-0.01"}],
            True,
        )
        fetcher = BybitFetcher(client)
        result = fetcher.fetch_all(["BTCUSDT"])

        assert result.symbols[0].funding.truncated is True

    def test_funding_max_pages_passed(self):
        client = self._make_client()
        fetcher = BybitFetcher(client, funding_max_pages=5)
        fetcher.fetch_all(["BTCUSDT"])

        client.get_transaction_log_all.assert_called_once_with(
            symbol="BTCUSDT",
            type="SETTLEMENT",
            max_pages=5,
        )
