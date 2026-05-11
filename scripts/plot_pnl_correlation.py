"""Plot cumulative live vs backtest PnL for the two Phase-4 smokes.

Two-panel figure:
  - left:  cumulative PnL curves over wall-clock time (live vs backtest),
           one panel per fill mode.
  - right: scatter of per-trade live_pnl vs backtest_pnl with the y=x
           reference line, annotated with Pearson correlation.

Reads `matched_trades.csv` produced by `apps/replay/src/replay/main.py`.
"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime


RESULTS = Path("results")
RUNS = [
    ("book_touch (0033)", RESULTS / "replay_ltcusdt_phase4.book_touch"),
    ("strict_cross (default)", RESULTS / "replay_ltcusdt_phase4.strict_cross_default"),
]


def load(run_dir: Path):
    ts, live, bt = [], [], []
    with (run_dir / "matched_trades.csv").open() as f:
        for row in csv.DictReader(f):
            ts.append(datetime.fromisoformat(row["live_timestamp"]))
            live.append(float(row["live_pnl"]))
            bt.append(float(row["backtest_pnl"]))
    order = np.argsort(ts)
    ts = np.array(ts)[order]
    live = np.cumsum(np.array(live)[order])
    bt = np.cumsum(np.array(bt)[order])
    return ts, live, bt


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 2:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


fig, axes = plt.subplots(len(RUNS), 2, figsize=(13, 8), constrained_layout=True)
fig.suptitle(
    "Phase 4 parity — live vs backtest PnL (recorder run b1f5b867)",
    fontsize=13, fontweight="bold",
)

for row, (label, run_dir) in enumerate(RUNS):
    ts, live_cum, bt_cum = load(run_dir)
    per_live = np.diff(live_cum, prepend=0.0)
    per_bt = np.diff(bt_cum, prepend=0.0)
    corr = pearson(per_live, per_bt)
    n = len(ts)

    ax_curve = axes[row, 0]
    ax_curve.plot(ts, live_cum, label="live", color="#1f77b4", linewidth=1.6)
    ax_curve.plot(ts, bt_cum, label="backtest", color="#ff7f0e",
                  linewidth=1.6, linestyle="--")
    ax_curve.fill_between(ts, live_cum, bt_cum, alpha=0.12, color="gray")
    ax_curve.set_title(f"{label} — cumulative PnL  (n={n}, corr={corr:.4f})")
    ax_curve.set_ylabel("Cumulative PnL (USDT)")
    ax_curve.axhline(0, color="black", linewidth=0.6, alpha=0.5)
    ax_curve.legend(loc="best", fontsize=9)
    ax_curve.grid(True, alpha=0.3)
    ax_curve.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_curve.xaxis.set_major_locator(mdates.HourLocator(interval=2))

    ax_sc = axes[row, 1]
    lim = max(abs(per_live).max(), abs(per_bt).max()) * 1.1 if n else 1.0
    ax_sc.plot([-lim, lim], [-lim, lim], color="black",
               linewidth=0.6, alpha=0.5, label="y = x")
    ax_sc.scatter(per_live, per_bt, s=14, alpha=0.55, color="#2ca02c",
                  edgecolors="none")
    ax_sc.set_xlim(-lim, lim)
    ax_sc.set_ylim(-lim, lim)
    ax_sc.set_xlabel("live PnL per trade")
    ax_sc.set_ylabel("backtest PnL per trade")
    ax_sc.set_title(f"{label} — per-trade scatter  (Pearson r={corr:.4f})")
    ax_sc.grid(True, alpha=0.3)
    ax_sc.legend(loc="best", fontsize=9)
    ax_sc.set_aspect("equal", adjustable="box")

out = RESULTS / "phase4_pnl_correlation.png"
fig.savefig(out, dpi=140)
print(f"wrote {out}")
