"""Tests for PnL comparator."""

from decimal import Decimal

from pnl_checker.comparator import (
    _compare_field,
    _info_field,
    compare,
)
from pnl_checker.fetcher import (
    FetchResult,
    SymbolFetchResult,
    PositionData,
    TickerData,
    FundingData,
    WalletData,
)
from pnl_checker.calculator import CalculationResult, PositionCalcResult


class TestCompareField:
    """Test individual field comparison."""

    def test_within_tolerance(self):
        """Values within tolerance should pass."""
        result = _compare_field("test", Decimal("10.005"), Decimal("10.006"), 0.01)
        assert result.passed is True
        assert result.delta == Decimal("0.001")

    def test_exceeds_tolerance(self):
        """Values exceeding tolerance should fail."""
        result = _compare_field("test", Decimal("10.0"), Decimal("10.02"), 0.01)
        assert result.passed is False

    def test_exact_match(self):
        """Exact match should pass."""
        result = _compare_field("test", Decimal("100"), Decimal("100"), 0.01)
        assert result.passed is True
        assert result.delta == Decimal("0")

    def test_at_boundary(self):
        """Value exactly at tolerance should pass."""
        result = _compare_field("test", Decimal("100"), Decimal("100.01"), 0.01)
        assert result.passed is True


class TestInfoField:
    """Test informational fields."""

    def test_info_field_no_check(self):
        result = _info_field("test", Decimal("123"))
        assert result.passed is None
        assert result.delta == "—"


