#!/usr/bin/env python3
"""Check for drift between hardcoded MM_TIERS and live Bybit API data.

Compares the hardcoded tier tables in ``gridcore.pnl`` against current
values from the Bybit ``/v5/market/risk-limit`` API.  Exits with code 1
and prints a drift report if any rate differs by more than the configured
threshold (default 5%).

Intended to run as a scheduled CI job (see .github/workflows/risk-tier-monitor.yml).

Usage:
    python scripts/check_tier_drift.py [--threshold 0.05]
"""

import argparse
import sys
from decimal import Decimal

from gridcore.pnl import (
    MM_TIERS,
    MM_TIERS_DEFAULT,
    MMTiers,
    parse_risk_limit_tiers,
)


def _fetch_live_tiers(symbol: str) -> MMTiers:
    """Fetch current tiers from Bybit public API (no auth required)."""
    from bybit_adapter.rest_client import BybitRestClient

    client = BybitRestClient(testnet=False)
    raw = client.get_risk_limit(symbol=symbol)
    if not raw:
        raise RuntimeError(f"Empty response from Bybit API for {symbol}")
    return parse_risk_limit_tiers(raw)


def _rate_drift_pct(hardcoded: Decimal, live: Decimal) -> float:
    """Return the absolute relative drift between two rates.

    Returns 0.0 when both are zero.  For Infinity caps the comparison
    is skipped by the caller (boundary values are expected to differ).
    """
    if hardcoded == live:
        return 0.0
    if hardcoded == Decimal("0"):
        return float("inf")
    return abs(float((live - hardcoded) / hardcoded))


def compare_tiers(
    symbol: str,
    hardcoded: MMTiers,
    live: MMTiers,
    threshold: float,
) -> list[str]:
    """Compare hardcoded vs live tiers and return a list of drift messages."""
    drifts: list[str] = []
    max_len = max(len(hardcoded), len(live))

    if len(hardcoded) != len(live):
        drifts.append(
            f"{symbol}: tier count mismatch — hardcoded={len(hardcoded)}, live={len(live)}"
        )

    for i in range(min(len(hardcoded), len(live))):
        h_max, h_mmr, h_ded, h_imr = hardcoded[i]
        l_max, l_mmr, l_ded, l_imr = live[i]

        # Compare max_value (skip Infinity)
        if h_max != Decimal("Infinity") and l_max != Decimal("Infinity"):
            drift = _rate_drift_pct(h_max, l_max)
            if drift > threshold:
                drifts.append(
                    f"{symbol} tier {i}: max_value drift {drift:.1%} "
                    f"(hardcoded={h_max}, live={l_max})"
                )

        # Compare MMR rate
        drift = _rate_drift_pct(h_mmr, l_mmr)
        if drift > threshold:
            drifts.append(
                f"{symbol} tier {i}: mmr_rate drift {drift:.1%} "
                f"(hardcoded={h_mmr}, live={l_mmr})"
            )

        # Compare deduction
        if h_ded != Decimal("0") or l_ded != Decimal("0"):
            drift = _rate_drift_pct(h_ded, l_ded) if h_ded != Decimal("0") else float("inf")
            if drift > threshold:
                drifts.append(
                    f"{symbol} tier {i}: deduction drift {drift:.1%} "
                    f"(hardcoded={h_ded}, live={l_ded})"
                )

        # Compare IMR rate
        drift = _rate_drift_pct(h_imr, l_imr)
        if drift > threshold:
            drifts.append(
                f"{symbol} tier {i}: imr_rate drift {drift:.1%} "
                f"(hardcoded={h_imr}, live={l_imr})"
            )

    return drifts


def main() -> int:
    parser = argparse.ArgumentParser(description="Check hardcoded tier drift vs Bybit API")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.05,
        help="Maximum allowed relative drift before flagging (default: 0.05 = 5%%)",
    )
    args = parser.parse_args()

    all_drifts: list[str] = []

    for symbol, hardcoded in MM_TIERS.items():
        print(f"Checking {symbol}...")
        try:
            live = _fetch_live_tiers(symbol)
        except Exception as e:
            all_drifts.append(f"{symbol}: failed to fetch live tiers — {e}")
            continue
        drifts = compare_tiers(symbol, hardcoded, live, args.threshold)
        all_drifts.extend(drifts)

    if all_drifts:
        print("\nDrift detected:")
        for d in all_drifts:
            print(f"  - {d}")
        return 1

    print("\nNo drift detected. Hardcoded tiers match live API data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
