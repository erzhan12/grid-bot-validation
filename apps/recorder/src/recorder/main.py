"""Main entry point for data recorder.

Usage:
    python -m recorder.main
    python -m recorder.main --config path/to/recorder.yaml
    python -m recorder.main --debug
"""

import argparse
import asyncio
import logging
import sys
from typing import Optional

from grid_db import DatabaseFactory, DatabaseSettings

from recorder.config import load_config
from recorder.recorder import Recorder


logger = logging.getLogger(__name__)


def setup_logging(debug: bool = False) -> None:
    """Configure logging for recorder.

    Args:
        debug: Enable DEBUG level logging.
    """
    level = logging.DEBUG if debug else logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)

    # Reduce noise from libraries
    logging.getLogger("pybit").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


async def main(config_path: Optional[str] = None) -> int:
    """Main async entry point.

    Args:
        config_path: Path to configuration file.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        return 1
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    logger.info("Recorder configuration:")
    logger.info(f"  Symbols: {config.symbols}")
    logger.info(f"  Testnet: {config.testnet}")
    logger.info(f"  Database: {config.database_url}")
    logger.info(f"  Private streams: {config.account is not None}")
    logger.info(f"  Health log interval: {config.health_log_interval}s")

    if not config.symbols:
        logger.error("No symbols configured. Add symbols to recorder.yaml")
        return 1

    # Initialize database — URL passed directly; DatabaseFactory._create_engine()
    # determines sqlite vs postgresql from the URL string itself.
    settings = DatabaseSettings(database_url=config.database_url)

    db = DatabaseFactory(settings)
    db.create_tables()
    logger.info("Database tables initialized")

    # Create and run recorder
    recorder = Recorder(config=config, db=db)

    try:
        await recorder.start()
        await recorder.run_until_shutdown()
    except Exception as e:
        logger.error(f"Recorder error: {e}")
        await recorder.stop()
        return 2

    return 0


def cli() -> None:
    """Command-line interface entry point."""
    parser = argparse.ArgumentParser(
        description="Data Recorder - Standalone Bybit mainnet data capture",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to configuration file (default: conf/recorder.yaml)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()
    setup_logging(debug=args.debug)

    try:
        exit_code = asyncio.run(main(args.config))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)


if __name__ == "__main__":
    cli()
