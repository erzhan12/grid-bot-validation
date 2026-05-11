"""Position telemetry pairing and delta metrics (feature 0034).

Pairs ``source='live'`` and ``source='backtest'`` position snapshots
with a per-side monotonic two-pointer:

* Live snapshots are event-driven (every Bybit position change).
* Backtest snapshots are fill-driven.
* Each live row pairs with at most one backtest row.

For each pair, computes per-field deltas and recomputes ``unrealised_pnl``
on both sides against ``live.mark_price`` so the delta is apples-to-apples
(each side's own ``size, entry_price``, the same mark).

NULL telemetry is handled per-field: a pair with ``live.position_im=None``
still emits per-pair deltas for the other fields and increments
``position_pairs_missing_telemetry``.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from gridcore.pnl import calc_unrealised_pnl

from grid_db.models import PositionSnapshot

from comparator.metrics import ValidationMetrics


logger = logging.getLogger(__name__)


PAIR_TOLERANCE_S = 5
"""Maximum exchange_ts gap allowed between paired live/backtest snapshots."""


@dataclass
class PositionComparisonPair:
    """One paired live/backtest position snapshot with computed deltas."""

    side: str  # 'Buy' (long) or 'Sell' (short)
    live: PositionSnapshot
    backtest: PositionSnapshot

    # Recomputed unrealized against live.mark_price for apples-to-apples.
    # None when live.mark_price is missing (the recomputation has no anchor).
    unrealised_pnl_recomputed_live: Optional[Decimal] = None
    unrealised_pnl_recomputed_bt: Optional[Decimal] = None

    # Per-field deltas. Decimal when both sides non-NULL; None otherwise.
    # Sign convention: backtest minus live.
    position_im_delta: Optional[Decimal] = None
    position_mm_delta: Optional[Decimal] = None
    liq_price_delta: Optional[Decimal] = None
    unrealised_pnl_delta: Optional[Decimal] = None
    cum_realised_pnl_delta: Optional[Decimal] = None

    # True when any per-field delta is None due to NULL telemetry on either
    # side (other fields may still have populated deltas).
    has_missing_telemetry: bool = False


def _safe_sub(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
    """``a - b`` returning None if either operand is None."""
    if a is None or b is None:
        return None
    return a - b


def _direction_for_side(side: str) -> str:
    """Map Bybit position side ('Buy'/'Sell') to gridcore direction string."""
    return "long" if side == "Buy" else "short"


def _build_pair(
    live: PositionSnapshot, bt: PositionSnapshot
) -> PositionComparisonPair:
    direction = _direction_for_side(live.side)
    mark = live.mark_price

    pair = PositionComparisonPair(
        side=live.side,
        live=live,
        backtest=bt,
        position_im_delta=_safe_sub(bt.position_im, live.position_im),
        position_mm_delta=_safe_sub(bt.position_mm, live.position_mm),
        liq_price_delta=_safe_sub(bt.liq_price, live.liq_price),
        cum_realised_pnl_delta=_safe_sub(bt.cum_realised_pnl, live.cum_realised_pnl),
    )

    if mark is not None:
        pair.unrealised_pnl_recomputed_live = calc_unrealised_pnl(
            direction, live.entry_price, mark, live.size,
        )
        pair.unrealised_pnl_recomputed_bt = calc_unrealised_pnl(
            direction, bt.entry_price, mark, bt.size,
        )
        pair.unrealised_pnl_delta = (
            pair.unrealised_pnl_recomputed_bt - pair.unrealised_pnl_recomputed_live
        )
    else:
        # mark_price missing → recomputation impossible; mark the row.
        pair.has_missing_telemetry = True

    # Field-by-field NULL detection (any None delta where both should be Decimal).
    if (
        pair.position_im_delta is None
        or pair.position_mm_delta is None
        or pair.liq_price_delta is None
        or pair.cum_realised_pnl_delta is None
    ):
        pair.has_missing_telemetry = True

    return pair


class PositionComparator:
    """Pairs live/backtest snapshots per-side and computes aggregate metrics."""

    def __init__(self, pair_tolerance_s: int = PAIR_TOLERANCE_S):
        self._tolerance_s = pair_tolerance_s

    def pair_and_compare(
        self,
        live_snapshots: list[PositionSnapshot],
        bt_snapshots: list[PositionSnapshot],
    ) -> list[PositionComparisonPair]:
        """Pair backtest snapshots with the next live snapshot within tolerance.

        Per-side monotonic two-pointer with explicit consume step: each
        live row is claimed by at most one backtest row. Bt rows that
        do not find a live row within ``pair_tolerance_s`` increment
        ``position_pairs_unmatched_bt`` (counted by ``fold_metrics_into``).
        """
        pairs: list[PositionComparisonPair] = []
        for side in ("Buy", "Sell"):
            live_side = [s for s in live_snapshots if s.side == side]
            bt_side = [s for s in bt_snapshots if s.side == side]
            pairs.extend(self._pair_side(live_side, bt_side))
        return pairs

    def _pair_side(
        self,
        live: list[PositionSnapshot],
        bt: list[PositionSnapshot],
    ) -> list[PositionComparisonPair]:
        out: list[PositionComparisonPair] = []
        live_idx = 0
        for bt_row in bt:
            bt_ts = bt_row.exchange_ts
            # Advance live_idx to the first live row with ts >= bt_ts.
            while live_idx < len(live) and live[live_idx].exchange_ts < bt_ts:
                live_idx += 1
            if live_idx >= len(live):
                # No future live row to claim — bt unmatched.
                out.append(_unmatched_bt_marker(bt_row))
                continue
            live_row = live[live_idx]
            gap = (live_row.exchange_ts - bt_ts).total_seconds()
            if gap > self._tolerance_s:
                # Within tolerance check failed; bt unmatched. Do NOT
                # advance live_idx — the next bt row may legitimately
                # claim this live row.
                out.append(_unmatched_bt_marker(bt_row))
                continue
            out.append(_build_pair(live_row, bt_row))
            live_idx += 1  # Consume: one-to-one invariant.
        return out

    def fold_metrics_into(
        self,
        metrics: ValidationMetrics,
        pairs: list[PositionComparisonPair],
    ) -> None:
        """Mutate ``metrics`` with the 12 new aggregate fields (idempotent)."""
        matched = [p for p in pairs if p.backtest is not None and p.live is not None]
        unmatched = [p for p in pairs if p.backtest is not None and p.live is None]

        metrics.position_pairs_compared = len(matched)
        metrics.position_pairs_unmatched_bt = len(unmatched)
        metrics.position_pairs_missing_telemetry = sum(
            1 for p in matched if p.has_missing_telemetry
        )

        def _agg(field_name: str) -> tuple[Decimal, Decimal]:
            """Mean abs / max abs over matched pairs, skipping None per-field."""
            vals = [
                abs(getattr(p, field_name))
                for p in matched
                if getattr(p, field_name) is not None
            ]
            if not vals:
                return Decimal("0"), Decimal("0")
            mean = Decimal(str(statistics.mean([float(v) for v in vals])))
            return mean, max(vals)

        metrics.position_im_mean_abs_delta, metrics.position_im_max_abs_delta = _agg(
            "position_im_delta"
        )
        metrics.position_mm_mean_abs_delta, metrics.position_mm_max_abs_delta = _agg(
            "position_mm_delta"
        )
        metrics.liq_price_mean_abs_delta, metrics.liq_price_max_abs_delta = _agg(
            "liq_price_delta"
        )
        metrics.unrealised_pnl_mean_abs_delta, metrics.unrealised_pnl_max_abs_delta = _agg(
            "unrealised_pnl_delta"
        )

        # cum_realised_pnl: compare the FINAL value delta only (per Step 4
        # design — per-pair rounding noise accumulates over long sessions).
        # In hedge mode `cum_realised_pnl` is per-direction, so aggregate
        # the last-pair delta for EACH side. Without per-side aggregation,
        # `matched[-1]` would always be "last Sell pair" (Buy pairs are
        # appended first), masking long-only divergence.
        per_side_final: dict[str, Decimal] = {}
        for pair in matched:
            if pair.cum_realised_pnl_delta is None:
                continue
            per_side_final[pair.side] = pair.cum_realised_pnl_delta
        metrics.cum_realised_pnl_final_delta = sum(
            per_side_final.values(), Decimal("0"),
        )


def _unmatched_bt_marker(bt: PositionSnapshot) -> PositionComparisonPair:
    """Sentinel pair for an unmatched backtest snapshot.

    ``live`` is filled with ``None`` via the ``backtest=bt, live=None``
    sentinel pattern — but the dataclass requires a ``PositionSnapshot``;
    use a separate flag instead. We model this as a pair with the backtest
    side set, the live side None, and ``has_missing_telemetry=True``.
    """
    # The dataclass demands ``live: PositionSnapshot``; we use an attribute
    # override pattern via ``object.__setattr__`` to allow None on the
    # mutable dataclass without rewriting the type hint.
    pair = PositionComparisonPair.__new__(PositionComparisonPair)
    pair.side = bt.side
    pair.live = None  # type: ignore[assignment]
    pair.backtest = bt
    pair.unrealised_pnl_recomputed_live = None
    pair.unrealised_pnl_recomputed_bt = None
    pair.position_im_delta = None
    pair.position_mm_delta = None
    pair.liq_price_delta = None
    pair.unrealised_pnl_delta = None
    pair.cum_realised_pnl_delta = None
    pair.has_missing_telemetry = True
    return pair


__all__ = [
    "PAIR_TOLERANCE_S",
    "PositionComparator",
    "PositionComparisonPair",
]
