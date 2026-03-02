"""Tests for pnl_checker main entry point."""

from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from pnl_checker.main import main, cli
from pnl_checker.config import PnlCheckerConfig
from pnl_checker.fetcher import FetchResult, SymbolFetchResult, PositionData, TickerData, FundingData, WalletData
from pnl_checker.calculator import CalculationResult
from pnl_checker.comparator import ComparisonResult, PositionComparison, FieldComparison


def _make_config():
    return PnlCheckerConfig(
        account={"api_key": "test_key_0123456789", "api_secret": "test_secret_0123456789"},
        symbols=[{"symbol": "BTCUSDT", "tick_size": "0.1"}],
    )


def _make_fetch_result():
    return FetchResult(
        symbols=[
            SymbolFetchResult(
                symbol="BTCUSDT",
                positions=[
                    PositionData(
                        symbol="BTCUSDT", side="Buy", size=Decimal("0.01"),
                        avg_price=Decimal("50000"), mark_price=Decimal("51000"),
                        liq_price=Decimal("45000"), leverage=Decimal("10"),
                        position_value=Decimal("510"), position_im=Decimal("51"),
                        position_mm=Decimal("5.1"), unrealised_pnl=Decimal("10"),
                        cur_realised_pnl=Decimal("0"), cum_realised_pnl=Decimal("0"),
                        position_idx=1,
                    ),
                ],
                ticker=TickerData(
                    symbol="BTCUSDT", last_price=Decimal("51000"),
                    mark_price=Decimal("51000"), funding_rate=Decimal("0.0001"),
                ),
                funding=FundingData(
                    symbol="BTCUSDT", cumulative_funding=Decimal("-0.05"),
                    transaction_count=5,
                ),
            ),
        ],
        wallet=WalletData(
            total_equity=Decimal("10000"), total_wallet_balance=Decimal("9980"),
            total_margin_balance=Decimal("10000"), total_available_balance=Decimal("9900"),
            total_perp_upl=Decimal("20"), total_initial_margin=Decimal("102"),
            total_maintenance_margin=Decimal("10.2"),
            account_im_rate=Decimal("0.0102"), account_mm_rate=Decimal("0.00102"),
            margin_mode="REGULAR_MARGIN",
            usdt_wallet_balance=Decimal("9980"),
            usdt_unrealised_pnl=Decimal("20"), usdt_cum_realised_pnl=Decimal("0"),
        ),
    )


def _make_passing_comparison():
    return ComparisonResult(
        positions=[
            PositionComparison(
                symbol="BTCUSDT", direction="long",
                fields=[FieldComparison(
                    field_name="test", bybit_value=Decimal("10"),
                    our_value=Decimal("10"), delta=Decimal("0"), passed=True,
                )],
            ),
        ],
        tolerance=0.01,
    )


def _make_failing_comparison():
    return ComparisonResult(
        positions=[
            PositionComparison(
                symbol="BTCUSDT", direction="long",
                fields=[FieldComparison(
                    field_name="test", bybit_value=Decimal("10"),
                    our_value=Decimal("15"), delta=Decimal("5"), passed=False,
                )],
            ),
        ],
        tolerance=0.01,
    )


class TestMain:
    """Test main() entry point."""

    @patch("pnl_checker.main.save_json")
    @patch("pnl_checker.main.print_console")
    @patch("pnl_checker.main.compare")
    @patch("pnl_checker.main.calculate")
    @patch("pnl_checker.main.BybitFetcher")
    @patch("pnl_checker.main.BybitRestClient")
    @patch("pnl_checker.main.load_config")
    def test_config_not_found_returns_1(
        self, mock_load, mock_client, mock_fetcher, mock_calc, mock_compare,
        mock_print, mock_save
    ):
        mock_load.side_effect = FileNotFoundError("no config")
        assert main(config_path="/no/such/file") == 1

    @patch("pnl_checker.main.save_json")
    @patch("pnl_checker.main.print_console")
    @patch("pnl_checker.main.compare")
    @patch("pnl_checker.main.calculate")
    @patch("pnl_checker.main.BybitFetcher")
    @patch("pnl_checker.main.BybitRestClient")
    @patch("pnl_checker.main.load_config")
    def test_no_positions_returns_0(
        self, mock_load, mock_client, mock_fetcher, mock_calc, mock_compare,
        mock_print, mock_save
    ):
        mock_load.return_value = _make_config()
        mock_fetcher_inst = MagicMock()
        mock_fetcher.return_value = mock_fetcher_inst
        mock_fetcher_inst.fetch_all.return_value = FetchResult(symbols=[], wallet=None)

        assert main() == 0

    @patch("pnl_checker.main.save_json")
    @patch("pnl_checker.main.print_console")
    @patch("pnl_checker.main.compare")
    @patch("pnl_checker.main.calculate")
    @patch("pnl_checker.main.BybitFetcher")
    @patch("pnl_checker.main.BybitRestClient")
    @patch("pnl_checker.main.load_config")
    def test_all_pass_returns_0(
        self, mock_load, mock_client, mock_fetcher, mock_calc, mock_compare,
        mock_print, mock_save
    ):
        mock_load.return_value = _make_config()
        mock_fetcher_inst = MagicMock()
        mock_fetcher.return_value = mock_fetcher_inst
        mock_fetcher_inst.fetch_all.return_value = _make_fetch_result()
        mock_calc.return_value = CalculationResult()
        mock_compare.return_value = _make_passing_comparison()

        assert main() == 0

    @patch("pnl_checker.main.save_json")
    @patch("pnl_checker.main.print_console")
    @patch("pnl_checker.main.compare")
    @patch("pnl_checker.main.calculate")
    @patch("pnl_checker.main.BybitFetcher")
    @patch("pnl_checker.main.BybitRestClient")
    @patch("pnl_checker.main.load_config")
    def test_failures_returns_1(
        self, mock_load, mock_client, mock_fetcher, mock_calc, mock_compare,
        mock_print, mock_save
    ):
        mock_load.return_value = _make_config()
        mock_fetcher_inst = MagicMock()
        mock_fetcher.return_value = mock_fetcher_inst
        mock_fetcher_inst.fetch_all.return_value = _make_fetch_result()
        mock_calc.return_value = CalculationResult()
        mock_compare.return_value = _make_failing_comparison()

        assert main() == 1


class TestCli:
    """Test CLI argument validation."""

    def test_negative_tolerance_rejected(self, monkeypatch):
        """Negative tolerance should cause argparse error."""
        monkeypatch.setattr("sys.argv", ["pnl_checker", "--tolerance", "-0.5"])
        with pytest.raises(SystemExit, match="2"):
            cli()
