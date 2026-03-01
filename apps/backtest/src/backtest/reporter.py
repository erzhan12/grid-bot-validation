"""Backtest reporter for exporting results to various formats.

Supports CSV export for trades, equity curve, and metrics summary.
"""

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Union

from backtest.session import BacktestSession, BacktestMetrics, BacktestTrade


class BacktestReporter:
    """Export backtest results to CSV and other formats.

    Example:
        session = engine.run(symbol="BTCUSDT", start_ts=start, end_ts=end)
        reporter = BacktestReporter(session)

        reporter.export_trades("trades.csv")
        reporter.export_equity_curve("equity.csv")
        reporter.export_metrics("metrics.csv")
        reporter.export_all("output_dir/")
    """

    def __init__(self, session: BacktestSession):
        """Initialize reporter with a backtest session.

        Args:
            session: Completed backtest session with results.
        """
        self._session = session
        if session.metrics is None:
            session.finalize()

    @property
    def session(self) -> BacktestSession:
        """The backtest session."""
        return self._session

    @property
    def metrics(self) -> BacktestMetrics:
        """The backtest metrics."""
        return self._session.metrics

    def _ensure_path(self, path: Union[str, Path]) -> Path:
        """Convert to Path and create parent directories."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def export_trades(self, path: Union[str, Path]) -> None:
        """Export trades to CSV.

        Args:
            path: Output file path.
        """
        path = self._ensure_path(path)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id",
                "timestamp",
                "symbol",
                "side",
                "direction",
                "price",
                "qty",
                "notional",
                "realized_pnl",
                "commission",
                "order_id",
                "client_order_id",
                "strat_id",
            ])

            for trade in self._session.trades:
                writer.writerow([
                    trade.trade_id,
                    trade.timestamp.isoformat(),
                    trade.symbol,
                    trade.side,
                    trade.direction,
                    str(trade.price),
                    str(trade.qty),
                    str(trade.price * trade.qty),
                    str(trade.realized_pnl),
                    str(trade.commission),
                    trade.order_id,
                    trade.client_order_id,
                    trade.strat_id,
                ])

    def export_equity_curve(self, path: Union[str, Path]) -> None:
        """Export equity curve to CSV.

        Args:
            path: Output file path.
        """
        path = self._ensure_path(path)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "equity", "return_pct"])

            initial = self._session.initial_balance
            for timestamp, equity in self._session.equity_curve:
                return_pct = float((equity - initial) / initial * 100) if initial > 0 else 0.0
                writer.writerow([
                    timestamp.isoformat(),
                    str(equity),
                    f"{return_pct:.4f}",
                ])

    def export_metrics(self, path: Union[str, Path]) -> None:
        """Export metrics summary to CSV.

        Args:
            path: Output file path.
        """
        path = self._ensure_path(path)

        m = self._session.metrics
        metrics_data = [
            ("session_id", self._session.session_id),
            ("initial_balance", str(m.initial_balance)),
            ("final_balance", str(m.final_balance)),
            ("return_pct", f"{m.return_pct:.2f}"),
            ("net_pnl", str(m.net_pnl)),
            ("total_realized_pnl", str(m.total_realized_pnl)),
            ("total_unrealized_pnl", str(m.total_unrealized_pnl)),
            ("total_commission", str(m.total_commission)),
            ("total_funding", str(m.total_funding)),
            ("total_trades", str(m.total_trades)),
            ("winning_trades", str(m.winning_trades)),
            ("losing_trades", str(m.losing_trades)),
            ("win_rate", f"{m.win_rate:.4f}"),
            ("avg_win", str(m.avg_win)),
            ("avg_loss", str(m.avg_loss)),
            ("profit_factor", f"{m.profit_factor:.4f}"),
            ("max_drawdown", str(m.max_drawdown)),
            ("max_drawdown_pct", f"{m.max_drawdown_pct:.2f}"),
            ("max_drawdown_duration", str(m.max_drawdown_duration)),
            ("sharpe_ratio", f"{m.sharpe_ratio:.4f}"),
            ("total_volume", str(m.total_volume)),
            ("turnover", f"{m.turnover:.2f}"),
            ("long_trades", str(m.long_trades)),
            ("short_trades", str(m.short_trades)),
            ("long_pnl", str(m.long_pnl)),
            ("short_pnl", str(m.short_pnl)),
            ("long_profit_factor", f"{m.long_profit_factor:.4f}"),
            ("short_profit_factor", f"{m.short_profit_factor:.4f}"),
            ("peak_im", str(m.peak_im)),
            ("peak_mm", str(m.peak_mm)),
            ("peak_imr_pct", f"{m.peak_imr_pct:.2f}"),
            ("peak_mmr_pct", f"{m.peak_mmr_pct:.2f}"),
        ]

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["metric", "value"])
            for metric, value in metrics_data:
                writer.writerow([metric, value])

    def export_all(self, output_dir: Union[str, Path], prefix: str = "") -> dict[str, Path]:
        """Export all data to a directory.

        Args:
            output_dir: Output directory path.
            prefix: Optional prefix for file names.

        Returns:
            Dict mapping export type to file path.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        prefix = f"{prefix}_" if prefix else ""
        session_id = self._session.session_id[:8]

        paths = {
            "trades": output_dir / f"{prefix}{session_id}_trades.csv",
            "equity_curve": output_dir / f"{prefix}{session_id}_equity.csv",
            "metrics": output_dir / f"{prefix}{session_id}_metrics.csv",
        }

        self.export_trades(paths["trades"])
        self.export_equity_curve(paths["equity_curve"])
        self.export_metrics(paths["metrics"])

        return paths

    def get_summary_dict(self) -> dict:
        """Get metrics as a dictionary.

        Returns:
            Dict with all metric values.
        """
        m = self._session.metrics
        return {
            "session_id": self._session.session_id,
            "initial_balance": float(m.initial_balance),
            "final_balance": float(m.final_balance),
            "return_pct": m.return_pct,
            "net_pnl": float(m.net_pnl),
            "total_realized_pnl": float(m.total_realized_pnl),
            "total_unrealized_pnl": float(m.total_unrealized_pnl),
            "total_commission": float(m.total_commission),
            "total_funding": float(m.total_funding),
            "total_trades": m.total_trades,
            "winning_trades": m.winning_trades,
            "losing_trades": m.losing_trades,
            "win_rate": m.win_rate,
            "avg_win": float(m.avg_win),
            "avg_loss": float(m.avg_loss),
            "profit_factor": m.profit_factor,
            "max_drawdown": float(m.max_drawdown),
            "max_drawdown_pct": m.max_drawdown_pct,
            "max_drawdown_duration": m.max_drawdown_duration,
            "sharpe_ratio": m.sharpe_ratio,
            "total_volume": float(m.total_volume),
            "turnover": m.turnover,
            "long_trades": m.long_trades,
            "short_trades": m.short_trades,
            "long_pnl": float(m.long_pnl),
            "short_pnl": float(m.short_pnl),
            "long_profit_factor": m.long_profit_factor,
            "short_profit_factor": m.short_profit_factor,
            "peak_im": float(m.peak_im),
            "peak_mm": float(m.peak_mm),
            "peak_imr_pct": m.peak_imr_pct,
            "peak_mmr_pct": m.peak_mmr_pct,
        }
