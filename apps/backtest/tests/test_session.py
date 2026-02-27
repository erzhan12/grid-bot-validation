"""Tests for backtest session."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest.session import BacktestSession, BacktestTrade, BacktestMetrics


class TestBacktestSession:
    """Tests for BacktestSession."""

    def test_init(self, session):
        """Session initializes correctly."""
        assert session.initial_balance == Decimal("10000")
        assert session.current_balance == Decimal("10000")
        assert len(session.trades) == 0
        assert len(session.equity_curve) == 0
        assert session.metrics is None

    def test_record_trade(self, session, sample_timestamp):
        """Recording trade updates session state."""
        trade = BacktestTrade(
            trade_id="t1",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100000"),
            qty=Decimal("0.1"),
            direction="long",
            timestamp=sample_timestamp,
            order_id="o1",
            client_order_id="c1",
            realized_pnl=Decimal("100"),
            commission=Decimal("2"),
        )

        session.record_trade(trade)

        assert len(session.trades) == 1
        assert session.total_realized_pnl == Decimal("100")
        assert session.total_commission == Decimal("2")

    def test_record_funding(self, session):
        """Recording funding updates session state."""
        session.record_funding(Decimal("-5"))  # Paid 5
        session.record_funding(Decimal("3"))   # Received 3

        assert session.total_funding == Decimal("-2")

    def test_update_equity(self, session, sample_timestamp):
        """Update equity records point and tracks drawdown."""
        session.update_equity(sample_timestamp, Decimal("0"))

        assert len(session.equity_curve) == 1
        assert session.equity_curve[0][1] == Decimal("10000")

    def test_update_equity_with_pnl(self, session, sample_timestamp):
        """Equity includes PnL components."""
        session.total_realized_pnl = Decimal("100")
        session.total_commission = Decimal("10")
        session.total_funding = Decimal("-5")

        equity = session.update_equity(sample_timestamp, Decimal("50"))  # Unrealized

        # 10000 + 100 + 50 - 10 - 5 = 10135
        assert equity == Decimal("10135")
        assert session.current_balance == Decimal("10135")

    def test_drawdown_tracking(self, session, sample_timestamp):
        """Drawdown is tracked correctly."""
        # Initial equity (peak)
        session.update_equity(sample_timestamp, Decimal("0"))

        # Increase (new peak)
        session.total_realized_pnl = Decimal("500")
        session.update_equity(sample_timestamp, Decimal("0"))
        assert session._peak_equity == Decimal("10500")

        # Drawdown
        session.total_realized_pnl = Decimal("300")  # Lost 200
        session.update_equity(sample_timestamp, Decimal("0"))
        assert session._max_drawdown == Decimal("200")

    def test_finalize_metrics(self, session, sample_timestamp):
        """Finalize calculates all metrics."""
        # Record some trades
        session.record_trade(BacktestTrade(
            trade_id="t1", symbol="BTCUSDT", side="Buy",
            price=Decimal("100000"), qty=Decimal("0.1"), direction="long",
            timestamp=sample_timestamp, order_id="o1", client_order_id="c1",
            realized_pnl=Decimal("100"), commission=Decimal("2"),
        ))
        session.record_trade(BacktestTrade(
            trade_id="t2", symbol="BTCUSDT", side="Sell",
            price=Decimal("99000"), qty=Decimal("0.1"), direction="long",
            timestamp=sample_timestamp, order_id="o2", client_order_id="c2",
            realized_pnl=Decimal("-50"), commission=Decimal("2"),
        ))

        metrics = session.finalize(final_unrealized_pnl=Decimal("25"))

        assert metrics.total_trades == 2
        assert metrics.winning_trades == 1
        assert metrics.losing_trades == 1
        assert metrics.win_rate == 0.5
        assert metrics.total_realized_pnl == Decimal("50")
        assert metrics.total_unrealized_pnl == Decimal("25")
        assert metrics.total_commission == Decimal("4")

    def test_finalize_profit_factor(self, session, sample_timestamp):
        """Profit factor calculated correctly."""
        session.record_trade(BacktestTrade(
            trade_id="t1", symbol="BTCUSDT", side="Buy",
            price=Decimal("100000"), qty=Decimal("0.1"), direction="long",
            timestamp=sample_timestamp, order_id="o1", client_order_id="c1",
            realized_pnl=Decimal("200"), commission=Decimal("0"),
        ))
        session.record_trade(BacktestTrade(
            trade_id="t2", symbol="BTCUSDT", side="Sell",
            price=Decimal("99000"), qty=Decimal("0.1"), direction="long",
            timestamp=sample_timestamp, order_id="o2", client_order_id="c2",
            realized_pnl=Decimal("-100"), commission=Decimal("0"),
        ))

        metrics = session.finalize()

        # Profit factor = gross_profit / gross_loss = 200 / 100 = 2.0
        assert metrics.profit_factor == 2.0

    def test_finalize_return_pct(self, session, sample_timestamp):
        """Return percentage calculated correctly."""
        session.total_realized_pnl = Decimal("1000")

        metrics = session.finalize()

        # Return = 1000 / 10000 * 100 = 10%
        assert metrics.return_pct == 10.0

    def test_get_summary(self, session, sample_timestamp):
        """Summary string generated."""
        session.record_trade(BacktestTrade(
            trade_id="t1", symbol="BTCUSDT", side="Buy",
            price=Decimal("100000"), qty=Decimal("0.1"), direction="long",
            timestamp=sample_timestamp, order_id="o1", client_order_id="c1",
            realized_pnl=Decimal("100"), commission=Decimal("2"),
        ))

        summary = session.get_summary()

        assert "Backtest Results" in summary
        assert "Trades: 1" in summary
        assert "Win Rate" in summary
        assert "Margin:" in summary
        assert "Peak IM:" in summary
        assert "Peak MM:" in summary


class TestSessionMarginTracking:
    """Tests for margin peak tracking in BacktestSession."""

    def test_margin_peaks_tracked(self, session, sample_timestamp):
        """Peak IM and MM tracked across equity updates."""
        t = sample_timestamp

        session.update_equity(t, Decimal("0"), total_im=Decimal("100"), total_mm=Decimal("10"))
        session.update_equity(t, Decimal("0"), total_im=Decimal("200"), total_mm=Decimal("20"))
        session.update_equity(t, Decimal("0"), total_im=Decimal("150"), total_mm=Decimal("15"))

        assert session._peak_im == Decimal("200")
        assert session._peak_mm == Decimal("20")

    def test_margin_peaks_in_metrics(self, session, sample_timestamp):
        """Peak margin values appear in finalized metrics."""
        t = sample_timestamp
        session.update_equity(t, Decimal("0"), total_im=Decimal("500"), total_mm=Decimal("50"))

        metrics = session.finalize()

        assert metrics.peak_im == Decimal("500")
        assert metrics.peak_mm == Decimal("50")
        # IMR% = 500 / 10000 * 100 = 5.0%
        assert metrics.peak_imr_pct == pytest.approx(5.0)
        # MMR% = 50 / 10000 * 100 = 0.5%
        assert metrics.peak_mmr_pct == pytest.approx(0.5)

    def test_margin_zero_by_default(self, session):
        """Margin peaks are zero when no margin data provided."""
        metrics = session.finalize()

        assert metrics.peak_im == Decimal("0")
        assert metrics.peak_mm == Decimal("0")
        assert metrics.peak_imr_pct == 0.0
        assert metrics.peak_mmr_pct == 0.0

    def test_backward_compatible_update_equity(self, session, sample_timestamp):
        """update_equity works without margin params (backward compatible)."""
        equity = session.update_equity(sample_timestamp, Decimal("100"))

        assert equity == Decimal("10100")  # 10000 + 100 unrealized
