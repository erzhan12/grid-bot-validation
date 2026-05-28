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
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from gridcore.pnl import calc_unrealised_pnl

from grid_db.models import PositionSnapshot

from comparator.metrics import ValidationMetrics


logger = logging.getLogger(__name__)


PAIR_TOLERANCE_S = 5
"""Maximum exchange_ts gap allowed between paired live/backtest snapshots."""

STATE_SIZE_TOLERANCE = Decimal("0.001")
"""Default |live.size - bt.size| above which a pair is state-diverged.

Sized to cover float / tier-rounding noise. LTCUSDT qty_step is 0.1 →
0.001 is well below one quantum, so any real position drift exceeds
this threshold immediately.
"""

STATE_ENTRY_REL_TOLERANCE = Decimal("0.001")
"""Default relative entry-price drift |Δentry/live.entry| → state-diverged.

0.1% is roughly the worst-case rounding within a single fill quantum;
real divergence (missed fills, manual interventions) far exceeds this.
"""


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
    # 0056: per-pair delta of the cycle-scoped realized PnL counter. NOT
    # included in the `has_missing_telemetry` NULL-detection block below —
    # pre-0056 rows have NULL here and treating that as missing telemetry
    # would universally trip the flag for legacy replays.
    cur_realised_pnl_delta: Optional[Decimal] = None
    # 0059: per-snapshot delta of the STORED unrealised_pnl (bt - live),
    # 1:1 with the 0058 `upnl_usdt` log line. Distinct from
    # `unrealised_pnl_delta`, which is recomputed against `live.mark_price`.
    upnl_usdt_delta: Optional[Decimal] = None
    # 0059: per-snapshot delta of position notional (size * entry_price).
    # Like `cur_realised_pnl_delta`, NOT in the has_missing_telemetry block:
    # pre-0059 rows are NULL and would universally trip the flag.
    pos_value_delta: Optional[Decimal] = None

    # True when any per-field delta is None due to NULL telemetry on either
    # side (other fields may still have populated deltas).
    has_missing_telemetry: bool = False

    # 0044: pair matched by exchange_ts but live and backtest hold different
    # position state (size or entry price beyond tolerance). The deltas above
    # are still computed for diagnostic CSV output, but ``fold_metrics_into``
    # excludes them from mean/max abs aggregates so artefacts like operator
    # manual interventions (recorded live, absent from grid replay) don't
    # pollute the headline `liq_price_max_abs_delta` metric.
    state_diverged: bool = False


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
        cur_realised_pnl_delta=_safe_sub(bt.cur_realised_pnl, live.cur_realised_pnl),
        # 0059: stored-value parity (NOT recomputed) + position notional.
        upnl_usdt_delta=_safe_sub(bt.unrealised_pnl, live.unrealised_pnl),
        pos_value_delta=_safe_sub(bt.position_value, live.position_value),
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

    def __init__(
        self,
        pair_tolerance_s: int = PAIR_TOLERANCE_S,
        state_size_tolerance: Decimal = STATE_SIZE_TOLERANCE,
        state_entry_rel_tolerance: Decimal = STATE_ENTRY_REL_TOLERANCE,
    ):
        self._tolerance_s = pair_tolerance_s
        self._size_tol = state_size_tolerance
        self._entry_rel_tol = state_entry_rel_tolerance

    def _state_diverged(
        self, live: PositionSnapshot, bt: PositionSnapshot,
    ) -> bool:
        """0044: detect whether a paired snapshot's underlying state has drifted.

        Returns True when |live.size − bt.size| exceeds ``state_size_tolerance``
        OR the relative entry-price drift exceeds ``state_entry_rel_tolerance``.
        Both-zero positions never diverge (closed-on-both-sides matches
        trivially). When one side is zero and the other isn't, the size
        delta triggers the divergence flag.
        """
        l_size = live.size or Decimal("0")
        b_size = bt.size or Decimal("0")
        if l_size == 0 and b_size == 0:
            return False
        if abs(l_size - b_size) > self._size_tol:
            return True
        l_entry = live.entry_price or Decimal("0")
        b_entry = bt.entry_price or Decimal("0")
        if l_entry > 0:
            if abs(l_entry - b_entry) / l_entry > self._entry_rel_tol:
                return True
        return False

    def pair_and_compare(
        self,
        live_snapshots: list[PositionSnapshot],
        bt_snapshots: list[PositionSnapshot],
    ) -> list[PositionComparisonPair]:
        """Pair backtest snapshots with the first live snapshot within tolerance.

        Per-side monotonic two-pointer with explicit consume step: each
        live row is claimed by at most one backtest row. Bt rows that
        do not find a live row within ``+/-pair_tolerance_s`` increment
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
        tolerance = timedelta(seconds=self._tolerance_s)
        for bt_row in bt:
            bt_ts = bt_row.exchange_ts
            lower_bound = bt_ts - tolerance
            upper_bound = bt_ts + tolerance

            # Drop stale live rows that cannot match this or any future bt row.
            while live_idx < len(live) and live[live_idx].exchange_ts < lower_bound:
                live_idx += 1
            if live_idx >= len(live):
                # No unclaimed live row remains — bt unmatched.
                out.append(_unmatched_bt_marker(bt_row))
                continue

            live_row = live[live_idx]
            if live_row.exchange_ts > upper_bound:
                # No live row inside the bidirectional tolerance window. Do
                # not advance live_idx; the next bt row may claim this live row.
                out.append(_unmatched_bt_marker(bt_row))
                continue
            pair = _build_pair(live_row, bt_row)
            # 0044: flag (but keep) pairs where position state has drifted
            # between live and backtest. The pair is still emitted with
            # deltas for diagnostic CSV inspection, but folded out of the
            # mean/max aggregates so operator manual fills don't pollute
            # the headline metric.
            if self._state_diverged(live_row, bt_row):
                pair.state_diverged = True
                logger.debug(
                    "Position pair state diverged at exchange_ts=%s "
                    "side=%s: live(size=%s, entry=%s) vs bt(size=%s, entry=%s)",
                    bt_row.exchange_ts, live_row.side,
                    live_row.size, live_row.entry_price,
                    bt_row.size, bt_row.entry_price,
                )
            out.append(pair)
            live_idx += 1  # Consume: one-to-one invariant.
        return out

    def fold_metrics_into(
        self,
        metrics: ValidationMetrics,
        pairs: list[PositionComparisonPair],
    ) -> None:
        """Mutate ``metrics`` with the 21 telemetry aggregate fields (idempotent).

        21 total = 12 pre-existing + 9 from 0059.
        0059 adds nine per-snapshot aggregates (upnl/cur/cum/pos-value
        mean+max |delta| and pos_value_final_delta) alongside the original
        12. The new per-snapshot mean/max families sum |delta| across ALL
        matched pairs, whereas the ``*_final_delta`` fields keep only the
        last per-side value; both are retained because intermediate drift
        that cancels out is invisible to a final-only comparison.
        """
        all_matched = [
            p for p in pairs if p.backtest is not None and p.live is not None
        ]
        unmatched = [p for p in pairs if p.backtest is not None and p.live is None]
        # 0044: state-diverged pairs are matched-by-timestamp but excluded
        # from delta aggregates. They are counted separately so reporter
        # output makes the artefact visible without polluting the metric.
        diverged = [p for p in all_matched if p.state_diverged]
        matched = [p for p in all_matched if not p.state_diverged]

        metrics.position_pairs_compared = len(matched)
        metrics.position_pairs_unmatched_bt = len(unmatched)
        metrics.position_pairs_state_diverged = len(diverged)
        metrics.position_pairs_missing_telemetry = sum(
            1 for p in matched if p.has_missing_telemetry
        )

        def _agg(field_name: str) -> tuple[Decimal, Decimal]:
            """Mean abs / max abs over matched pairs, skipping None per-field.

            Pure Decimal arithmetic — no float roundtrip — so cumulative
            rounding from many small deltas stays exact. Important because
            `position_im` / `position_mm` deltas can be 1e-8 USDT per pair
            and the acceptance gate is set at 0.05 USDT total.
            """
            vals = [
                abs(getattr(p, field_name))
                for p in matched
                if getattr(p, field_name) is not None
            ]
            if not vals:
                return Decimal("0"), Decimal("0")
            mean = sum(vals, Decimal("0")) / Decimal(len(vals))
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
        # 0059: per-snapshot mean/max |delta| families. cur/cum reuse the
        # existing per-pair delta fields; upnl/pos_value use the new ones.
        metrics.upnl_usdt_mean_abs_delta, metrics.upnl_usdt_max_abs_delta = _agg(
            "upnl_usdt_delta"
        )
        metrics.cur_realised_usdt_mean_abs_delta, metrics.cur_realised_usdt_max_abs_delta = _agg(
            "cur_realised_pnl_delta"
        )
        metrics.cum_realised_usdt_mean_abs_delta, metrics.cum_realised_usdt_max_abs_delta = _agg(
            "cum_realised_pnl_delta"
        )
        metrics.pos_value_usdt_mean_abs_delta, metrics.pos_value_usdt_max_abs_delta = _agg(
            "pos_value_delta"
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

        # 0056: same per-side last-pair aggregation as `cum_realised_pnl`.
        # The cycle counter retains the just-closed cycle total between
        # close and the next opening fill, so the last observed delta per
        # side captures either an in-progress cycle or a just-completed
        # one. NULL pairs (pre-0056 rows) are skipped.
        cur_per_side_final: dict[str, Decimal] = {}
        for pair in matched:
            if pair.cur_realised_pnl_delta is None:
                continue
            cur_per_side_final[pair.side] = pair.cur_realised_pnl_delta
        metrics.cur_realised_pnl_final_delta = sum(
            cur_per_side_final.values(), Decimal("0"),
        )

        # 0059: same per-side last-pair aggregation for position value.
        # Reporting tradeoff: equal-and-opposite long/short position-value
        # drift can cancel in this scalar sum, so operators should rely on
        # pos_value_usdt_mean/max_abs_delta (which sum |delta| across all
        # matched pairs) for hedge-mode diagnosis, not this scalar alone.
        pos_value_per_side_final: dict[str, Decimal] = {}
        for pair in matched:
            if pair.pos_value_delta is None:
                continue
            pos_value_per_side_final[pair.side] = pair.pos_value_delta
        metrics.pos_value_final_delta = sum(
            pos_value_per_side_final.values(), Decimal("0"),
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
    pair.cur_realised_pnl_delta = None
    pair.upnl_usdt_delta = None  # 0059
    pair.pos_value_delta = None  # 0059
    pair.has_missing_telemetry = True
    return pair


__all__ = [
    "PAIR_TOLERANCE_S",
    "PositionComparator",
    "PositionComparisonPair",
]
