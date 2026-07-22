"""Shared-wallet ground-truth reads and replayed-curve reconciliation."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from grid_db import WalletSnapshot, WalletSnapshotRepository

from live_check.window import Window, to_naive_utc


@dataclass(frozen=True)
class WalletCurvePoint:
    """Materialized futures-basis wallet snapshot fields."""

    exchange_ts: datetime
    total_equity: Optional[Decimal]
    total_margin_balance: Optional[Decimal]
    account_mm_rate: Optional[Decimal]


def _decoded_raw_json(raw_json: Any) -> Optional[dict[str, Any]]:
    """Return a wallet raw_json dict, including one double-encoded layer."""
    if raw_json is None:
        return None
    raw = raw_json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    return raw


def _futures_equity(row: WalletSnapshot, coin: str) -> Optional[Decimal]:
    """Return USDT futures equity from per-coin raw_json, not account total."""
    raw = _decoded_raw_json(row.raw_json)
    if raw is None or raw.get("coin") != coin:
        return None
    if row.wallet_balance is None or "unrealisedPnl" not in raw:
        return None
    try:
        result = row.wallet_balance + Decimal(str(raw["unrealisedPnl"]))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Decimal("NaN")/("Infinity") parse WITHOUT raising — reject non-finite
    # so a malformed value can never poison the reconcile max-diff.
    return result if result.is_finite() else None


def _futures_mm_rate(row: WalletSnapshot, coin: str) -> Optional[Decimal]:
    """Return top-level position MM divided by futures equity, if available."""
    futures_equity = _futures_equity(row, coin)
    raw = _decoded_raw_json(row.raw_json)
    if futures_equity is None or futures_equity == 0 or raw is None:
        return None
    if "totalPositionMM" not in raw:
        return None
    try:
        result = Decimal(str(raw["totalPositionMM"])) / futures_equity
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


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
            total_equity=_futures_equity(row, coin),
            total_margin_balance=_futures_equity(row, coin),
            account_mm_rate=_futures_mm_rate(row, coin),
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

    Both timestamp streams are normalized to naive UTC before comparison so a
    mixed tz-aware/tz-naive pair cannot raise a TypeError mid-walk (the prod
    SQLite path is naive on both sides; this guards injected/aware inputs).
    """
    replay = sorted(replay_curve, key=lambda p: to_naive_utc(p.exchange_ts))
    replay_ts = [to_naive_utc(p.exchange_ts) for p in replay]
    equity_deltas: list[Decimal] = []
    margin_deltas: list[Decimal] = []
    mm_rate_deltas: list[Decimal] = []
    final_equity_delta = Decimal("0")
    idx = -1
    for point in recorded_curve:
        point_ts = to_naive_utc(point.exchange_ts)
        while (
            idx + 1 < len(replay)
            and replay_ts[idx + 1] <= point_ts
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
