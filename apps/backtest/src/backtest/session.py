"""Backtest session for in-memory results storage.

Stores trades, equity curve, and calculates final metrics.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from gridcore import DirectionType


@dataclass
class BacktestTrade:
    """Record of a simulated trade."""

    trade_id: str
    symbol: str
    side: str  # 'Buy' or 'Sell'
    price: Decimal
    qty: Decimal
    direction: str  # 'long' or 'short'
    timestamp: datetime
    order_id: str
    client_order_id: str
    realized_pnl: Decimal
    commission: Decimal
    strat_id: str = ""


@dataclass
class BacktestMetrics:
    """Final metrics for backtest."""

    # Trade stats
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_loss: Decimal = field(default_factory=lambda: Decimal("0"))

    # PnL
    total_realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    total_unrealized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    total_commission: Decimal = field(default_factory=lambda: Decimal("0"))
    total_funding: Decimal = field(default_factory=lambda: Decimal("0"))
    net_pnl: Decimal = field(default_factory=lambda: Decimal("0"))

    # Risk
    max_drawdown: Decimal = field(default_factory=lambda: Decimal("0"))
    max_drawdown_pct: float = 0.0
    max_drawdown_duration: int = 0  # Number of ticks in longest drawdown
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0  # Risk-adjusted return (annualized)

    # Margin (from gridcore IM/MM calculations)
    peak_im: Decimal = field(default_factory=lambda: Decimal("0"))
    peak_mm: Decimal = field(default_factory=lambda: Decimal("0"))
    peak_imr_pct: float = 0.0  # Peak initial margin rate %
    peak_mmr_pct: float = 0.0  # Peak maintenance margin rate %

    # Balance
    initial_balance: Decimal = field(default_factory=lambda: Decimal("0"))
    final_balance: Decimal = field(default_factory=lambda: Decimal("0"))
    return_pct: float = 0.0

    # Turnover
    total_volume: Decimal = field(default_factory=lambda: Decimal("0"))
    turnover: float = 0.0  # total_volume / initial_balance

    # Long/Short breakdown
    long_trades: int = 0
    short_trades: int = 0
    long_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    short_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    long_profit_factor: float = 0.0
    short_profit_factor: float = 0.0


class BacktestSession:
    """In-memory storage for backtest results.

    Tracks trades, equity curve, and calculates performance metrics.
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        initial_balance: Decimal = Decimal("10000"),
    ):
        """Initialize backtest session.

        Args:
            session_id: Unique session identifier (generated if None)
            initial_balance: Starting wallet balance
        """
        self.session_id = session_id or uuid.uuid4().hex
        self.initial_balance = initial_balance
        self.current_balance = initial_balance

        # Trade tracking
        self.trades: list[BacktestTrade] = []

        # Equity curve: (timestamp, equity)
        self.equity_curve: list[tuple[datetime, Decimal]] = []

        # Running totals
        self.total_realized_pnl = Decimal("0")
        self.total_commission = Decimal("0")
        self.total_funding = Decimal("0")

        # Peak for drawdown calculation
        self._peak_equity = initial_balance
        self._max_drawdown = Decimal("0")

        # Drawdown duration tracking
        self._drawdown_start_idx: Optional[int] = None  # Index when drawdown started
        self._max_drawdown_duration = 0  # Longest drawdown in ticks
        self._current_drawdown_duration = 0

        # Volume tracking for turnover
        self.total_volume = Decimal("0")

        # Margin peak tracking
        self._peak_im = Decimal("0")
        self._peak_mm = Decimal("0")
        self._peak_imr_pct = 0.0
        self._peak_mmr_pct = 0.0
        self._last_margin_inputs: Optional[tuple[Decimal, Decimal, Decimal]] = None
        self._last_margin_pct: tuple[float, float] = (0.0, 0.0)

        # Final metrics (populated by finalize())
        self.metrics: Optional[BacktestMetrics] = None

    def record_trade(self, trade: BacktestTrade) -> None:
        """Record executed trade.

        Args:
            trade: Trade record to add
        """
        self.trades.append(trade)
        self.total_realized_pnl += trade.realized_pnl
        self.total_commission += trade.commission
        # Track volume for turnover calculation
        self.total_volume += trade.qty * trade.price

    def record_funding(self, amount: Decimal) -> None:
        """Record funding payment.

        Args:
            amount: Funding amount (negative = paid, positive = received)
        """
        self.total_funding += amount

    def update_equity(
        self,
        timestamp: datetime,
        unrealized_pnl: Decimal,
        total_im: Decimal = Decimal("0"),
        total_mm: Decimal = Decimal("0"),
    ) -> Decimal:
        """Record equity point, update drawdown, and track margin peaks.

        Args:
            timestamp: Current timestamp
            unrealized_pnl: Current unrealized PnL
            total_im: Sum of initial margins across all positions
            total_mm: Sum of maintenance margins across all positions

        Returns:
            Current equity
        """
        equity = (
            self.initial_balance
            + self.total_realized_pnl
            + unrealized_pnl
            - self.total_commission
            + self.total_funding
        )

        self.equity_curve.append((timestamp, equity))
        self.current_balance = equity

        # Update peak and drawdown
        if equity >= self._peak_equity:
            self._peak_equity = equity
            # Exited drawdown - check if this was the longest
            if self._current_drawdown_duration > self._max_drawdown_duration:
                self._max_drawdown_duration = self._current_drawdown_duration
            self._current_drawdown_duration = 0
        else:
            # In drawdown - increment duration
            self._current_drawdown_duration += 1

        drawdown = self._peak_equity - equity
        if drawdown > self._max_drawdown:
            self._max_drawdown = drawdown

        # Track margin peaks
        if total_im > self._peak_im:
            self._peak_im = total_im
        if total_mm > self._peak_mm:
            self._peak_mm = total_mm
        if equity > 0:
            imr_pct, mmr_pct = self._get_margin_pct(total_im, total_mm, equity)
            if imr_pct > self._peak_imr_pct:
                self._peak_imr_pct = imr_pct
            if mmr_pct > self._peak_mmr_pct:
                self._peak_mmr_pct = mmr_pct

        return equity

    def _get_margin_pct(
        self,
        total_im: Decimal,
        total_mm: Decimal,
        equity: Decimal,
    ) -> tuple[float, float]:
        """Compute IMR/MMR percentages with a tiny cache for repeated ticks."""
        inputs = (total_im, total_mm, equity)
        if self._last_margin_inputs != inputs:
            scale = Decimal("100") / equity
            self._last_margin_pct = (
                float(total_im * scale),
                float(total_mm * scale),
            )
            self._last_margin_inputs = inputs
        return self._last_margin_pct

    def finalize(
        self,
        final_unrealized_pnl: Decimal = Decimal("0"),
        sharpe_interval: timedelta = timedelta(hours=1),
    ) -> BacktestMetrics:
        """Calculate final metrics.

        Args:
            final_unrealized_pnl: Unrealized PnL at end of backtest
            sharpe_interval: Resampling interval for Sharpe ratio calculation.
                Raw tick data has irregular spacing, so equity is resampled
                to fixed intervals before computing returns.

        Returns:
            Calculated metrics
        """
        # Calculate trade stats
        total_trades = len(self.trades)
        winning_trades = sum(1 for t in self.trades if t.realized_pnl > 0)
        losing_trades = sum(1 for t in self.trades if t.realized_pnl < 0)

        wins = [t.realized_pnl for t in self.trades if t.realized_pnl > 0]
        losses = [t.realized_pnl for t in self.trades if t.realized_pnl < 0]

        avg_win = sum(wins) / len(wins) if wins else Decimal("0")
        avg_loss = sum(losses) / len(losses) if losses else Decimal("0")
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

        # Calculate profit factor
        gross_profit = sum(wins) if wins else Decimal("0")
        gross_loss = abs(sum(losses)) if losses else Decimal("0")
        profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else 0.0

        # Final balance
        final_balance = (
            self.initial_balance
            + self.total_realized_pnl
            + final_unrealized_pnl
            - self.total_commission
            + self.total_funding
        )

        # Net PnL
        net_pnl = final_balance - self.initial_balance

        # Return percentage
        return_pct = float(net_pnl / self.initial_balance * 100) if self.initial_balance > 0 else 0.0

        # Max drawdown percentage
        max_dd_pct = (
            float(self._max_drawdown / self._peak_equity * 100)
            if self._peak_equity > 0
            else 0.0
        )

        # Finalize drawdown duration (check if still in drawdown at end)
        max_dd_duration = max(self._max_drawdown_duration, self._current_drawdown_duration)

        # Calculate Sharpe ratio from equity curve (resampled to fixed intervals)
        sharpe_ratio = self._calculate_sharpe_ratio(sharpe_interval)

        # Calculate turnover
        turnover = (
            float(self.total_volume / self.initial_balance)
            if self.initial_balance > 0
            else 0.0
        )

        # Long/short breakdown
        long_trades = [t for t in self.trades if t.direction == DirectionType.LONG]
        short_trades = [t for t in self.trades if t.direction == DirectionType.SHORT]

        long_pnl = sum(t.realized_pnl for t in long_trades)
        short_pnl = sum(t.realized_pnl for t in short_trades)

        long_wins = [t.realized_pnl for t in long_trades if t.realized_pnl > 0]
        long_losses = [t.realized_pnl for t in long_trades if t.realized_pnl < 0]
        long_gross_profit = sum(long_wins) if long_wins else Decimal("0")
        long_gross_loss = abs(sum(long_losses)) if long_losses else Decimal("0")
        long_profit_factor = float(long_gross_profit / long_gross_loss) if long_gross_loss > 0 else 0.0

        short_wins = [t.realized_pnl for t in short_trades if t.realized_pnl > 0]
        short_losses = [t.realized_pnl for t in short_trades if t.realized_pnl < 0]
        short_gross_profit = sum(short_wins) if short_wins else Decimal("0")
        short_gross_loss = abs(sum(short_losses)) if short_losses else Decimal("0")
        short_profit_factor = float(short_gross_profit / short_gross_loss) if short_gross_loss > 0 else 0.0

        self.metrics = BacktestMetrics(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            total_realized_pnl=self.total_realized_pnl,
            total_unrealized_pnl=final_unrealized_pnl,
            total_commission=self.total_commission,
            total_funding=self.total_funding,
            net_pnl=net_pnl,
            max_drawdown=self._max_drawdown,
            max_drawdown_pct=max_dd_pct,
            max_drawdown_duration=max_dd_duration,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe_ratio,
            initial_balance=self.initial_balance,
            final_balance=final_balance,
            return_pct=return_pct,
            total_volume=self.total_volume,
            turnover=turnover,
            long_trades=len(long_trades),
            short_trades=len(short_trades),
            long_pnl=long_pnl,
            short_pnl=short_pnl,
            long_profit_factor=long_profit_factor,
            short_profit_factor=short_profit_factor,
            peak_im=self._peak_im,
            peak_mm=self._peak_mm,
            peak_imr_pct=self._peak_imr_pct,
            peak_mmr_pct=self._peak_mmr_pct,
        )

        return self.metrics

    def _calculate_sharpe_ratio(self, interval: timedelta = timedelta(hours=1)) -> float:
        """Calculate annualized Sharpe ratio from equity curve.

        Raw equity data comes at irregular tick intervals, so it is
        resampled to fixed-width buckets before computing returns.
        Each bucket takes the last equity value that falls within it.

        Args:
            interval: Resampling interval (default: 1 hour).

        Returns:
            Annualized Sharpe ratio (0 if insufficient data).
        """
        if len(self.equity_curve) < 2:
            return 0.0

        # Resample to fixed intervals
        resampled = self._resample_equity(interval)
        if len(resampled) < 2:
            return 0.0

        # Calculate returns between consecutive buckets
        returns = []
        for i in range(1, len(resampled)):
            if resampled[i - 1] != 0:
                ret = (resampled[i] - resampled[i - 1]) / resampled[i - 1]
                returns.append(ret)

        if len(returns) < 2:
            return 0.0

        # Calculate mean and std of returns
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
        std_return = variance ** 0.5

        if std_return == 0:
            return 0.0

        # Annualize: crypto trades 24/7 → 365.25 days/year
        seconds_per_year = 365.25 * 24 * 3600
        periods_per_year = seconds_per_year / interval.total_seconds()

        sharpe = (mean_return / std_return) * (periods_per_year ** 0.5)
        return sharpe

    def _resample_equity(self, interval: timedelta) -> list[float]:
        """Resample equity curve to fixed-width time buckets.

        Takes the last equity value within each bucket. Buckets with
        no data points are skipped (no forward-fill).

        Args:
            interval: Bucket width.

        Returns:
            List of equity values at regular intervals.
        """
        if not self.equity_curve:
            return []

        start_ts = self.equity_curve[0][0]
        end_ts = self.equity_curve[-1][0]

        resampled: list[float] = []
        bucket_start = start_ts
        eq_idx = 0

        while bucket_start <= end_ts:
            bucket_end = bucket_start + interval
            last_value = None

            while eq_idx < len(self.equity_curve) and self.equity_curve[eq_idx][0] < bucket_end:
                last_value = float(self.equity_curve[eq_idx][1])
                eq_idx += 1

            if last_value is not None:
                resampled.append(last_value)

            bucket_start = bucket_end

        return resampled

    def get_summary(self) -> str:
        """Get human-readable summary of results."""
        if self.metrics is None:
            self.finalize()

        m = self.metrics
        return f"""
Backtest Results (Session: {self.session_id[:8]}...)
{'='*50}
Trades: {m.total_trades} (Win: {m.winning_trades}, Loss: {m.losing_trades})
Win Rate: {m.win_rate:.1%}
Avg Win: {m.avg_win:.2f} | Avg Loss: {m.avg_loss:.2f}
Profit Factor: {m.profit_factor:.2f}

PnL Breakdown:
  Realized:   {m.total_realized_pnl:>12.2f}
  Unrealized: {m.total_unrealized_pnl:>12.2f}
  Commission: {-m.total_commission:>12.2f}
  Funding:    {m.total_funding:>12.2f}
  Net PnL:    {m.net_pnl:>12.2f}

Balance:
  Initial:    {m.initial_balance:>12.2f}
  Final:      {m.final_balance:>12.2f}
  Return:     {m.return_pct:>11.2f}%

Risk:
  Max Drawdown: {m.max_drawdown:.2f} ({m.max_drawdown_pct:.1f}%)
  Max DD Duration: {m.max_drawdown_duration} ticks
  Sharpe Ratio: {m.sharpe_ratio:.2f}

Margin:
  Peak IM: {m.peak_im:.4f} ({m.peak_imr_pct:.2f}%)
  Peak MM: {m.peak_mm:.4f} ({m.peak_mmr_pct:.2f}%)

Activity:
  Total Volume: {m.total_volume:.2f}
  Turnover: {m.turnover:.2f}x

Long/Short Breakdown:
  Long:  {m.long_trades} trades, PnL: {m.long_pnl:.2f}, PF: {m.long_profit_factor:.2f}
  Short: {m.short_trades} trades, PnL: {m.short_pnl:.2f}, PF: {m.short_profit_factor:.2f}
"""
