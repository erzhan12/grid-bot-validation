"""Shared-wallet ground-truth reads and replayed-curve reconciliation."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from grid_db import WalletSnapshotRepository

from live_check.window import Window, to_naive_utc


@dataclass(frozen=True)
class WalletCurvePoint:
    """Materialized account-level wallet snapshot fields."""

    exchange_ts: datetime
    total_equity: Optional[Decimal]
    total_margin_balance: Optional[Decimal]
    account_mm_rate: Optional[Decimal]


@dataclass(frozen=True)
class SharedWalletDiff:
    """Replay-vs-recorded shared wallet reconciliation metrics."""

    max_equity_delta: Decimal
    final_equity_delta: Decimal
    max_margin_balance_delta: Decimal
    max_account_mm_rate_delta: Decimal
    equity_points: int
    margin_balance_points: int
    account_mm_rate_points: int


def load_wallet_curve(
    session: Session,
    run_id: str,
    account_id: str,
    coin: str,
    window: Window,
) -> list[WalletCurvePoint]:
    """Load account wallet snapshots, run-filtered and end-anchor de-duped.

    ``WalletSnapshotRepository.get_by_account_range`` filters account, coin and
    inclusive bounds but not run_id, so this function post-filters by run_id.
    The at-window-end anchor comes from the run-scoped repository method and
    supersedes any range row at the same ``exchange_ts``.
    """
    repo = WalletSnapshotRepository(session)
    rows = [
        row for row in repo.get_by_account_range(
            account_id,
            coin,
            to_naive_utc(window.start),
            to_naive_utc(window.end),
        )
        if row.run_id == run_id and row.coin == coin
    ]
    anchor = repo.get_latest_before(
        run_id,
        account_id,
        coin,
        to_naive_utc(window.end),
    )
    by_ts = {row.exchange_ts: row for row in rows}
    if anchor is not None:
        by_ts[anchor.exchange_ts] = anchor

    return [
        WalletCurvePoint(
            exchange_ts=row.exchange_ts,
            total_equity=row.total_equity,
            total_margin_balance=row.total_margin_balance,
            account_mm_rate=row.account_mm_rate,
        )
        for row in sorted(by_ts.values(), key=lambda r: r.exchange_ts)
    ]


def reconcile_wallet_curve(
    replay_curve: Iterable,
    recorded_curve: list[WalletCurvePoint],
) -> SharedWalletDiff:
    """Compare replayed account samples at recorded timestamps.

    Recorded NULL fields are skipped per field, never coerced to zero. Replayed
    values are sampled nearest at-or-before the recorded timestamp.
    """
    replay = sorted(replay_curve, key=lambda p: p.exchange_ts)
    equity_deltas: list[Decimal] = []
    margin_deltas: list[Decimal] = []
    mm_rate_deltas: list[Decimal] = []
    final_equity_delta = Decimal("0")
    idx = -1
    for point in recorded_curve:
        while (
            idx + 1 < len(replay)
            and replay[idx + 1].exchange_ts <= point.exchange_ts
        ):
            idx += 1
        if idx < 0:
            continue
        sample = replay[idx]
        if point.total_equity is not None:
            delta = sample.total_equity - point.total_equity
            equity_deltas.append(abs(delta))
            final_equity_delta = abs(delta)
        if point.total_margin_balance is not None:
            margin_deltas.append(
                abs(sample.total_margin_balance - point.total_margin_balance)
            )
        if point.account_mm_rate is not None:
            mm_rate_deltas.append(
                abs(sample.account_mm_rate - point.account_mm_rate)
            )

    return SharedWalletDiff(
        max_equity_delta=max(equity_deltas, default=Decimal("0")),
        final_equity_delta=final_equity_delta,
        max_margin_balance_delta=max(margin_deltas, default=Decimal("0")),
        max_account_mm_rate_delta=max(mm_rate_deltas, default=Decimal("0")),
        equity_points=len(equity_deltas),
        margin_balance_points=len(margin_deltas),
        account_mm_rate_points=len(mm_rate_deltas),
    )