class TestCompare:
    """Test full comparison pipeline."""

    def _make_test_data(self):
        """Create matching fetch and calc results."""
        pos = PositionData(
            symbol="BTCUSDT",
            side="Buy",
            size=Decimal("0.01"),
            avg_price=Decimal("50000"),
            mark_price=Decimal("51000"),
            liq_price=Decimal("45000"),
            leverage=Decimal("10"),
            position_value=Decimal("510"),
            position_im=Decimal("51"),
            position_mm=Decimal("5.1"),
            unrealised_pnl=Decimal("10"),
            cur_realised_pnl=Decimal("0"),
            cum_realised_pnl=Decimal("0"),
            position_idx=1,
        )

        fetch = FetchResult(
            symbols=[
                SymbolFetchResult(
                    symbol="BTCUSDT",
                    positions=[pos],
                    ticker=TickerData(
                        symbol="BTCUSDT",
                        last_price=Decimal("51000"),
                        mark_price=Decimal("51000"),
                        funding_rate=Decimal("0.0001"),
                    ),
                    funding=FundingData(
                        symbol="BTCUSDT",
                        cumulative_funding=Decimal("-0.05"),
                        transaction_count=5,
                    ),
                ),
            ],
            wallet=WalletData(
                total_equity=Decimal("10000"),
                total_wallet_balance=Decimal("9980"),
                total_margin_balance=Decimal("10000"),
                total_available_balance=Decimal("9900"),
                total_perp_upl=Decimal("20"),
                total_initial_margin=Decimal("102"),
                total_maintenance_margin=Decimal("10.2"),
                usdt_wallet_balance=Decimal("9980"),
                usdt_unrealised_pnl=Decimal("20"),
                usdt_cum_realised_pnl=Decimal("0"),
            ),
        )

        calc = CalculationResult(positions=[
            PositionCalcResult(
                symbol="BTCUSDT",
                direction="long",
                unrealised_pnl_mark=Decimal("10"),
                unrealised_pnl_last=Decimal("10"),
                unrealised_pnl_pct_bbu2_mark=Decimal("19.6"),
                unrealised_pnl_pct_bbu2_last=Decimal("19.6"),
                unrealised_pnl_pct_bybit=Decimal("19.6"),
                position_value_mark=Decimal("510"),
                initial_margin=Decimal("51"),
                liq_ratio=0.8824,
                funding_snapshot=Decimal("0.0051"),
                buy_multiplier=1.0,
                sell_multiplier=1.0,
                risk_rule_triggered="none",
            ),
        ])

        return fetch, calc

    def test_all_pass_when_matching(self):
        """All checked fields should pass when values match."""
        fetch, calc = self._make_test_data()
        result = compare(fetch, calc, tolerance=0.01)
        assert result.all_passed is True
        assert result.total_fail == 0

    def test_fail_on_large_delta(self):
        """Should fail when delta exceeds tolerance."""
        fetch, calc = self._make_test_data()
        # Make our PnL calculation way off
        calc.positions[0].unrealised_pnl_mark = Decimal("15")
        result = compare(fetch, calc, tolerance=0.01)
        assert result.total_fail > 0
        assert not result.all_passed

    def test_account_summary_populated(self):
        """Account summary should be populated from wallet data."""
        fetch, calc = self._make_test_data()
        result = compare(fetch, calc, tolerance=0.01)
        assert len(result.account.fields) > 0

    def test_position_comparison_has_fields(self):
        """Position comparison should have all expected fields."""
        fetch, calc = self._make_test_data()
        result = compare(fetch, calc, tolerance=0.01)
        assert len(result.positions) == 1
        pos_comp = result.positions[0]
        field_names = [f.field_name for f in pos_comp.fields]
        assert "Unrealized PnL (mark)" in field_names
        assert "Position Value" in field_names
        assert "Initial Margin" in field_names

    def test_funding_appears_once_for_hedge_mode_symbol(self):
        """Funding field should appear exactly once across long+short positions."""
        long_pos = PositionData(
            symbol="BTCUSDT", side="Buy", size=Decimal("0.01"),
            avg_price=Decimal("50000"), mark_price=Decimal("51000"),
            liq_price=Decimal("45000"), leverage=Decimal("10"),
            position_value=Decimal("510"), position_im=Decimal("51"),
            position_mm=Decimal("5.1"), unrealised_pnl=Decimal("10"),
            cur_realised_pnl=Decimal("0"), cum_realised_pnl=Decimal("0"),
            position_idx=1,
        )
        short_pos = PositionData(
            symbol="BTCUSDT", side="Sell", size=Decimal("0.01"),
            avg_price=Decimal("52000"), mark_price=Decimal("51000"),
            liq_price=Decimal("58000"), leverage=Decimal("10"),
            position_value=Decimal("510"), position_im=Decimal("51"),
            position_mm=Decimal("5.1"), unrealised_pnl=Decimal("10"),
            cur_realised_pnl=Decimal("0"), cum_realised_pnl=Decimal("0"),
            position_idx=2,
        )

        fetch = FetchResult(
            symbols=[
                SymbolFetchResult(
                    symbol="BTCUSDT",
                    positions=[long_pos, short_pos],
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
        )

        calc = CalculationResult(positions=[
            PositionCalcResult(
                symbol="BTCUSDT", direction="long",
                unrealised_pnl_mark=Decimal("10"), unrealised_pnl_last=Decimal("10"),
                unrealised_pnl_pct_bbu2_mark=Decimal("19.6"),
                unrealised_pnl_pct_bbu2_last=Decimal("19.6"),
                unrealised_pnl_pct_bybit=Decimal("19.6"),
                position_value_mark=Decimal("510"), initial_margin=Decimal("51"),
                liq_ratio=0.8824, funding_snapshot=Decimal("0.0051"),
            ),
            PositionCalcResult(
                symbol="BTCUSDT", direction="short",
                unrealised_pnl_mark=Decimal("10"), unrealised_pnl_last=Decimal("10"),
                unrealised_pnl_pct_bbu2_mark=Decimal("19.6"),
                unrealised_pnl_pct_bbu2_last=Decimal("19.6"),
                unrealised_pnl_pct_bybit=Decimal("19.6"),
                position_value_mark=Decimal("510"), initial_margin=Decimal("51"),
                liq_ratio=1.1373, funding_snapshot=Decimal("0.0051"),
            ),
        ])

        result = compare(fetch, calc, tolerance=0.01)

        # Count how many positions have funding fields
        funding_count = 0
        for pos in result.positions:
            for f in pos.fields:
                if f.field_name == "Cum Funding (from tx log)":
                    funding_count += 1

        assert funding_count == 1  # Only on the first position

    def test_missing_calc_creates_fail_entry(self):
        """When calc result is missing for a position, it should create a fail entry."""
        fetch, _ = self._make_test_data()
        # Empty calc result — no matching calculation
        calc = CalculationResult(positions=[])

        result = compare(fetch, calc, tolerance=0.01)

        assert result.all_passed is False
        assert len(result.positions) == 1
        field_names = [f.field_name for f in result.positions[0].fields]
        assert "Calculation Missing" in field_names

    def test_funding_error_causes_fail(self):
        """When fetch_error is set on funding, comparison should fail."""
        fetch, calc = self._make_test_data()
        # Set a fetch error on the funding data
        fetch.symbols[0].funding.fetch_error = "API timeout"

        result = compare(fetch, calc, tolerance=0.01)

        assert result.all_passed is False
        field_names = [f.field_name for f in result.positions[0].fields]
        assert "Funding Fetch Error" in field_names

    def test_truncated_funding_causes_fail(self):
        """When funding data is truncated, comparison should fail."""
        fetch, calc = self._make_test_data()
        fetch.symbols[0].funding.truncated = True

        result = compare(fetch, calc, tolerance=0.01)

        assert result.all_passed is False
        field_names = [f.field_name for f in result.positions[0].fields]
        assert "Funding Data Truncated" in field_names
