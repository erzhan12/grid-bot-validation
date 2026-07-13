"""Main entry point for live_check (feature 0088).

Usage:
    uv run live-check                  # --once over the default 4h window
    uv run live-check --watch 10m      # rolling one-line ticks
    uv run live-check --per-fill --last 2h
    uv run live-check --curve

Exit codes (pinned so cron/automation never mistakes a zero-data window for
success):
    0 — all strats PASS.
    1 — any strat FAIL (verdict diverges) OR a config error.
    2 — SKIP / no-data / N/A (empty window, seed miss, stale data) or an
        unexpected exception.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from grid_db import DatabaseFactory, DatabaseSettings, Run, RunRepository, redact_db_url
from replay.snapshot_loader import SeedDataQualityError

from live_check import ground_truth, render, runner
from live_check.config import LiveCheckConfig, StratCheckConfig, load_config
from live_check.verdict import evaluate
from live_check.window import (
    Window,
    check_post_0080_floors,
    compute_window,
    freshness_skip_reason,
    parse_duration,
    staleness_threshold,
    to_naive_utc,
)

logger = logging.getLogger(__name__)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 2

_CURVE_CSV_DIR = "results/live_check"


def setup_logging(debug: bool = False) -> None:
    """Configure logging for live_check.

    Args:
        debug: Enable DEBUG level logging.
    """
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)


def check_strat(
    strat: StratCheckConfig,
    window: Window,
    run_id: str,
    account_id: str,
    db: DatabaseFactory,
    config: LiveCheckConfig,
) -> tuple:
    """Run one strat's replay + ground truth + verdict for a window.

    Returns:
        ``("skip", reason)`` for empty-window / no-ticker / seed-miss, else
        ``("pass" | "fail", Verdict, ReplayResult)``.
    """
    with db.get_readonly_session() as session:
        exec_count = ground_truth.live_exec_count(
            session, run_id, strat.symbol, window.start, window.end
        )
        has_ticker = (
            ground_truth.latest_ticker_ts(session, strat.symbol) is not None
        )
    # Empty-window guard: a zero-data window yields matched==0 and zero
    # deltas — an all-PASS lie. Report SKIP instead, never PASS.
    if exec_count == 0:
        return ("skip", "no data in window (0 live executions)")
    if not has_ticker:
        return ("skip", "no ticker data")

    try:
        result = runner.run_strat(strat, window, run_id, account_id, db)
    except SeedDataQualityError as e:
        return ("skip", f"seed miss at {window.start.isoformat()}: {e}")

    with db.get_readonly_session() as session:
        truth = ground_truth.collect(
            session, run_id, account_id, strat.symbol, window
        )
    v = evaluate(result, truth, config.thresholds)
    return ("pass" if v.passed else "fail", v, result)


def _resolve_run(db: DatabaseFactory, run_id: Optional[str]):
    """Resolve (run_id, account_id, run_start) from config/CLI or discovery.

    Pre-queries the ``Run`` row: ``SeedConfig`` requires ``account_id`` at
    CONSTRUCTION time, so it must be known before ``build_replay_config``.
    """
    with db.get_readonly_session() as session:
        if run_id is None:
            run = RunRepository(session).get_latest_by_type("recording")
            if run is None:
                raise ValueError(
                    f"No recording runs found in database "
                    f"({redact_db_url(db.settings.get_database_url())})"
                )
        else:
            run = session.get(Run, run_id)
            if run is None:
                raise ValueError(f"Run '{run_id}' not found in database")
        return run.run_id, run.account_id, run.start_ts


def _exit_code(outcomes: list[str]) -> int:
    """FAIL(1) beats SKIP(2) beats PASS(0)."""
    if any(o == "fail" for o in outcomes):
        return EXIT_FAIL
    if any(o == "skip" for o in outcomes):
        return EXIT_SKIP
    return EXIT_PASS


def run_single(config: LiveCheckConfig, args, db: DatabaseFactory) -> int:
    """--once / --per-fill / --curve: one window, one report, exit."""
    run_id, account_id, run_start = _resolve_run(db, config.run_id)
    window = compute_window(parse_duration(args.last), parse_duration(args.lag))
    check_post_0080_floors(window.start, run_start)

    outcomes: list[str] = []
    results = []
    for strat in config.strats:
        outcome = check_strat(strat, window, run_id, account_id, db, config)
        outcomes.append(outcome[0])
        if outcome[0] == "skip":
            print(f"{strat.strat_id} ({strat.symbol}) — SKIP: {outcome[1]}")
        else:
            results.append((strat, outcome[1], outcome[2]))

    if results:
        if args.per_fill:
            with db.get_readonly_session() as session:
                enriched = [
                    (
                        strat,
                        v,
                        result,
                        ground_truth.get_window_executions(
                            session, run_id, strat.symbol,
                            window.start, window.end,
                        ),
                    )
                    for strat, v, result in results
                ]
            print(render.render_per_fill(enriched))
        elif args.curve:
            print(render.render_curve(results, csv_dir=_CURVE_CSV_DIR))
        else:
            print(render.render_once(results))

    return _exit_code(outcomes)


def watch_tick(
    config: LiveCheckConfig,
    db: DatabaseFactory,
    run_id: str,
    account_id: str,
    last,
    lag,
    threshold,
    now: Optional[datetime] = None,
) -> list[str]:
    """One --watch tick: freshness gate + per-strat check, one line each.

    ``last``/``lag`` are the RESOLVED CLI/config timedeltas — passed
    explicitly so a ``--last`` override reaches the actual reconcile window
    (reading ``config.last`` here would silently ignore the CLI flag).

    Per-tick SKIP and FAIL become lines, never exceptions — the watch loop
    must survive them.
    """
    window = compute_window(last, lag, now=now)
    lines: list[str] = []
    for strat in config.strats:
        with db.get_readonly_session() as session:
            ticker_ts = ground_truth.latest_ticker_ts(session, strat.symbol)
        reason = freshness_skip_reason(ticker_ts, lag, threshold, now=now)
        if reason is not None:
            lines.append(f"{strat.strat_id} SKIP: {reason}")
            continue
        outcome = check_strat(strat, window, run_id, account_id, db, config)
        if outcome[0] == "skip":
            lines.append(f"{strat.strat_id} SKIP: {outcome[1]}")
        else:
            lines.append(
                render.render_watch_line([(strat, outcome[1], outcome[2])])
            )
    return lines


def run_watch(config: LiveCheckConfig, args, db: DatabaseFactory) -> int:
    """--watch loop: recompute the rolling window every interval.

    Exits ONLY on a fatal (guard violation, unexpected exception) — never on
    a per-tick SKIP or FAIL.
    """
    run_id, account_id, run_start = _resolve_run(db, config.run_id)
    interval = parse_duration(args.watch)
    last = parse_duration(args.last)
    lag = parse_duration(args.lag)
    override = (
        parse_duration(config.staleness_threshold)
        if config.staleness_threshold is not None
        else None
    )
    threshold = staleness_threshold(lag, override)

    while True:
        window = compute_window(last, lag)
        check_post_0080_floors(window.start, run_start)
        tick_ts = to_naive_utc(datetime.now(timezone.utc))
        for line in watch_tick(
            config, db, run_id, account_id, last, lag, threshold
        ):
            print(f"{tick_ts:%H:%M:%S} {line}")
        time.sleep(interval.total_seconds())


def main(args) -> int:
    """Dispatch a live_check invocation; returns the process exit code."""
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Configuration error: {e}")
        return EXIT_FAIL

    if args.database_url:
        config = config.model_copy(update={"database_url": args.database_url})
    if args.run_id:
        config = config.model_copy(update={"run_id": args.run_id})
    if args.last is None:
        args.last = config.last
    if args.lag is None:
        args.lag = config.lag

    # READ-ONLY open (Phase 1B(a)): mode=ro only, never immutable=1. All
    # ground-truth reads use get_readonly_session(); the replay engine gets
    # the same factory with snapshot emission disabled (Phase 1B(b)).
    db = DatabaseFactory(
        DatabaseSettings(database_url=config.database_url, read_only=True)
    )
    logger.info(
        "live_check: db=%s (read-only)", redact_db_url(config.database_url)
    )

    try:
        if args.watch:
            return run_watch(config, args, db)
        return run_single(config, args, db)
    except ValueError as e:
        # Guard violations (pre-0080 floors, unknown run_id) and config errors.
        logger.error(str(e))
        return EXIT_FAIL
    except SeedDataQualityError as e:
        # Non-watch modes surface a seed miss as a clear no-data exit.
        logger.error(f"Seed miss: {e}")
        return EXIT_SKIP
    except Exception:
        logger.error("Unexpected error", exc_info=True)
        return EXIT_SKIP


def cli() -> None:
    """Command-line interface entry point."""
    parser = argparse.ArgumentParser(
        description="live_check - replay-vs-live reconciliation checker",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true",
        help="Run one check over the window and exit (default)",
    )
    mode.add_argument(
        "--watch", type=str, default=None, metavar="INTERVAL",
        help="Loop every INTERVAL (e.g. 10m), one line per tick per strat",
    )
    mode.add_argument(
        "--per-fill", action="store_true",
        help="Detailed table, one row per raw live execution",
    )
    mode.add_argument(
        "--curve", action="store_true",
        help="Cumulative realized sparklines + CSV export",
    )
    parser.add_argument(
        "--last", type=str, default=None,
        help="Window length (default from config, 4h)",
    )
    parser.add_argument(
        "--lag", type=str, default=None,
        help="Window end lag behind now (default from config, 2m)",
    )
    parser.add_argument(
        "--config", "-c", type=str, default=None,
        help="Path to configuration file (default: conf/live_check.yaml)",
    )
    parser.add_argument(
        "--database-url", type=str, default=None,
        help="Override recorder database URL",
    )
    parser.add_argument(
        "--run-id", type=str, default=None,
        help="Override recorder run_id",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging",
    )
    args = parser.parse_args()
    setup_logging(args.debug)
    sys.exit(main(args))


if __name__ == "__main__":
    cli()
