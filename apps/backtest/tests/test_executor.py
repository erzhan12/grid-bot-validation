"""Tests for backtest executor."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gridcore import PlaceLimitIntent, CancelIntent

from backtest.executor import BacktestExecutor, OrderResult, CancelResult
from backtest.fill_simulator import TradeThroughFillSimulator
from backtest.order_manager import BacktestOrderManager


class TestBacktestExecutor:
    """Tests for BacktestExecutor."""

    @pytest.fixture
    def executor_with_qty_calc(self, order_manager):
        """Executor with custom qty calculator."""
        def qty_calculator(intent, wallet_balance):
            # Use 1% of wallet per order
            return wallet_balance * Decimal("0.01") / intent.price

        return BacktestExecutor(
            order_manager=order_manager,
            qty_calculator=qty_calculator,
        )

    def test_execute_place_success(self, executor, sample_timestamp):
        """Successfully place an order."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("99000"),
            qty=Decimal("0.1"),
            grid_level=1,
            direction="long",
        )

        result = executor.execute_place(intent, sample_timestamp)

        assert result.success is True
        assert result.order_id is not None
        assert result.error is None

    def test_execute_place_duplicate_rejected(self, executor, sample_timestamp):
        """Duplicate order is rejected."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("99000"),
            qty=Decimal("0.1"),
            grid_level=1,
            direction="long",
        )

        # First placement succeeds
        result1 = executor.execute_place(intent, sample_timestamp)
        assert result1.success is True

        # Second placement fails (duplicate)
        result2 = executor.execute_place(intent, sample_timestamp)
        assert result2.success is False
        assert "duplicate" in result2.error

    def test_execute_place_zero_qty_rejected(self, executor, sample_timestamp):
        """Order with zero qty is rejected."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("99000"),
            qty=Decimal("0"),
            grid_level=1,
            direction="long",
        )

        result = executor.execute_place(intent, sample_timestamp)

        assert result.success is False
        assert "qty" in result.error

    def test_execute_place_with_qty_calculator(self, executor_with_qty_calc, sample_timestamp):
        """Qty calculator is used when provided."""
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("999"),  # This should be overridden
            grid_level=1,
            direction="long",
        )

        wallet_balance = Decimal("10000")
        result = executor_with_qty_calc.execute_place(
            intent, sample_timestamp, wallet_balance
        )

        assert result.success is True

        # Verify qty was calculated: 10000 * 0.01 / 100000 = 0.001
        order = executor_with_qty_calc.order_manager.get_order_by_id(result.order_id)
        assert order.qty == Decimal("0.001")

    def test_execute_cancel_success(self, executor, sample_timestamp):
        """Successfully cancel an order."""
        # First place an order
        place_intent = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("99000"),
            qty=Decimal("0.1"),
            grid_level=1,
            direction="long",
        )
        place_result = executor.execute_place(place_intent, sample_timestamp)
        assert place_result.success is True

        # Now cancel it
        cancel_intent = CancelIntent(
            symbol="BTCUSDT",
            order_id=place_result.order_id,
            reason="test",
        )
        cancel_result = executor.execute_cancel(cancel_intent, sample_timestamp)

        assert cancel_result.success is True
        assert cancel_result.error is None

    def test_execute_cancel_not_found(self, executor, sample_timestamp):
        """Cancel non-existent order fails."""
        cancel_intent = CancelIntent(
            symbol="BTCUSDT",
            order_id="nonexistent_order",
            reason="test",
        )

        result = executor.execute_cancel(cancel_intent, sample_timestamp)

        assert result.success is False
        assert "not found" in result.error

    def test_execute_batch_mixed_intents(self, executor, sample_timestamp):
        """Execute batch of mixed place and cancel intents."""
        # Create place intents
        place1 = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("99000"),
            qty=Decimal("0.1"),
            grid_level=1,
            direction="long",
        )
        place2 = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Sell",
            price=Decimal("101000"),
            qty=Decimal("0.1"),
            grid_level=2,
            direction="long",
        )

        # Execute batch of placements
        place_results, cancel_results = executor.execute_batch(
            [place1, place2], sample_timestamp
        )

        assert len(place_results) == 2
        assert len(cancel_results) == 0
        assert all(r.success for r in place_results)

        # Now cancel one
        cancel = CancelIntent(
            symbol="BTCUSDT",
            order_id=place_results[0].order_id,
            reason="test",
        )

        place_results2, cancel_results2 = executor.execute_batch(
            [cancel], sample_timestamp
        )

        assert len(place_results2) == 0
        assert len(cancel_results2) == 1
        assert cancel_results2[0].success is True
