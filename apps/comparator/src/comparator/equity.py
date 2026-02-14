"""Equity curve comparison between live wallet snapshots and backtest equity.

Loads both curves, resamples to a common time grid, computes divergence metrics,
and exports the comparison to CSV.
"""

import csv
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

from sqlalchemy.orm import Session

from grid_db import WalletSnapshotRepository

from comparator.loader import _normalize_ts
from comparator.metrics import _pearson_correlation

logger = logging.getLogger(__name__)

EquityPoint = tuple[datetime, Decimal]
ResampledRow = tuple[datetime, Optional[Decimal], Optional[Decimal]]


class EquityComparator:
    """Compare live and backtest equity curves.

    Loads both curves, resamples to a common time grid, computes divergence
    metrics, and exports the comparison to CSV.
    """

    def load_live(
        self,
        session: Session,
        account_id: str,
        coin: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> list[EquityPoint]:
        """Load live equity curve from WalletSnapshot records.

        Args:
            session: SQLAlchemy session.
            account_id: Bybit account ID.
            coin: Coin for wallet balance (e.g., 'USDT').
            start_ts: Start of time window.
            end_ts: End of time window.

        Returns:
            List of (timestamp, wallet_balance) sorted by time.
        """
        repo = WalletSnapshotRepository(session)
        snapshots = repo.get_by_account_range(account_id, coin, start_ts, end_ts)

        points = [(_normalize_ts(s.exchange_ts), s.wallet_balance) for s in snapshots]
        logger.info("Loaded %d live equity points for account %s", len(points), account_id)
        return points

    def load_backtest_from_csv(self, path: Union[str, Path]) -> list[EquityPoint]:
        """Load backtest equity curve from CSV export.

        Expects columns: timestamp, equity, return_pct

        Args:
            path: Path to backtest equity CSV.

        Returns:
            List of (timestamp, equity) sorted by time.
        """
        points: list[EquityPoint] = []
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = _normalize_ts(datetime.fromisoformat(row["timestamp"]))
                equity = Decimal(row["equity"])
                points.append((ts, equity))

        points.sort(key=lambda p: p[0])
        logger.info("Loaded %d backtest equity points from CSV", len(points))
        return points

    def load_backtest_from_session(
        self,
        equity_curve: list[tuple[datetime, Decimal]],
    ) -> list[EquityPoint]:
        """Load backtest equity curve from BacktestSession.equity_curve.

        Args:
            equity_curve: List of (timestamp, equity) from BacktestSession.

        Returns:
            List of (timestamp, equity) sorted by time.
        """
        points = [(_normalize_ts(ts), eq) for ts, eq in equity_curve]
        points.sort(key=lambda p: p[0])
        logger.info("Loaded %d backtest equity points from session", len(points))
        return points

    def resample(
        self,
        live: list[EquityPoint],
        backtest: list[EquityPoint],
        interval: timedelta = timedelta(hours=1),
    ) -> list[ResampledRow]:
        """Resample both curves to a common time grid.

        For each bucket, takes the last value that falls within it.
        If one curve has no data in a bucket, that value is None.

        Args:
            live: Live equity points.
            backtest: Backtest equity points.
            interval: Time bucket width.

        Returns:
            List of (timestamp, live_equity, backtest_equity) tuples.
        """
        if not live and not backtest:
            return []

        # Determine overall time range
        all_ts = [p[0] for p in live] + [p[0] for p in backtest]
        start = min(all_ts)
        end = max(all_ts)

        resampled: list[ResampledRow] = []
        bucket_start = start
        live_idx = 0
        bt_idx = 0

        while bucket_start <= end:
            bucket_end = bucket_start + interval

            # Find last live value in this bucket
            live_val: Optional[Decimal] = None
            while live_idx < len(live) and live[live_idx][0] < bucket_end:
                live_val = live[live_idx][1]
                live_idx += 1

            # Find last backtest value in this bucket
            bt_val: Optional[Decimal] = None
            while bt_idx < len(backtest) and backtest[bt_idx][0] < bucket_end:
                bt_val = backtest[bt_idx][1]
                bt_idx += 1

            if live_val is not None or bt_val is not None:
                resampled.append((bucket_start, live_val, bt_val))

            bucket_start = bucket_end

        logger.info("Resampled to %d common grid points", len(resampled))
        return resampled

    def compute_metrics(
        self,
        resampled: list[ResampledRow],
    ) -> tuple[Decimal, Decimal, float]:
        """Compute equity divergence metrics from resampled grid.

        Args:
            resampled: Output from resample().

        Returns:
            Tuple of (max_divergence, mean_divergence, correlation).
        """
        divergences: list[Decimal] = []
        live_vals: list[float] = []
        bt_vals: list[float] = []

        for _, live_eq, bt_eq in resampled:
            if live_eq is not None and bt_eq is not None:
                divergences.append(abs(bt_eq - live_eq))
                live_vals.append(float(live_eq))
                bt_vals.append(float(bt_eq))

        if not divergences:
            return Decimal("0"), Decimal("0"), 0.0

        max_div = max(divergences)
        mean_div = sum(divergences) / len(divergences)
        correlation = _pearson_correlation(live_vals, bt_vals)

        return max_div, mean_div, correlation

    def export(
        self,
        resampled: list[ResampledRow],
        path: Union[str, Path],
    ) -> None:
        """Export equity comparison to CSV.

        Args:
            resampled: Output from resample().
            path: Output CSV path.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "live_equity", "backtest_equity", "divergence"])

            for ts, live_eq, bt_eq in resampled:
                divergence = ""
                if live_eq is not None and bt_eq is not None:
                    divergence = str(bt_eq - live_eq)

                writer.writerow([
                    ts.isoformat(),
                    str(live_eq) if live_eq is not None else "",
                    str(bt_eq) if bt_eq is not None else "",
                    divergence,
                ])

        logger.info("Exported equity comparison (%d points) to %s", len(resampled), path)
