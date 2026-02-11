"""CLI entry point for backtest.

Usage:
    uv run python -m backtest.main --config conf/backtest.yaml
    uv run python -m backtest.main --config conf/backtest.yaml --start 2025-01-01 --end 2025-01-31
    uv run python -m backtest.main --config conf/backtest.yaml --export results.csv
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from grid_db import DatabaseFactory, DatabaseSettings

from backtest.config import load_config
from backtest.engine import BacktestEngine


def setup_logging(debug: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if debug else logging.INFO
    format_str = "%(asctime)s %(name)s %(levelname)s: %(message)s"
    logging.basicConfig(level=level, format=format_str)

    # Reduce noise from SQLAlchemy
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run grid trading backtest with trade-through fill model",
    )

    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: conf/backtest.yaml)",
    )

    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Symbol to backtest (overrides config, e.g., BTCUSDT)",
    )

    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )

    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )

    parser.add_argument(
        "--export",
        type=str,
        default=None,
        help="Export results to CSV file",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit on first symbol failure in multi-symbol runs",
    )

    return parser.parse_args()


def parse_datetime(s: str) -> datetime:
    """Parse datetime string in various formats."""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    raise ValueError(f"Unable to parse datetime: {s}")


def export_results(session, filepath: str) -> None:
    """Export backtest results to CSV."""
    import csv

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)

        # Write trades
        writer.writerow([
            "trade_id", "symbol", "side", "price", "qty", "direction",
            "timestamp", "order_id", "realized_pnl", "commission", "strat_id"
        ])

        for trade in session.trades:
            writer.writerow([
                trade.trade_id,
                trade.symbol,
                trade.side,
                str(trade.price),
                str(trade.qty),
                trade.direction,
                trade.timestamp.isoformat(),
                trade.order_id,
                str(trade.realized_pnl),
                str(trade.commission),
                trade.strat_id,
            ])

    print(f"Exported {len(session.trades)} trades to {filepath}")


def main() -> int:
    """Main entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    logger = logging.getLogger(__name__)

    try:
        # Load config
        config = load_config(args.config)
        logger.info(f"Loaded config with {len(config.strategies)} strategies")

        # Create database connection
        settings = DatabaseSettings(database_url=config.database_url)
        db = DatabaseFactory(settings)

        # Create backtest engine
        engine = BacktestEngine(config=config, db=db)

        # Determine symbol(s) to backtest
        if args.symbol:
            symbols = [args.symbol]
        else:
            symbols = list(set(s.symbol for s in config.strategies))

        # Determine date range
        if args.start:
            start_ts = parse_datetime(args.start)
        else:
            # Default: 30 days ago
            start_ts = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = start_ts.replace(day=1)  # First of month

        if args.end:
            end_ts = parse_datetime(args.end)
        else:
            # Default: now
            end_ts = datetime.now()

        logger.info(f"Backtest period: {start_ts} to {end_ts}")
        logger.info(f"Symbols: {symbols}")

        # Run backtest
        failed_symbols: list[str] = []
        for symbol in symbols:
            logger.info(f"\n{'='*50}")
            logger.info(f"Running backtest for {symbol}")
            logger.info(f"{'='*50}")

            try:
                session = engine.run(symbol, start_ts, end_ts)
            except Exception as e:
                logger.exception(f"Backtest failed for {symbol}: {e}")
                if args.strict:
                    return 2
                failed_symbols.append(symbol)
                continue

            # Print summary
            print(session.get_summary())

            # Export if requested
            if args.export:
                export_path = args.export
                if len(symbols) > 1:
                    # Add symbol to filename for multi-symbol runs
                    base = Path(args.export)
                    export_path = str(base.with_stem(f"{base.stem}_{symbol}"))
                export_results(session, export_path)

        if failed_symbols:
            logger.error(f"Failed symbols: {', '.join(failed_symbols)}")
            return 2

        return 0

    except FileNotFoundError as e:
        logger.error(f"Config error: {e}")
        return 1
    except Exception as e:
        logger.exception(f"Startup failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
