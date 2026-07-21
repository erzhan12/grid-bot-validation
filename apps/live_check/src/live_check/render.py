"""Render modes for live_check output.

Grain note (by design, not an inconsistency): ``--per-fill`` uses RAW
execution grain (one row per live execution, keyed by exec_id) while
``--curve`` uses aggregated trade grain (``match_result.matched``
NormalizedTrade pairs). Per-fill drills into individual executions; curve
compares cumulative trade-level realized point-by-point.
"""

import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Optional

from gridcore.intents import extract_client_order_prefix

_ZERO = Decimal("0")

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _flag(ok: bool) -> str:
    return "✓" if ok else "✗"


def _fmt(d: Decimal) -> str:
    return f"{d:+.4f}"


def render_once(results) -> str:
    """Per-strat summary blocks for a single run.

    Args:
        results: list of ``(strat, Verdict, ReplayResult)`` tuples.
    """
    blocks = []
    for strat, verdict, _result in results:
        status = "PASS" if verdict.passed else "FAIL"
        blocks.append(
            "\n".join(
                [
                    f"=== {strat.strat_id} ({strat.symbol}) — {status} ===",
                    f"  matched      {verdict.matched_count} trades "
                    f"(raw execs: {verdict.live_exec_count}) "
                    f"live_only={verdict.live_only_count} "
                    f"backtest_only={verdict.backtest_only_count} "
                    f"{_flag(verdict.matched_ok)}",
                    f"  Δrealized    {_fmt(verdict.d_realized)} "
                    f"{_flag(verdict.realized_ok)}",
                    f"  Δcommission  {_fmt(verdict.d_commission)} "
                    f"{_flag(verdict.commission_ok)}",
                    f"  Δunrealised  {_fmt(verdict.d_unrealised)} "
                    f"{_flag(verdict.unrealised_ok)}",
                ]
            )
        )
    return "\n\n".join(blocks)


def render_shared_wallet(strat_results, shared_verdict) -> str:
    """Render per-strat rows plus shared account-level wallet gates."""
    blocks = [render_once(strat_results)]
    status = "PASS" if shared_verdict.passed else "FAIL"
    diff = shared_verdict.wallet_diff
    blocks.append(
        "\n".join(
            [
                f"=== shared wallet — {status} ===",
                f"  max Δequity          {_fmt(diff.max_equity_delta)} "
                f"{_flag(shared_verdict.equity_ok)} "
                f"({diff.equity_points} points)",
                f"  final Δequity        {_fmt(diff.final_equity_delta)}",
                f"  max Δmargin_balance  {_fmt(diff.max_margin_balance_delta)} "
                f"{_flag(shared_verdict.total_margin_balance_ok)} "
                f"({diff.margin_balance_points} points)",
                f"  max Δaccount_mm_rate {_fmt(diff.max_account_mm_rate_delta)} "
                f"{_flag(shared_verdict.account_mm_rate_ok)} "
                f"({diff.account_mm_rate_points} points)",
            ]
        )
    )
    return "\n\n".join(blocks)


def render_watch_line(results, tick_ts=None) -> str:
    """One-line traffic-light per strat for a watch tick.

    Args:
        results: list of ``(strat, Verdict, ReplayResult)`` tuples.
        tick_ts: Optional tick timestamp prefix.
    """
    prefix = f"{tick_ts:%H:%M:%S} " if tick_ts is not None else ""
    lines = []
    for strat, verdict, _result in results:
        lines.append(
            f"{prefix}{strat.strat_id} {_flag(verdict.passed)} "
            f"matched={verdict.matched_count} "
            f"live_only={verdict.live_only_count} "
            f"bt_only={verdict.backtest_only_count} "
            f"Δr={_fmt(verdict.d_realized)} "
            f"Δc={_fmt(verdict.d_commission)} "
            f"Δu={_fmt(verdict.d_unrealised)}"
        )
    return "\n".join(lines)


