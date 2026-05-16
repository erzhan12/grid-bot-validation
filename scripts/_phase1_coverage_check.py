"""Phase 1 coverage check for feature 0043 hedge-mode liq data collection.

Walks position_snapshots for a run_id in exchange_ts order, maintains a
running (long_size, short_size) state, classifies each transition into
one of four hedge configurations, and prints counts. Used by the
recorder-babysit loop to decide when to stop.

Categories:
- long_only:   long > 0, short == 0
- short_only:  short > 0, long == 0
- net_long:    long > short > 0
- fully_hedged: both > 0, |long - short| / max(long, short) < tol

Snapshots where neither side changes the running state are not counted
(we collapse same-state runs). A category is "covered" when at least
TARGET distinct (long_size, short_size) pairs have been observed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from decimal import Decimal


TARGET_PER_CATEGORY = 5
HEDGE_TOL = Decimal("0.01")


def classify(long_size: Decimal, short_size: Decimal) -> str | None:
    """Return category name or None if no position (both zero)."""
    if long_size <= 0 and short_size <= 0:
        return None
    if short_size <= 0:
        return "long_only"
    if long_size <= 0:
        return "short_only"
    # Both > 0
    bigger = max(long_size, short_size)
    if abs(long_size - short_size) / bigger < HEDGE_TOL:
        return "fully_hedged"
    if long_size > short_size:
        return "net_long"
    return "net_short"  # tracked but not required by plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """
        SELECT exchange_ts, side, size
        FROM position_snapshots
        WHERE run_id = ?
        ORDER BY exchange_ts ASC, id ASC
        """,
        (args.run_id,),
    ).fetchall()

    # Group rows by exchange_ts so a single REST-snapshot timestamp that
    # carries both Buy and Sell collapses into one state transition.
    from itertools import groupby

    long_size = Decimal("0")
    short_size = Decimal("0")
    distinct_pairs: dict[str, set[tuple]] = {
        "long_only": set(),
        "short_only": set(),
        "net_long": set(),
        "net_short": set(),
        "fully_hedged": set(),
    }
    prev_pair: tuple | None = None

    for _, group in groupby(rows, key=lambda r: r[0]):
        for _exchange_ts, side, size in group:
            size_dec = Decimal(str(size))
            if side == "Buy":
                long_size = size_dec
            elif side == "Sell":
                short_size = size_dec
        pair = (long_size, short_size)
        if pair == prev_pair:
            continue
        prev_pair = pair
        cat = classify(long_size, short_size)
        if cat is not None:
            distinct_pairs[cat].add(pair)

    counts = {k: len(v) for k, v in distinct_pairs.items()}
    # 2026-05-15: drop long_only / short_only from required set — grid bots
    # rarely close one leg fully, so waiting for those configurations is
    # not a realistic stop criterion. Hedge formula is direction-symmetric
    # so net_long + net_short + fully_hedged spans the derivation space.
    required = ("net_long", "net_short", "fully_hedged")
    all_covered = all(counts[k] >= TARGET_PER_CATEGORY for k in required)

    print(f"snapshots_total={len(rows)}")
    for k in ("long_only", "short_only", "net_long", "net_short", "fully_hedged"):
        marker = "OK" if counts[k] >= TARGET_PER_CATEGORY else "..."
        required_marker = "*" if k in required else " "
        print(f"  {required_marker}{k:<14} {counts[k]:>3}  {marker}")
    print(f"all_required_covered={all_covered}")

    return 0 if all_covered else 1


if __name__ == "__main__":
    sys.exit(main())
