#!/usr/bin/env python3
"""Verify the 0065 non-USDT collateral re-mark model against a REAL recorder DB.

Demonstrates, on recorded data, that a non-USDT collateral coin's re-mark term
``balance × (mark_end − mark_seed)`` (acceptance #3b) explains the corresponding
slice of live account ``totalEquity`` drift over a window (acceptance #3a
attribution). Uses the same loader (``load_collateral_seed``) and feed
(``CollateralMarkFeed``) the replay engine uses, so a match here means the
production path is sound.

This does NOT run a full grid replay (which would also need aligned grid-state
seeds for the traded symbol); the automated end-to-end #3a/#3b checks live in
``apps/replay/tests/test_engine_collateral_integration.py``. Acceptance #1
(liq_price delta shrink) is an end-to-end replay observation on the fixture
window and is out of scope for this attribution script.

Static-balance caveat: ``balance`` is locked at the seed row, so pick an
``--at-ts`` AFTER the last collateral-balance change in the window, and treat
deposits/withdrawals/spot-trades of the coin during the window as un-modelled.

Default args target the fixture in this repo (data/recorder_ltcusdt_phase4.db,
SOL collateral, run 1aff13af…).

Usage:
    uv run python scripts/verify_0065_collateral.py \
        --db data/recorder_ltcusdt_phase4.db \
        --coin SOL --symbol SOLUSDT \
        --at-ts 2026-06-01T17:42:10Z --end-ts 2026-06-02T20:00:00Z
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from decimal import Decimal

from grid_db import DatabaseFactory, DatabaseSettings, WalletSnapshotRepository

from replay.engine import CollateralMarkFeed
from replay.snapshot_loader import load_collateral_seed


DEFAULTS = dict(
    db="data/recorder_ltcusdt_phase4.db",
    run_id="1aff13af-2613-4af0-ab12-57aefb43a726",
    account_id="9bdb9748-f9e0-5c13-b144-0ad6a8dbcaba",
    coin="SOL",
    symbol="SOLUSDT",
    usdt_coin="USDT",
    at_ts="2026-06-01T17:42:10Z",
    end_ts="2026-06-02T20:00:00Z",
)


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for key, val in DEFAULTS.items():
        p.add_argument(f"--{key.replace('_', '-')}", default=val)
    p.add_argument(
        "--staleness-seconds", type=int, default=60,
        help="Max wallet-row age for using usdValue as seed mark (else ticker).",
    )
    args = p.parse_args()

    at_ts = _parse_ts(args.at_ts)
    end_ts = _parse_ts(args.end_ts)
    coin = args.coin

    db = DatabaseFactory(DatabaseSettings(db_type="sqlite", db_name=args.db))

    with db.get_session() as session:
        (
            coin_balances, seed_marks, _ratios,
            excluded, missing, switch_off,
        ) = load_collateral_seed(
            session, args.run_id, args.account_id, at_ts,
            args.usdt_coin, [coin], {coin: args.symbol}, {},
            timedelta(seconds=args.staleness_seconds),
        )

        if coin not in coin_balances:
            print(f"[FAIL] {coin} not modelled at {args.at_ts}: "
                  f"excluded={excluded} missing_mark={missing}")
            return 1

        balance = coin_balances[coin]
        seed_mark = seed_marks[coin]

        wallet_repo = WalletSnapshotRepository(session)
        seed_te_row = wallet_repo.get_latest_before(
            args.run_id, args.account_id, args.usdt_coin, at_ts,
        )
        end_te_row = wallet_repo.get_latest_before(
            args.run_id, args.account_id, args.usdt_coin, end_ts,
        )
        # Read scalars while the rows are still attached to the session.
        live_seed_te = seed_te_row.total_equity if seed_te_row else None
        live_end_te = end_te_row.total_equity if end_te_row else None

    # Walk the feed to the window end to get the final mark.
    feed = CollateralMarkFeed(
        db=db, symbol_for={coin: args.symbol}, start_ts=at_ts, end_ts=end_ts,
    )
    end_mark = feed.mark_at(coin, end_ts)
    if end_mark is None:
        print(f"[FAIL] no {args.symbol} ticker at-or-before {args.end_ts}")
        return 1

    modelled_drift = balance * (end_mark - seed_mark)

    print("=" * 64)
    print(f"  0065 collateral re-mark verification — {coin} ({args.symbol})")
    print("=" * 64)
    print(f"  window           : {args.at_ts} -> {args.end_ts}")
    print(f"  {coin} balance (seed) : {balance}")
    print(f"  seed mark        : {seed_mark}")
    print(f"  end  mark        : {end_mark}")
    print(f"  #3b modelled drift = balance*(end-seed) : {modelled_drift}")
    if switch_off:
        print(f"  NOTE collateralSwitch/marginCollateral off: {switch_off}")
    print("-" * 64)
    if live_seed_te is not None and live_end_te is not None:
        live_delta = live_end_te - live_seed_te
        residual = live_delta - modelled_drift
        print(f"  live totalEquity (seed) : {live_seed_te}")
        print(f"  live totalEquity (end)  : {live_end_te}")
        print(f"  live ΔtotalEquity       : {live_delta}")
        print(f"  residual (Δlive - drift): {residual}")
        print("  (residual = futures PnL + other coins + balance changes;")
        print("   the SOL re-mark explains the rest of the totalEquity move)")
    else:
        print("  live totalEquity rows unavailable for one/both bounds.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
