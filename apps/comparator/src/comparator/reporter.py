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
from comparator.position_metrics import PositionComparisonPair

logger = logging.getLogger(__name__)

ResampledRow = tuple[datetime, Optional[Decimal], Optional[Decimal]]


class ComparatorReporter:
    """Export comparison results to CSV and console."""

    # Characters that spreadsheet apps (Excel, Sheets, LibreOffice) interpret
    # as formula triggers when a cell value starts with them. Prefixing with
    # a single quote forces the cell to be read as a literal string.
    _CSV_INJECTION_TRIGGERS = ("=", "+", "-", "@")

    def __init__(
        self,
        match_result: MatchResult,
        metrics: ValidationMetrics,
        equity_data: list[ResampledRow] | None = None,
        metadata: dict[str, str] | None = None,
        position_pairs: list[PositionComparisonPair] | None = None,
    ):
        self._match_result = match_result
        self._metrics = metrics
        self._equity_data = equity_data
        self._metadata = metadata or {}
        self._position_pairs = position_pairs or []

    def _ensure_path(self, path: Union[str, Path]) -> Path:
        """Convert to Path and create parent directories."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _sanitize_csv_value(self, value: str) -> str:
        """Defuse spreadsheet formula injection on cell values.

        Spreadsheet apps interpret a cell starting with =, +, -, @ as a
        formula. Prefix such values with a single quote so they're read as
        literal text instead.
        """
        if value and value[0] in self._CSV_INJECTION_TRIGGERS:
            return f"'{value}"
        return value

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
            # 0034: position telemetry parity metrics.
            ("position_im_mean_abs_delta", str(m.position_im_mean_abs_delta)),
            ("position_im_max_abs_delta", str(m.position_im_max_abs_delta)),
            ("position_mm_mean_abs_delta", str(m.position_mm_mean_abs_delta)),
            ("position_mm_max_abs_delta", str(m.position_mm_max_abs_delta)),
            ("liq_price_mean_abs_delta", str(m.liq_price_mean_abs_delta)),
            ("liq_price_max_abs_delta", str(m.liq_price_max_abs_delta)),
            ("unrealised_pnl_mean_abs_delta", str(m.unrealised_pnl_mean_abs_delta)),
            ("unrealised_pnl_max_abs_delta", str(m.unrealised_pnl_max_abs_delta)),
            ("cum_realised_pnl_final_delta", str(m.cum_realised_pnl_final_delta)),
            ("cur_realised_pnl_final_delta", str(m.cur_realised_pnl_final_delta)),
            # 0059: per-snapshot parity aggregates (distinct from the
            # *_pnl_final_delta rows above — the _usdt_*_abs_delta suffix
            # separates the per-snapshot family from the final-only scalars).
            ("upnl_usdt_mean_abs_delta", str(m.upnl_usdt_mean_abs_delta)),
            ("upnl_usdt_max_abs_delta", str(m.upnl_usdt_max_abs_delta)),
            ("cur_realised_usdt_mean_abs_delta", str(m.cur_realised_usdt_mean_abs_delta)),
            ("cur_realised_usdt_max_abs_delta", str(m.cur_realised_usdt_max_abs_delta)),
            ("cum_realised_usdt_mean_abs_delta", str(m.cum_realised_usdt_mean_abs_delta)),
            ("cum_realised_usdt_max_abs_delta", str(m.cum_realised_usdt_max_abs_delta)),
            ("pos_value_usdt_mean_abs_delta", str(m.pos_value_usdt_mean_abs_delta)),
            ("pos_value_usdt_max_abs_delta", str(m.pos_value_usdt_max_abs_delta)),
            ("pos_value_final_delta", str(m.pos_value_final_delta)),
            ("position_pairs_compared", str(m.position_pairs_compared)),
            ("position_pairs_unmatched_bt", str(m.position_pairs_unmatched_bt)),
            ("position_pairs_state_diverged", str(m.position_pairs_state_diverged)),
            ("position_pairs_missing_telemetry", str(m.position_pairs_missing_telemetry)),
            # 0065: non-USDT collateral re-mark attribution. Total always
            # present (0 → back-compat); per-coin + exclusion lists only when
            # non-empty so USDT-only runs stay byte-identical.
            ("non_usdt_collateral_drift_total", str(m.non_usdt_collateral_drift_total)),
        ]
        for coin, value in m.collateral_drift_by_coin.items():
            rows.append((f"collateral_drift.{coin}", str(value)))
        if m.collateral_excluded_coins:
            rows.append(
                ("collateral_excluded_coins", ",".join(m.collateral_excluded_coins))
            )
        if m.collateral_missing_mark_coins:
            rows.append(
                (
                    "collateral_missing_mark_coins",
                    ",".join(m.collateral_missing_mark_coins),
                )
            )
        if m.collateral_switch_off_coins:
            rows.append(
                ("collateral_switch_off_coins", ",".join(m.collateral_switch_off_coins))
            )

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["metric", "value"])
            for metric, value in rows:
                writer.writerow([metric, value])
            for key, value in sorted(self._metadata.items()):
                writer.writerow([f"meta.{key}", self._sanitize_csv_value(value)])

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

    def export_position_comparison(self, path: Union[str, Path]) -> None:
        """Export per-pair position telemetry deltas to CSV (0034).

        Columns mirror the plan: per-side, live and backtest sides plus
        the recomputed unrealized PnL (apples-to-apples against
        live.mark_price) and absolute deltas.
        """
        path = self._ensure_path(path)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "side",
                "live_ts",
                "backtest_ts",
                "live_size",
                "bt_size",
                "live_entry",
                "bt_entry",
                "mark_price",
                "live_im",
                "bt_im",
                "im_delta",
                "live_mm",
                "bt_mm",
                "mm_delta",
                "live_liq",
                "bt_liq",
                "liq_delta",
                "live_unrealised_recomp",
                "bt_unrealised_recomp",
                "unrealised_delta",
                "live_cum_realised",
                "bt_cum_realised",
                "cum_realised_delta",
                "live_cur_realised",
                "bt_cur_realised",
                "cur_realised_delta",
                # 0059: stored upnl parity (NOT the recomputed-vs-mark
                # columns above) + position notional.
                "live_upnl_usdt",
                "bt_upnl_usdt",
                "upnl_usdt_delta",
                "live_position_value",
                "bt_position_value",
                "pos_value_delta",
                "state_diverged",
            ])

            for pair in self._position_pairs:
                if pair.live is None:
                    continue  # unmatched-bt sentinel; counted but not emitted
                live = pair.live
                bt = pair.backtest
                writer.writerow([
                    pair.side,
                    live.exchange_ts.isoformat(),
                    bt.exchange_ts.isoformat(),
                    str(live.size),
                    str(bt.size),
                    str(live.entry_price),
                    str(bt.entry_price),
                    str(live.mark_price) if live.mark_price is not None else "",
                    str(live.position_im) if live.position_im is not None else "",
                    str(bt.position_im) if bt.position_im is not None else "",
                    str(pair.position_im_delta) if pair.position_im_delta is not None else "",
                    str(live.position_mm) if live.position_mm is not None else "",
                    str(bt.position_mm) if bt.position_mm is not None else "",
                    str(pair.position_mm_delta) if pair.position_mm_delta is not None else "",
                    str(live.liq_price) if live.liq_price is not None else "",
                    str(bt.liq_price) if bt.liq_price is not None else "",
                    str(pair.liq_price_delta) if pair.liq_price_delta is not None else "",
                    str(pair.unrealised_pnl_recomputed_live) if pair.unrealised_pnl_recomputed_live is not None else "",
                    str(pair.unrealised_pnl_recomputed_bt) if pair.unrealised_pnl_recomputed_bt is not None else "",
                    str(pair.unrealised_pnl_delta) if pair.unrealised_pnl_delta is not None else "",
                    str(live.cum_realised_pnl) if live.cum_realised_pnl is not None else "",
                    str(bt.cum_realised_pnl) if bt.cum_realised_pnl is not None else "",
                    str(pair.cum_realised_pnl_delta) if pair.cum_realised_pnl_delta is not None else "",
                    str(live.cur_realised_pnl) if live.cur_realised_pnl is not None else "",
                    str(bt.cur_realised_pnl) if bt.cur_realised_pnl is not None else "",
                    str(pair.cur_realised_pnl_delta) if pair.cur_realised_pnl_delta is not None else "",
                    # 0059: stored upnl parity + position value.
                    str(live.unrealised_pnl) if live.unrealised_pnl is not None else "",
                    str(bt.unrealised_pnl) if bt.unrealised_pnl is not None else "",
                    str(pair.upnl_usdt_delta) if pair.upnl_usdt_delta is not None else "",
                    str(live.position_value) if live.position_value is not None else "",
                    str(bt.position_value) if bt.position_value is not None else "",
                    str(pair.pos_value_delta) if pair.pos_value_delta is not None else "",
                    "1" if pair.state_diverged else "0",
                ])

        emitted = sum(1 for p in self._position_pairs if p.live is not None)
        logger.info("Exported %d position pairs to %s", emitted, path)

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

        # 0034: emit position_comparison.csv only when at least one paired
        # row exists. Migrated DB with no backtest rows yet → no file
        # (this is the only intentional silent path; un-migrated DBs raise
        # at load time in position_loader.py).
        if any(p.live is not None for p in self._position_pairs):
            position_path = output_dir / "position_comparison.csv"
            self.export_position_comparison(position_path)
            paths["position_comparison"] = position_path

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
            f"    Non-USDT collateral drift: {m.non_usdt_collateral_drift_total}",
            "",
            "  POSITION TELEMETRY",
            f"    Pairs compared:        {m.position_pairs_compared}",
            f"    Unmatched (bt):        {m.position_pairs_unmatched_bt}",
            f"    State diverged:        {m.position_pairs_state_diverged}",
            f"    Missing telemetry:     {m.position_pairs_missing_telemetry}",
            f"    IM mean |delta|:       {m.position_im_mean_abs_delta}",
            f"    IM max |delta|:        {m.position_im_max_abs_delta}",
            f"    MM mean |delta|:       {m.position_mm_mean_abs_delta}",
            f"    MM max |delta|:        {m.position_mm_max_abs_delta}",
            f"    Liq price mean:        {m.liq_price_mean_abs_delta}",
            f"    Liq price max:         {m.liq_price_max_abs_delta}",
            f"    Unrealised mean:       {m.unrealised_pnl_mean_abs_delta}",
            f"    Unrealised max:        {m.unrealised_pnl_max_abs_delta}",
            f"    Cum realised final:    {m.cum_realised_pnl_final_delta}",
            f"    Cur realised final:    {m.cur_realised_pnl_final_delta}",
            # 0059: per-snapshot |delta| families. Labels deliberately differ
            # from the "* final" lines above so the two cannot be conflated.
            f"    Upnl mean |delta|:        {m.upnl_usdt_mean_abs_delta}",
            f"    Upnl max |delta|:         {m.upnl_usdt_max_abs_delta}",
            f"    Cur realised mean |delta|:{m.cur_realised_usdt_mean_abs_delta}",
            f"    Cur realised max |delta|: {m.cur_realised_usdt_max_abs_delta}",
            f"    Cum realised mean |delta|:{m.cum_realised_usdt_mean_abs_delta}",
            f"    Cum realised max |delta|: {m.cum_realised_usdt_max_abs_delta}",
            f"    Pos value mean |delta|:   {m.pos_value_usdt_mean_abs_delta}",
            f"    Pos value max |delta|:    {m.pos_value_usdt_max_abs_delta}",
            f"    Pos value final:          {m.pos_value_final_delta}",
            "",
            "=" * 60,
        ]
        print("\n".join(lines))
