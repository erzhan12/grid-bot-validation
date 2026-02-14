"""Comparator reporter for CSV export and console output.

Exports matched trades, unmatched trades, validation metrics,
and equity comparison to CSV files.
"""

import csv
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

from comparator.matcher import MatchResult
from comparator.metrics import ValidationMetrics

logger = logging.getLogger(__name__)

ResampledRow = tuple[datetime, Optional[Decimal], Optional[Decimal]]


class ComparatorReporter:
    """Export comparison results to CSV and console."""

    def __init__(
        self,
        match_result: MatchResult,
        metrics: ValidationMetrics,
        equity_data: list[ResampledRow] | None = None,
    ):
        self._match_result = match_result
        self._metrics = metrics
        self._equity_data = equity_data

    def _ensure_path(self, path: Union[str, Path]) -> Path:
        """Convert to Path and create parent directories."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def export_matched_trades(self, path: Union[str, Path]) -> None:
        """Export matched trade pairs with deltas to CSV."""
        path = self._ensure_path(path)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "client_order_id",
                "occurrence",
                "symbol",
                "side",
                "live_price",
                "backtest_price",
                "price_delta",
                "live_qty",
                "backtest_qty",
                "qty_delta",
                "live_fee",
                "backtest_fee",
                "fee_delta",
                "live_pnl",
                "backtest_pnl",
                "pnl_delta",
                "live_timestamp",
                "backtest_timestamp",
            ])

            # Zip directly — trade_deltas is computed in the same order as
            # matched pairs (metrics.py:173), so they're 1:1.
            for pair, delta in zip(
                self._match_result.matched, self._metrics.trade_deltas
            ):
                writer.writerow([
                    pair.live.client_order_id,
                    pair.live.occurrence,
                    pair.live.symbol,
                    pair.live.side,
                    str(pair.live.price),
                    str(pair.backtest.price),
                    str(delta.price_delta),
                    str(pair.live.qty),
                    str(pair.backtest.qty),
                    str(delta.qty_delta),
                    str(pair.live.fee),
                    str(pair.backtest.fee),
                    str(delta.fee_delta),
                    str(pair.live.realized_pnl),
                    str(pair.backtest.realized_pnl),
                    str(delta.pnl_delta),
                    pair.live.timestamp.isoformat(),
                    pair.backtest.timestamp.isoformat(),
                ])

        logger.info("Exported %d matched trades to %s", len(self._match_result.matched), path)

    def export_unmatched_trades(self, path: Union[str, Path]) -> None:
        """Export live-only and backtest-only trades to CSV."""
        path = self._ensure_path(path)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "source",
                "client_order_id",
                "occurrence",
                "symbol",
                "side",
                "price",
                "qty",
                "fee",
                "realized_pnl",
                "timestamp",
            ])

            for trade in self._match_result.live_only:
                writer.writerow([
                    "live_only",
                    trade.client_order_id,
                    trade.occurrence,
                    trade.symbol,
                    trade.side,
                    str(trade.price),
                    str(trade.qty),
                    str(trade.fee),
                    str(trade.realized_pnl),
                    trade.timestamp.isoformat(),
                ])

            for trade in self._match_result.backtest_only:
                writer.writerow([
                    "backtest_only",
                    trade.client_order_id,
                    trade.occurrence,
                    trade.symbol,
                    trade.side,
                    str(trade.price),
                    str(trade.qty),
                    str(trade.fee),
                    str(trade.realized_pnl),
                    trade.timestamp.isoformat(),
                ])

        total = len(self._match_result.live_only) + len(self._match_result.backtest_only)
        logger.info("Exported %d unmatched trades to %s", total, path)

    def export_metrics(self, path: Union[str, Path]) -> None:
        """Export validation metrics to key-value CSV."""
        path = self._ensure_path(path)

        m = self._metrics
        rows = [
            ("total_live_trades", str(m.total_live_trades)),
            ("total_backtest_trades", str(m.total_backtest_trades)),
            ("matched_count", str(m.matched_count)),
            ("live_only_count", str(m.live_only_count)),
            ("backtest_only_count", str(m.backtest_only_count)),
            ("match_rate", f"{m.match_rate:.4f}"),
            ("phantom_rate", f"{m.phantom_rate:.4f}"),
            ("price_mean_abs_delta", str(m.price_mean_abs_delta)),
            ("price_median_abs_delta", str(m.price_median_abs_delta)),
            ("price_max_abs_delta", str(m.price_max_abs_delta)),
            ("qty_mean_abs_delta", str(m.qty_mean_abs_delta)),
            ("qty_median_abs_delta", str(m.qty_median_abs_delta)),
            ("qty_max_abs_delta", str(m.qty_max_abs_delta)),
            ("total_live_fees", str(m.total_live_fees)),
            ("total_backtest_fees", str(m.total_backtest_fees)),
            ("fee_delta", str(m.fee_delta)),
            ("total_live_pnl", str(m.total_live_pnl)),
            ("total_backtest_pnl", str(m.total_backtest_pnl)),
            ("cumulative_pnl_delta", str(m.cumulative_pnl_delta)),
            ("pnl_correlation", f"{m.pnl_correlation:.6f}"),
            ("total_live_volume", str(m.total_live_volume)),
            ("total_backtest_volume", str(m.total_backtest_volume)),
            ("long_match_count", str(m.long_match_count)),
            ("short_match_count", str(m.short_match_count)),
            ("long_pnl_delta", str(m.long_pnl_delta)),
            ("short_pnl_delta", str(m.short_pnl_delta)),
            ("mean_time_delta_seconds", f"{m.mean_time_delta_seconds:.2f}"),
            ("breaches_count", str(m.breaches_count)),
            ("equity_max_divergence", str(m.equity_max_divergence)),
            ("equity_mean_divergence", str(m.equity_mean_divergence)),
            ("equity_correlation", f"{m.equity_correlation:.6f}"),
        ]

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["metric", "value"])
            for metric, value in rows:
                writer.writerow([metric, value])

        logger.info("Exported validation metrics to %s", path)

    def export_equity(self, path: Union[str, Path]) -> None:
        """Export equity comparison to CSV.

        Args:
            path: Output CSV path.
        """
        if not self._equity_data:
            return

        path = self._ensure_path(path)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "live_equity", "backtest_equity", "divergence"])

            for ts, live_eq, bt_eq in self._equity_data:
                divergence = ""
                if live_eq is not None and bt_eq is not None:
                    divergence = str(bt_eq - live_eq)

                writer.writerow([
                    ts.isoformat(),
                    str(live_eq) if live_eq is not None else "",
                    str(bt_eq) if bt_eq is not None else "",
                    divergence,
                ])

        logger.info("Exported equity comparison (%d points) to %s", len(self._equity_data), path)

    def export_all(self, output_dir: Union[str, Path]) -> dict[str, Path]:
        """Export all reports to a directory.

        Returns:
            Dict mapping report type to file path.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        paths = {
            "matched_trades": output_dir / "matched_trades.csv",
            "unmatched_trades": output_dir / "unmatched_trades.csv",
            "validation_metrics": output_dir / "validation_metrics.csv",
        }

        self.export_matched_trades(paths["matched_trades"])
        self.export_unmatched_trades(paths["unmatched_trades"])
        self.export_metrics(paths["validation_metrics"])

        if self._equity_data:
            equity_path = output_dir / "equity_comparison.csv"
            self.export_equity(equity_path)
            paths["equity_comparison"] = equity_path

        return paths

    def print_summary(self) -> None:
        """Print summary metrics to console."""
        m = self._metrics
        lines = [
            "",
            "=" * 60,
            "  BACKTEST vs LIVE COMPARISON",
            "=" * 60,
            "",
            f"  Live trades:      {m.total_live_trades}",
            f"  Backtest trades:  {m.total_backtest_trades}",
            f"  Matched:          {m.matched_count}",
            f"  Live-only:        {m.live_only_count} (missed by backtest)",
            f"  Backtest-only:    {m.backtest_only_count} (phantom fills)",
            f"  Match rate:       {m.match_rate:.1%}",
            f"  Phantom rate:     {m.phantom_rate:.1%}",
            "",
            "  PRICE ACCURACY (matched pairs)",
            f"    Mean |delta|:   {m.price_mean_abs_delta}",
            f"    Median |delta|: {m.price_median_abs_delta}",
            f"    Max |delta|:    {m.price_max_abs_delta}",
            "",
            "  QUANTITY ACCURACY",
            f"    Mean |delta|:   {m.qty_mean_abs_delta}",
            f"    Max |delta|:    {m.qty_max_abs_delta}",
            "",
            "  PnL COMPARISON",
            f"    Live total:     {m.total_live_pnl}",
            f"    Backtest total: {m.total_backtest_pnl}",
            f"    Delta:          {m.cumulative_pnl_delta}",
            f"    Correlation:    {m.pnl_correlation:.4f}",
            "",
            "  FEES",
            f"    Live total:     {m.total_live_fees}",
            f"    Backtest total: {m.total_backtest_fees}",
            f"    Delta:          {m.fee_delta}",
            "",
            "  VOLUME",
            f"    Live total:     {m.total_live_volume}",
            f"    Backtest total: {m.total_backtest_volume}",
            "",
            "  DIRECTION BREAKDOWN",
            f"    Long matched:   {m.long_match_count}",
            f"    Short matched:  {m.short_match_count}",
            f"    Long PnL delta: {m.long_pnl_delta}",
            f"    Short PnL delta:{m.short_pnl_delta}",
            "",
            "  TIMING",
            f"    Mean |delta|:   {m.mean_time_delta_seconds:.1f}s",
            "",
            "  TOLERANCE BREACHES",
            f"    Count:          {m.breaches_count}",
            "",
            "  EQUITY CURVE",
            f"    Max divergence: {m.equity_max_divergence}",
            f"    Mean divergence:{m.equity_mean_divergence}",
            f"    Correlation:    {m.equity_correlation:.4f}",
            "",
            "=" * 60,
        ]
        print("\n".join(lines))
