"""CLI entry point for replay engine.

Usage:
    uv run python -m replay.main --config conf/replay.yaml
    uv run python -m replay.main --config conf/replay.yaml --run-id UUID
    uv run python -m replay.main --config conf/replay.yaml --start 2025-02-20 --end 2025-02-23
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from grid_db import DatabaseFactory, DatabaseSettings

from comparator import ComparatorReporter

from replay.config import load_config


def setup_logging(debug: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if debug else logging.INFO
    format_str = "%(asctime)s %(name)s %(levelname)s: %(message)s"
    logging.basicConfig(level=level, format=format_str)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def parse_args(argv=None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Replay recorded mainnet data through GridEngine for shadow validation",
    )

    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to replay config YAML (default: conf/replay.yaml)",
    )

    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Override database URL from config",
    )

    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Recorder run_id (default: auto-discover latest)",
    )

    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Override symbol from config",
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
        "--output",
        type=str,
        default=None,
        help="Output directory for reports (default: results/replay)",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    return parser.parse_args(argv)


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


def main(argv=None) -> int:
    """Main entry point."""
    args = parse_args(argv)
    setup_logging(debug=args.debug)

    logger = logging.getLogger(__name__)

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(f"Config error: {e}")
        return 1

    # Apply CLI overrides
    if args.database_url:
        config.database_url = args.database_url
    if args.run_id:
        config.run_id = args.run_id
    if args.symbol:
        config.symbol = args.symbol
    if args.start:
        config.start_ts = parse_datetime(args.start)
    if args.end:
        config.end_ts = parse_datetime(args.end)
    if args.output:
        config.output_dir = args.output

    logger.info(f"Replay config: symbol={config.symbol}, db={config.database_url}")

    try:
        # Create database connection
        settings = DatabaseSettings(database_url=config.database_url)
        db = DatabaseFactory(settings)

        # Run replay
        from replay.engine import ReplayEngine

        engine = ReplayEngine(config=config, db=db)
        result = engine.run()

        # Print session summary
        print(result.session.get_summary())

        # Export comparison reports
        output_dir = Path(config.output_dir)
        reporter = ComparatorReporter(
            match_result=result.match_result,
            metrics=result.metrics,
        )
        exported = reporter.export_all(output_dir)
        reporter.print_summary()

        for report_type, path in exported.items():
            logger.info(f"Exported {report_type}: {path}")

        return 0

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except Exception as e:
        logger.exception(f"Replay failed: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
