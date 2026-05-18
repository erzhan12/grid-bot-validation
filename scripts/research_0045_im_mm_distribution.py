"""Feature 0045 Phase 1 — derive hedge-aware IM/MM distribution rule.

Read-only analytics. Pairs ``(symbol, local_ts)`` long/short snapshots
from ``position_snapshots`` (``source='live'``), then scores six
candidate distribution rules against the observed
``position_im`` / ``position_mm`` values from Bybit's UTA hedge mode.

Pass / fail rule (per ``docs/features/0045_PLAN.md`` Phase 1):

    max |Δ_IM| ≤ 1 USDT  AND  max |Δ_MM| ≤ 0.1 USDT

across the entire paired dataset.

Hypotheses (entry-PV and mark-PV variants):

    H1 — notional-weighted (entry):  X_leg = X_combined * leg_pv / combined_pv
    H2 — dominant-full + residual:   X_dom = single_leg(dom_pv);
                                     X_smaller = X_combined - X_dom
    H3 — max-of-singletons:          X_leg = single_leg(leg_pv)   (baseline)
    H4 — H1 / H2 with leg_pv = size * mark_price
    H5 — dominant-pool with hedge-credited residual:
           combined_input = max + alpha * min  (alpha ∈ {0, 0.25, 0.5})
    H6 — direction-asymmetric singleton formulas

Output: markdown-style table per hypothesis, plus a header with the
loaded snapshot count, run_id range, and DB path so the result can be
re-derived later.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Iterable, Optional

# Local imports — pull gridcore primitives without a custom resolver.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "packages" / "gridcore" / "src"))
sys.path.insert(0, str(_REPO / "apps" / "backtest" / "src"))

from gridcore.pnl import (  # noqa: E402
    MMTiers,
    calc_initial_margin,
    calc_maintenance_margin,
)
from backtest.runner import BacktestRunner  # noqa: E402

getcontext().prec = 40

_ZERO = Decimal("0")
IM_PASS_THRESHOLD = Decimal("1")
MM_PASS_THRESHOLD = Decimal("0.1")


@dataclass(frozen=True)
class PairedSnapshot:
    """One paired (long, short) observation at a single ``local_ts``."""

    local_ts: str
    exchange_ts: str
    mark_price: Decimal
    leverage: Decimal
    L_size: Decimal
    L_entry: Decimal
    S_size: Decimal
    S_entry: Decimal
    live_im_L: Decimal
    live_im_S: Decimal
    live_mm_L: Decimal
    live_mm_S: Decimal

    @property
    def L_pv_entry(self) -> Decimal:
        return self.L_size * self.L_entry

    @property
    def S_pv_entry(self) -> Decimal:
        return self.S_size * self.S_entry

    @property
    def L_pv_mark(self) -> Decimal:
        return self.L_size * self.mark_price

    @property
    def S_pv_mark(self) -> Decimal:
        return self.S_size * self.mark_price


def _D(x) -> Decimal:
    if x is None:
        return _ZERO
    return Decimal(str(x))


def load_pairs(db_path: str, symbol: str, run_id: Optional[str]) -> list[PairedSnapshot]:
    """Load and pair live position snapshots by ``(symbol, local_ts)``.

    Falls back to ``(symbol, exchange_ts)`` pairing when ``local_ts``
    pairing yields fewer than two pairs (older recordings where the
    recorder wrote shared exchange_ts).
    """
    conn = sqlite3.connect(db_path)
    where = "source='live' AND symbol=?"
    params: list = [symbol]
    if run_id:
        where += " AND run_id=?"
        params.append(run_id)

    rows = conn.execute(
        f"""
        SELECT local_ts, exchange_ts, side, size, entry_price, mark_price,
               position_im, position_mm, raw_json
        FROM position_snapshots
        WHERE {where}
        ORDER BY local_ts ASC, side ASC
        """,
        params,
    ).fetchall()
    conn.close()

    def _leverage_from_raw(raw_json: Optional[str]) -> Decimal:
        if not raw_json:
            return Decimal("1")
        import json

        try:
            return Decimal(str(json.loads(raw_json).get("leverage", "1")))
        except (json.JSONDecodeError, KeyError, ValueError, ArithmeticError):
            return Decimal("1")

    by_ts: dict[str, dict[str, tuple]] = defaultdict(dict)
    for r in rows:
        by_ts[r[0]][r[2]] = r  # local_ts → side → row

    pairs: list[PairedSnapshot] = []
    for local_ts, sides in by_ts.items():
        if "Buy" not in sides or "Sell" not in sides:
            continue
        buy = sides["Buy"]
        sell = sides["Sell"]
        # Require both sides report the same mark_price within this ts —
        # otherwise the snapshot pair straddles a tick and is unreliable.
        mark_b = _D(buy[5])
        mark_s = _D(sell[5])
        mark = mark_b if mark_b == mark_s else (mark_b + mark_s) / Decimal("2")
        pairs.append(
            PairedSnapshot(
                local_ts=str(local_ts),
                exchange_ts=str(buy[1]),
                mark_price=mark,
                leverage=_leverage_from_raw(buy[8]),
                L_size=_D(buy[3]),
                L_entry=_D(buy[4]),
                S_size=_D(sell[3]),
                S_entry=_D(sell[4]),
                live_im_L=_D(buy[6]),
                live_im_S=_D(sell[6]),
                live_mm_L=_D(buy[7]),
                live_mm_S=_D(sell[7]),
            )
        )
    return pairs


# ---------------------------------------------------------------------------
# Hypotheses
# ---------------------------------------------------------------------------


def _single_leg(
    pv: Decimal, leverage: Decimal, symbol: str, tiers: Optional[MMTiers]
) -> tuple[Decimal, Decimal]:
    """Wrap ``calc_initial_margin`` + ``calc_maintenance_margin`` for one leg."""
    if pv <= 0:
        return _ZERO, _ZERO
    im, _ = calc_initial_margin(pv, leverage, symbol, tiers=tiers)
    mm, _ = calc_maintenance_margin(pv, symbol, tiers=tiers)
    return im, mm


def _combined(
    pv: Decimal, leverage: Decimal, symbol: str, tiers: Optional[MMTiers]
) -> tuple[Decimal, Decimal]:
    """IM/MM at the COMBINED notional (tier looked up on combined_pv)."""
    return _single_leg(pv, leverage, symbol, tiers)


def h1_notional_weighted(
    pair: PairedSnapshot,
    symbol: str,
    tiers: Optional[MMTiers],
    use_mark: bool,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """H1: X_leg = X_combined * leg_pv / combined_pv."""
    Lpv = pair.L_pv_mark if use_mark else pair.L_pv_entry
    Spv = pair.S_pv_mark if use_mark else pair.S_pv_entry
    combined_pv = Lpv + Spv
    if combined_pv == _ZERO:
        return _ZERO, _ZERO, _ZERO, _ZERO
    cim, cmm = _combined(combined_pv, pair.leverage, symbol, tiers)
    return (
        cim * Lpv / combined_pv,
        cmm * Lpv / combined_pv,
        cim * Spv / combined_pv,
        cmm * Spv / combined_pv,
    )


def h2_dominant_full(
    pair: PairedSnapshot,
    symbol: str,
    tiers: Optional[MMTiers],
    use_mark: bool,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """H2: dominant leg = single_leg(dominant_pv); smaller = combined - dominant."""
    Lpv = pair.L_pv_mark if use_mark else pair.L_pv_entry
    Spv = pair.S_pv_mark if use_mark else pair.S_pv_entry
    combined_pv = Lpv + Spv
    if combined_pv == _ZERO:
        return _ZERO, _ZERO, _ZERO, _ZERO
    cim, cmm = _combined(combined_pv, pair.leverage, symbol, tiers)
    if Lpv >= Spv:
        dim, dmm = _single_leg(Lpv, pair.leverage, symbol, tiers)
        return dim, dmm, max(cim - dim, _ZERO), max(cmm - dmm, _ZERO)
    dim, dmm = _single_leg(Spv, pair.leverage, symbol, tiers)
    return max(cim - dim, _ZERO), max(cmm - dmm, _ZERO), dim, dmm


def h3_max_of_singletons(
    pair: PairedSnapshot,
    symbol: str,
    tiers: Optional[MMTiers],
    use_mark: bool,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """H3: production baseline — independent per-leg single-leg formula."""
    Lpv = pair.L_pv_mark if use_mark else pair.L_pv_entry
    Spv = pair.S_pv_mark if use_mark else pair.S_pv_entry
    lim, lmm = _single_leg(Lpv, pair.leverage, symbol, tiers)
    sim, smm = _single_leg(Spv, pair.leverage, symbol, tiers)
    return lim, lmm, sim, smm


def h5_dominant_pool(
    pair: PairedSnapshot,
    symbol: str,
    tiers: Optional[MMTiers],
    use_mark: bool,
    alpha: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """H5: combined_input = max + alpha * min, distributed via H1."""
    Lpv = pair.L_pv_mark if use_mark else pair.L_pv_entry
    Spv = pair.S_pv_mark if use_mark else pair.S_pv_entry
    big = max(Lpv, Spv)
    small = min(Lpv, Spv)
    pool = big + alpha * small
    if pool == _ZERO:
        return _ZERO, _ZERO, _ZERO, _ZERO
    cim, cmm = _combined(pool, pair.leverage, symbol, tiers)
    # Distribute by raw leg PVs proportion of pool inputs.
    if Lpv >= Spv:
        Lw = Lpv / (Lpv + alpha * Spv) if (Lpv + alpha * Spv) > 0 else _ZERO
    else:
        Lw = (alpha * Lpv) / (alpha * Lpv + Spv) if (alpha * Lpv + Spv) > 0 else _ZERO
    Sw = Decimal("1") - Lw
    return cim * Lw, cmm * Lw, cim * Sw, cmm * Sw


def h6_direction_asymmetric(
    pair: PairedSnapshot,
    symbol: str,
    tiers: Optional[MMTiers],
    use_mark: bool,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """H6: dominant leg gets full single-leg; smaller leg gets a discounted rate.

    Implements the docstring observation from ``_estimate_pair_liq_prices``
    (feature 0043): the smaller leg's published margin has its own
    hedge-discount formula, independent of the dominant leg's.

    Concrete form tested here:
        smaller_X = single_leg(smaller_pv) * (smaller_pv / dominant_pv)
    (proportional reduction by relative size — a simple ansatz to test).
    """
    Lpv = pair.L_pv_mark if use_mark else pair.L_pv_entry
    Spv = pair.S_pv_mark if use_mark else pair.S_pv_entry
    if Lpv == _ZERO or Spv == _ZERO:
        # Degenerate: collapse to H3.
        return h3_max_of_singletons(pair, symbol, tiers, use_mark)
    lim_full, lmm_full = _single_leg(Lpv, pair.leverage, symbol, tiers)
    sim_full, smm_full = _single_leg(Spv, pair.leverage, symbol, tiers)
    if Lpv >= Spv:
        ratio = Spv / Lpv
        return lim_full, lmm_full, sim_full * ratio, smm_full * ratio
    ratio = Lpv / Spv
    return lim_full * ratio, lmm_full * ratio, sim_full, smm_full


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class HypothesisScore:
    name: str
    rows: list[tuple[str, Decimal, Decimal, Decimal, Decimal]]  # ts, ΔIM_L, ΔMM_L, ΔIM_S, ΔMM_S

    @property
    def max_abs_im(self) -> Decimal:
        if not self.rows:
            return _ZERO
        return max(max(abs(r[1]), abs(r[3])) for r in self.rows)

    @property
    def max_abs_mm(self) -> Decimal:
        if not self.rows:
            return _ZERO
        return max(max(abs(r[2]), abs(r[4])) for r in self.rows)

    def passes(self) -> bool:
        return (
            self.max_abs_im <= IM_PASS_THRESHOLD
            and self.max_abs_mm <= MM_PASS_THRESHOLD
        )


def _score(
    name: str,
    pairs: list[PairedSnapshot],
    rule,
) -> HypothesisScore:
    rows: list[tuple[str, Decimal, Decimal, Decimal, Decimal]] = []
    for p in pairs:
        im_L, mm_L, im_S, mm_S = rule(p)
        rows.append(
            (
                p.local_ts,
                im_L - p.live_im_L,
                mm_L - p.live_mm_L,
                im_S - p.live_im_S,
                mm_S - p.live_mm_S,
            )
        )
    return HypothesisScore(name, rows)


def all_hypotheses(
    pairs: list[PairedSnapshot], symbol: str, tiers: Optional[MMTiers]
) -> list[HypothesisScore]:
    scores: list[HypothesisScore] = []
    for use_mark, tag in ((False, "entry"), (True, "mark")):
        scores.append(_score(
            f"H1_{tag} (notional-weighted)",
            pairs,
            lambda p, um=use_mark: h1_notional_weighted(p, symbol, tiers, um),
        ))
        scores.append(_score(
            f"H2_{tag} (dominant-full + residual)",
            pairs,
            lambda p, um=use_mark: h2_dominant_full(p, symbol, tiers, um),
        ))
        scores.append(_score(
            f"H3_{tag} (max-of-singletons / baseline)",
            pairs,
            lambda p, um=use_mark: h3_max_of_singletons(p, symbol, tiers, um),
        ))
        for alpha_str in ("0", "0.25", "0.5"):
            alpha = Decimal(alpha_str)
            scores.append(_score(
                f"H5_{tag}_alpha={alpha_str} (dominant-pool)",
                pairs,
                lambda p, um=use_mark, a=alpha: h5_dominant_pool(p, symbol, tiers, um, a),
            ))
        scores.append(_score(
            f"H6_{tag} (direction-asymmetric)",
            pairs,
            lambda p, um=use_mark: h6_direction_asymmetric(p, symbol, tiers, um),
        ))
    return scores


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt_observed(pairs: Iterable[PairedSnapshot]) -> str:
    lines = [
        "| local_ts | L_size | S_size | mark | "
        "live_im_L | live_mm_L | live_im_S | live_mm_S |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in pairs:
        lines.append(
            f"| {p.local_ts} | {p.L_size} | {p.S_size} | {p.mark_price} | "
            f"{p.live_im_L} | {p.live_mm_L} | {p.live_im_S} | {p.live_mm_S} |"
        )
    return "\n".join(lines)


def _fmt_scores(scores: list[HypothesisScore]) -> str:
    lines = [
        "| hypothesis | max |ΔIM| | max |ΔMM| | passes |",
        "|---|---:|---:|:---:|",
    ]
    for s in sorted(scores, key=lambda x: x.max_abs_im + x.max_abs_mm):
        lines.append(
            f"| {s.name} | {s.max_abs_im:.4f} | {s.max_abs_mm:.4f} | "
            f"{'YES' if s.passes() else 'no'} |"
        )
    return "\n".join(lines)


def _fmt_residuals(s: HypothesisScore) -> str:
    lines = [
        f"### Residuals — {s.name}",
        "",
        "| local_ts | ΔIM_L | ΔMM_L | ΔIM_S | ΔMM_S |",
        "|---|---:|---:|---:|---:|",
    ]
    for ts, dim_l, dmm_l, dim_s, dmm_s in s.rows:
        lines.append(
            f"| {ts} | {dim_l:+.4f} | {dmm_l:+.4f} | {dim_s:+.4f} | {dmm_s:+.4f} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="Path to recorder DB (sqlite)")
    parser.add_argument("--symbol", default="LTCUSDT")
    parser.add_argument("--run-id", default=None, help="Optional: restrict to a run_id")
    parser.add_argument(
        "--risk-limits",
        default=None,
        help="Path to risk_limits_cache.json (defaults to conf/ auto-discover)",
    )
    parser.add_argument(
        "--show-residuals",
        action="store_true",
        help="Print per-row Δ table for the top-3 hypotheses",
    )
    args = parser.parse_args()

    tiers = BacktestRunner._load_mm_tiers(args.symbol, args.risk_limits)
    pairs = load_pairs(args.db, args.symbol, args.run_id)

    print(f"# Feature 0045 Phase 1 — IM/MM distribution analysis")
    print(f"")
    print(f"- DB: `{args.db}`")
    print(f"- Symbol: `{args.symbol}`")
    print(f"- Run filter: `{args.run_id or '(all)'}`")
    print(f"- Paired snapshots: **{len(pairs)}**")
    print(f"- Tier source: {'cache' if tiers else 'hardcoded default'}")
    if tiers:
        print(f"- First tier: max={tiers[0][0]}, mmr={tiers[0][1]}, "
              f"ded={tiers[0][2]}, imr={tiers[0][3]}")
    print(f"")
    if not pairs:
        print("No paired snapshots found. Phase 1 cannot proceed.")
        return 1

    print("## Observed live values")
    print()
    print(_fmt_observed(pairs))
    print()

    scores = all_hypotheses(pairs, args.symbol, tiers)
    print("## Hypothesis ranking (sorted by total |Δ|)")
    print()
    print(_fmt_scores(scores))
    print()

    if args.show_residuals:
        ranked = sorted(scores, key=lambda x: x.max_abs_im + x.max_abs_mm)
        for s in ranked[:3]:
            print()
            print(_fmt_residuals(s))

    passing = [s for s in scores if s.passes()]
    if not passing:
        print()
        print(
            "**Result: NO hypothesis passes the acceptance thresholds "
            f"(max |ΔIM| ≤ {IM_PASS_THRESHOLD}, max |ΔMM| ≤ {MM_PASS_THRESHOLD}). "
            "Phase 1 cannot recommend a rule from the current dataset.**"
        )
        return 2

    print()
    winner = sorted(passing, key=lambda s: s.max_abs_im + s.max_abs_mm)[0]
    print(f"**Winning rule:** {winner.name} "
          f"(max |ΔIM|={winner.max_abs_im:.4f}, max |ΔMM|={winner.max_abs_mm:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
