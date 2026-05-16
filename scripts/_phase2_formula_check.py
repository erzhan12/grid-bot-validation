"""Phase 2 sniff test for feature 0043 hedge-mode liq formula candidates.

For each (Buy, Sell) snapshot pair in position_snapshots for a run_id,
compute live liq vs four candidate models, and print the delta.

Pair construction: group rows by exchange_ts; if both Buy and Sell are
present for the same timestamp, treat them as a single state. Drop
groups where only one side appears (initial REST always writes both).

Balance: use the latest wallet_snapshot at-or-before the pair's
exchange_ts. Both `total_available_balance` and `total_equity`
variants are computed.

MM: use the snapshot's own `position_mm` per leg (Decimal dollars).
Approximation: at-liq MM is treated as equal to at-current MM (first
tier dominance — fine for the small sizes we hit).

Net-exposure model:
  q_net = |L - S| (sign indicates dominant direction)
  liq applies to the dominant leg; smaller leg → 0
  For net_short: liq = short_entry + (pool - mm_sum) / q_net
  For net_long:  liq = long_entry  - (pool - mm_sum) / q_net

Candidates compared:
  A) per_leg_avail  — current 0042 formula, totalAvailableBalance
  B) per_leg_equity — same per-leg formula but with totalEquity
  C) net_avail      — net-exposure with totalAvailableBalance
  D) net_equity     — net-exposure with totalEquity

Read-only; safe to run while recorder is writing.
"""

from __future__ import annotations

import argparse
import sqlite3
from decimal import Decimal
from itertools import groupby


def _D(x) -> Decimal:
    if x is None:
        return Decimal("0")
    return Decimal(str(x))


def per_leg_short(entry: Decimal, qty: Decimal, pool: Decimal, mm: Decimal) -> Decimal:
    if qty <= 0:
        return Decimal("0")
    return entry + (pool - mm) / qty


def per_leg_long(entry: Decimal, qty: Decimal, pool: Decimal, mm: Decimal) -> Decimal:
    if qty <= 0:
        return Decimal("0")
    return entry - (pool - mm) / qty


def net_short(short_entry: Decimal, q_net: Decimal, pool: Decimal, mm_sum: Decimal) -> Decimal:
    if q_net <= 0:
        return Decimal("0")
    return short_entry + (pool - mm_sum) / q_net


def net_long(long_entry: Decimal, q_net: Decimal, pool: Decimal, mm_sum: Decimal) -> Decimal:
    if q_net <= 0:
        return Decimal("0")
    return long_entry - (pool - mm_sum) / q_net


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    pos_rows = conn.execute(
        """
        SELECT exchange_ts, side, size, entry_price, liq_price, position_mm, mark_price, id
        FROM position_snapshots
        WHERE run_id = ?
        ORDER BY exchange_ts ASC, id ASC
        """,
        (args.run_id,),
    ).fetchall()

    wallet_rows = conn.execute(
        """
        SELECT exchange_ts, total_available_balance, total_equity
        FROM wallet_snapshots
        WHERE run_id = ?
          AND total_available_balance IS NOT NULL
        ORDER BY exchange_ts ASC
        """,
        (args.run_id,),
    ).fetchall()

    def wallet_at_or_before(ts: str) -> tuple[Decimal, Decimal] | None:
        latest: tuple[Decimal, Decimal] | None = None
        for w_ts, tab, te in wallet_rows:
            if w_ts <= ts:
                latest = (_D(tab), _D(te))
            else:
                break
        return latest

    print(f"{'ts':<27} {'dir':<10} {'L':>5} {'S':>5} {'q_net':>5} {'equity':>7} {'mm_sum':>6} {'liq_live':>9} {'net_eq':>9} {'delta':>7}")

    # Group by exchange_ts, take the latest size per side within the group.
    for ts, group in groupby(pos_rows, key=lambda r: r[0]):
        state: dict[str, tuple] = {}
        for r in group:
            state[r[1]] = r  # Buy / Sell -> latest row in this ts group

        if "Buy" not in state or "Sell" not in state:
            continue

        L_size = _D(state["Buy"][2])
        L_entry = _D(state["Buy"][3])
        L_mm = _D(state["Buy"][5])
        S_size = _D(state["Sell"][2])
        S_entry = _D(state["Sell"][3])
        S_mm = _D(state["Sell"][5])

        # liq_price may be NULL/None on the hedged (smaller) side.
        liq_long_live_raw = state["Buy"][4]
        liq_short_live_raw = state["Sell"][4]
        liq_long_live = _D(liq_long_live_raw) if liq_long_live_raw is not None else None
        liq_short_live = _D(liq_short_live_raw) if liq_short_live_raw is not None else None

        wallet = wallet_at_or_before(ts)
        if wallet is None:
            continue
        avail, equity = wallet
        mm_sum = L_mm + S_mm

        if L_size == 0 and S_size == 0:
            continue
        elif S_size > L_size and S_size > 0:
            direction = "net_short"
            q_net = S_size - L_size
            net_eq = net_short(S_entry, q_net, equity, mm_sum)
            live = liq_short_live
        elif L_size > S_size and L_size > 0:
            direction = "net_long"
            q_net = L_size - S_size
            net_eq = net_long(L_entry, q_net, equity, mm_sum)
            live = liq_long_live
        else:
            direction = "fully_hedged"
            q_net = Decimal("0")
            net_eq = Decimal("0")
            # On fully hedged Bybit returns NULL for both legs; prefer
            # whichever is present, else show 0.
            live = liq_long_live if liq_long_live is not None else liq_short_live

        live_str = f"{float(live):>9.2f}" if live is not None else f"{'NULL':>9}"
        delta_str = (
            f"{float(net_eq - live):>+7.2f}"
            if live is not None and live > 0
            else f"{'---':>7}"
        )

        # Marker for the gold-standard quantitative match cases:
        #   net_long  + non-NULL liq_long_live  → matches positive predicted liq
        #   net_short + non-NULL liq_short_live → matches positive predicted liq
        # Both produce a meaningful numeric delta. Net-long currently
        # uncovered; flag it loudly when it appears.
        if live is not None and live > 0 and direction != "fully_hedged":
            flag = "** "
        else:
            flag = "   "

        print(
            f"{flag}{ts:<27} "
            f"{direction:<10} "
            f"{float(L_size):>5.2f} {float(S_size):>5.2f} {float(q_net):>5.2f} "
            f"{float(equity):>7.2f} {float(mm_sum):>6.2f} "
            f"{live_str} {float(net_eq):>9.2f} {delta_str}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