def render_per_fill(results) -> str:
    """Detailed per-execution table: one row per RAW live execution.

    Sources the live side from raw ``private_executions`` rows (which carry
    exec_id — ``match_result.matched`` NormalizedTrades aggregate partial
    fills and carry no exec_id, so they cannot build this table).

    Pairing: in event_follower mode the engine flushes ONE ``BacktestTrade``
    per order lifecycle (``_FillRollup``: VWAP price, summed qty/pnl), keyed
    ``(client_order_id=link prefix, order_id)``. Live execs are therefore
    grouped by the SAME key — ``extract_client_order_prefix`` strips the
    live ``-{millis}`` orderLinkId suffix (raw link would never match), with
    ``order_id`` fallback for NULL links — and each group's AGGREGATE
    (Σqty, VWAP px, Σpnl) is compared against its bt trade. Rows still
    render at raw-exec grain; the bt columns and ok flag are per-group.

    Args:
        results: list of ``(strat, Verdict, ReplayResult, exec_rows)``
            tuples, where ``exec_rows`` are materialized
            ``ground_truth.ExecRow`` items for the strat's window.
    """
    header = (
        f"{'exec_id':<20} {'time':<19} {'side':<4} "
        f"{'live_px':>10} {'bt_px':>10} {'dpx':>8} "
        f"{'live_qty':>9} {'bt_qty':>9} {'live_pnl':>10} {'bt_pnl':>10} ok"
    )
    blocks = []
    for strat, _verdict, result, exec_rows in results:
        bt_by_key = {
            (trade.client_order_id, trade.order_id): trade
            for trade in result.session.trades
        }

        def _key(ex):
            prefix = extract_client_order_prefix(ex.order_link_id)
            return (prefix or ex.order_id, ex.order_id)

        groups: dict[tuple, list] = defaultdict(list)
        for ex in exec_rows:
            groups[_key(ex)].append(ex)

        group_ok: dict[tuple, bool] = {}
        for key, execs in groups.items():
            bt = bt_by_key.get(key)
            if bt is None:
                group_ok[key] = False
                continue
            qty_sum = sum((e.exec_qty or _ZERO) for e in execs)
            pnl_sum = sum((e.closed_pnl or _ZERO) for e in execs)
            notional = sum(
                (e.exec_price or _ZERO) * (e.exec_qty or _ZERO) for e in execs
            )
            vwap = notional / qty_sum if qty_sum else _ZERO
            group_ok[key] = (
                qty_sum == bt.qty
                and vwap == bt.price
                and pnl_sum == bt.realized_pnl
            )

        lines = [f"=== {strat.strat_id} ({strat.symbol}) per-fill ===", header]
        for ex in exec_rows:
            key = _key(ex)
            bt = bt_by_key.get(key)
            live_px = ex.exec_price if ex.exec_price is not None else _ZERO
            live_qty = ex.exec_qty if ex.exec_qty is not None else _ZERO
            live_pnl = ex.closed_pnl if ex.closed_pnl is not None else _ZERO
            if bt is not None:
                dpx = live_px - bt.price
                lines.append(
                    f"{ex.exec_id:<20} {ex.exchange_ts:%Y-%m-%d %H:%M:%S} "
                    f"{ex.side:<4} {live_px:>10.4f} {bt.price:>10.4f} "
                    f"{dpx:>8.4f} {live_qty:>9.4f} {bt.qty:>9.4f} "
                    f"{live_pnl:>10.4f} {bt.realized_pnl:>10.4f} "
                    f"{_flag(group_ok[key])}"
                )
            else:
                lines.append(
                    f"{ex.exec_id:<20} {ex.exchange_ts:%Y-%m-%d %H:%M:%S} "
                    f"{ex.side:<4} {live_px:>10.4f} {'—':>10} {'—':>8} "
                    f"{live_qty:>9.4f} {'—':>9} {live_pnl:>10.4f} {'—':>10} ✗"
                )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _sparkline(values: list[Decimal]) -> str:
    """ASCII sparkline over a numeric series (empty-safe)."""
    if not values:
        return "(no data)"
    lo, hi = min(values), max(values)
    span = hi - lo
    if span == 0:
        return _SPARK_CHARS[0] * len(values)
    out = []
    for v in values:
        idx = int((v - lo) / span * (len(_SPARK_CHARS) - 1))
        out.append(_SPARK_CHARS[idx])
    return "".join(out)


def render_curve(results, csv_dir: Optional[str] = None) -> str:
    """Cumulative realized curves: live vs replay, matched-pair grain.

    Both series walk the SAME ``match_result.matched`` pair sequence ordered
    by ``NormalizedTrade.timestamp`` — live is the cumsum of each pair's
    live-side realized, replay the cumsum of the bt side — so they are
    point-by-point comparable. (Raw-exec cumsum would be a different grain:
    partial fills change the point count.) Not built via ``EquityComparator``
    — its loaders read wallet snapshots / full equity curves, neither of
    which is this matched-pair cumulative-realized pair.

    Args:
        results: list of ``(strat, Verdict, ReplayResult)`` tuples.
        csv_dir: When set, also writes ``curve_<strat_id>.csv`` per strat.

    Returns:
        Sparkline text block per strat.
    """
    blocks = []
    for strat, _verdict, result in results:
        pairs = sorted(
            result.match_result.matched, key=lambda p: p.live.timestamp
        )
        live_cum: list[Decimal] = []
        bt_cum: list[Decimal] = []
        live_total = _ZERO
        bt_total = _ZERO
        for pair in pairs:
            live_total += pair.live.realized_pnl
            bt_total += pair.backtest.realized_pnl
            live_cum.append(live_total)
            bt_cum.append(bt_total)

        blocks.append(
            "\n".join(
                [
                    f"=== {strat.strat_id} ({strat.symbol}) cumulative "
                    f"realized ({len(pairs)} matched pairs) ===",
                    f"  live   {_sparkline(live_cum)} "
                    f"(final {live_total:+.4f})",
                    f"  replay {_sparkline(bt_cum)} "
                    f"(final {bt_total:+.4f})",
                ]
            )
        )

        if csv_dir is not None:
            path = Path(csv_dir) / f"curve_{strat.strat_id}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    ["timestamp", "client_order_id", "live_cum", "replay_cum"]
                )
                for pair, lv, bv in zip(pairs, live_cum, bt_cum):
                    writer.writerow(
                        [
                            pair.live.timestamp.isoformat(),
                            pair.live.client_order_id,
                            str(lv),
                            str(bv),
                        ]
                    )
            blocks.append(f"  csv: {path}")
    return "\n".join(blocks)
