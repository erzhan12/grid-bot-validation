"""CLI entry point for backtest-vs-live comparator.

Usage:
    # Compare using backtest trades CSV
    uv run python -m comparator.main \
        --run-id "uuid" \
        --backtest-trades path/to/trades.csv \
        --start "2025-01-01" --end "2025-01-31" \
        --output results/comparison/

    # Compare by running backtest from config
    uv run python -m comparator.main \
        --run-id "uuid" \
        --backtest-config path/to/backtest.yaml \
        --start "2025-01-01" --end "2025-01-31" \
        --symbol BTCUSDT \
        --output results/comparison/
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal

from grid_db import DatabaseFactory, DatabaseSettings

from comparator.config import ComparatorConfig
from comparator.equity import EquityComparator
from comparator.loader import NormalizedTrade, LiveTradeLoader, BacktestTradeLoader
from comparator.matcher import TradeMatcher
from comparator.metrics import calculate_metrics
from comparator.reporter import ComparatorReporter

logger = logging.getLogger(__name__)

EquityPoint = tuple[datetime, Decimal]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Compare backtest results against live trade data"
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Live run ID to compare against",
    )

    # Mutually exclusive backtest source
    bt_group = parser.add_mutually_exclusive_group(required=True)
    bt_group.add_argument(
        "--backtest-trades",
        help="Path to backtest trades CSV export",
    )
    bt_group.add_argument(
        "--backtest-config",
        help="Path to backtest config YAML (runs backtest then compares)",
    )

    parser.add_argument(
        "--start",
        required=True,
        help="Start date (YYYY-MM-DD or ISO format)",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End date (YYYY-MM-DD or ISO format)",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="Symbol filter (e.g., BTCUSDT)",
    )
    parser.add_argument(
        "--database-url",
        default="sqlite:///gridbot.db",
        help="Database connection URL",
    )
    parser.add_argument(
        "--output",
        default="results/comparison",
        help="Output directory for CSV reports",
    )
    parser.add_argument(
        "--backtest-equity",
        default=None,
        help="Path to backtest equity curve CSV (enables equity comparison)",
    )
    parser.add_argument(
        "--coin",
        default="USDT",
        help="Coin for live wallet balance (default: USDT)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def _parse_datetime(value: str, end_of_day: bool = False) -> datetime:
    """Parse a date or datetime string to UTC datetime.

    Args:
        value: Date string (YYYY-MM-DD) or ISO datetime string.
        end_of_day: If True and input is date-only, set time to 23:59:59.999999.
    """
    is_date_only = False
    try:
        dt = datetime.fromisoformat(value)
        if "T" not in value and " " not in value:
            is_date_only = True
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d")
        is_date_only = True

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    if end_of_day and is_date_only:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)

    return dt


def _load_backtest_from_config(
    config_path: str,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
    database_url: str,
) -> tuple[list[NormalizedTrade], list[EquityPoint]]:
    """Run backtest from config and return normalized trades + equity curve.

    Args:
        config_path: Path to backtest config YAML.
        symbol: Trading symbol.
        start_ts: Backtest start time.
        end_ts: Backtest end time.
        database_url: Database URL for market data.

    Returns:
        Tuple of (normalized_trades, equity_curve).
    """
    from backtest.config import load_config
    from backtest.engine import BacktestEngine

    bt_config = load_config(config_path)
    # Override database URL to match comparator's
    bt_config.database_url = database_url

    settings = DatabaseSettings(database_url=database_url)
    db = DatabaseFactory(settings)
    engine = BacktestEngine(config=bt_config, db=db)

    session = engine.run(symbol=symbol, start_ts=start_ts, end_ts=end_ts)

    # Convert trades
    bt_loader = BacktestTradeLoader()
    trades = bt_loader.load_from_session(session.trades)

    # Extract equity curve
    eq = EquityComparator()
    equity = eq.load_backtest_from_session(session.equity_curve)

    logger.info(
        "Backtest produced %d trades and %d equity points",
        len(trades), len(equity),
    )
    return trades, equity


def run(
    config: ComparatorConfig,
    backtest_trades: list[NormalizedTrade],
    backtest_equity: list[EquityPoint] | None = None,
    backtest_equity_path: str | None = None,
    coin: str = "USDT",
) -> int:
    """Run the comparison.

    Args:
        config: Comparator configuration.
        backtest_trades: Normalized backtest trades.
        backtest_equity: Backtest equity curve from session (if available).
        backtest_equity_path: Path to backtest equity CSV (alternative to backtest_equity).
        coin: Coin for live wallet balance lookup.

    Returns:
        Exit code (0=success, 1=config error, 2=execution error).
    """
    # Filter backtest trades by symbol to match live-side filtering
    if config.symbol:
        backtest_trades = [t for t in backtest_trades if t.symbol == config.symbol]

    if not backtest_trades:
        logger.error("No backtest trades to compare")
        return 1

    # Load live trades from database
    settings = DatabaseSettings(database_url=config.database_url)
    db = DatabaseFactory(settings)

    with db.get_session() as session:
        live_loader = LiveTradeLoader(session)
        live_trades = live_loader.load(
            run_id=config.run_id,
            start_ts=config.start_ts,
            end_ts=config.end_ts,
            symbol=config.symbol,
        )

    if not live_trades:
        logger.error("No live trades found for run_id=%s", config.run_id)
        return 1

    # Match trades
    matcher = TradeMatcher()
    match_result = matcher.match(live_trades, backtest_trades)

    # Calculate metrics
    metrics = calculate_metrics(
        match_result,
        price_tolerance=config.price_tolerance,
        qty_tolerance=config.qty_tolerance,
    )

    # Equity curve comparison
    eq = EquityComparator()
    bt_equity = backtest_equity
    if bt_equity is None and backtest_equity_path:
        bt_equity = eq.load_backtest_from_csv(backtest_equity_path)

    resampled_equity = None
    if bt_equity:
        from grid_db import Run
        with db.get_session() as session:
            run_record = session.get(Run, config.run_id)
            if run_record:
                live_equity = eq.load_live(
                    session, run_record.account_id, coin,
                    config.start_ts, config.end_ts,
                )

                resampled_equity = eq.resample(live_equity, bt_equity)
                max_div, mean_div, corr = eq.compute_metrics(resampled_equity)

                metrics.equity_max_divergence = max_div
                metrics.equity_mean_divergence = mean_div
                metrics.equity_correlation = corr
            else:
                logger.warning("Run %s not found, skipping equity comparison", config.run_id)

    # Report (equity data passed to reporter for inclusion in export_all)
    reporter = ComparatorReporter(match_result, metrics, equity_data=resampled_equity)
    reporter.print_summary()
    paths = reporter.export_all(config.output_dir)

    for name, path in paths.items():
        logger.info("Exported %s → %s", name, path)

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    try:
        config = ComparatorConfig(
            run_id=args.run_id,
            database_url=args.database_url,
            start_ts=_parse_datetime(args.start),
            end_ts=_parse_datetime(args.end, end_of_day=True),
            symbol=args.symbol,
            output_dir=args.output,
        )
    except Exception as e:
        logger.error("Configuration error: %s", e)
        return 1

    try:
        if args.backtest_config:
            # Run backtest from config, then compare
            if not args.symbol:
                logger.error("--symbol is required when using --backtest-config")
                return 1
            config.symbol = args.symbol
            bt_trades, bt_equity = _load_backtest_from_config(
                args.backtest_config,
                symbol=args.symbol,
                start_ts=config.start_ts,
                end_ts=config.end_ts,
                database_url=config.database_url,
            )
            return run(
                config,
                bt_trades,
                backtest_equity=bt_equity,
                coin=args.coin,
            )
        else:
            # Load backtest trades from CSV
            bt_loader = BacktestTradeLoader()
            bt_trades = bt_loader.load_from_csv(args.backtest_trades)
            return run(
                config,
                bt_trades,
                backtest_equity_path=args.backtest_equity,
                coin=args.coin,
            )
    except Exception:
        logger.exception("Comparison failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
