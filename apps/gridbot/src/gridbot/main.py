"""Main entry point for gridbot.

Usage:
    python -m gridbot.main
    python -m gridbot.main --config path/to/config.yaml
"""

import argparse
import asyncio
import logging
import signal
import sys
from typing import Optional

from grid_db import DatabaseFactory, DatabaseSettings

from gridbot.config import load_config, GridbotConfig
from gridbot.notifier import Notifier
from gridbot.orchestrator import Orchestrator


# Configure logging
def setup_logging(json_file: Optional[str] = None) -> None:
    """Set up logging with both console and optional JSON file output.

    Args:
        json_file: Path to JSON log file (optional).
    """
    # Console handler (human-readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    # JSON file handler (if specified)
    if json_file:
        try:
            import json

            class JsonFormatter(logging.Formatter):
                def format(self, record):
                    log_dict = {
                        "timestamp": self.formatTime(record),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": record.getMessage(),
                    }
                    if record.exc_info:
                        log_dict["exception"] = self.formatException(record.exc_info)
                    return json.dumps(log_dict)

            file_handler = logging.FileHandler(json_file)
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(JsonFormatter())
            root_logger.addHandler(file_handler)
        except Exception as e:
            logging.warning(f"Failed to set up JSON logging: {e}")

    # Reduce noise from libraries
    logging.getLogger("pybit").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


async def main(config_path: Optional[str] = None) -> int:
    """Main async entry point.

    Args:
        config_path: Path to configuration file.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    # Load configuration
    try:
        config = load_config(config_path)
        logger.info(f"Loaded configuration with {len(config.strategies)} strategies")
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        return 1
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    # Initialize database (optional)
    db = None
    if config.database_url:
        try:
            settings = DatabaseSettings()
            # Parse database URL to settings
            if config.database_url.startswith("sqlite"):
                settings.db_type = "sqlite"
                # Extract path from URL
                if ":///" in config.database_url:
                    settings.db_name = config.database_url.split(":///")[-1]
                elif "memory" in config.database_url:
                    settings.db_name = ":memory:"
            db = DatabaseFactory(settings)
            logger.info(f"Database initialized: {config.database_url}")
        except Exception as e:
            logger.warning(f"Failed to initialize database: {e}")

    # Create notifier
    telegram_config = None
    if config.notification and config.notification.telegram:
        telegram_config = config.notification.telegram
    notifier = Notifier(telegram_config)

    # Create orchestrator
    orchestrator = Orchestrator(config, db, notifier=notifier)

    # Set up signal handlers
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, initiating shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start orchestrator
    try:
        await orchestrator.start()
        logger.info("Gridbot started successfully")

        # Wait for shutdown signal
        await shutdown_event.wait()

    except Exception as e:
        logger.error(f"Error during startup: {e}")
        return 1

    finally:
        # Stop orchestrator
        logger.info("Shutting down gridbot")
        await orchestrator.stop()

    logger.info("Gridbot stopped")
    return 0


def cli() -> None:
    """Command-line interface entry point."""
    parser = argparse.ArgumentParser(
        description="Gridbot - Multi-tenant grid trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to configuration file (default: conf/gridbot.yaml)",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Path to JSON log file (optional)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Set up logging
    setup_logging(json_file=args.log_file)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("gridbot").setLevel(logging.DEBUG)
        logging.getLogger("gridcore").setLevel(logging.DEBUG)

    # Run main
    try:
        exit_code = asyncio.run(main(args.config))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)


if __name__ == "__main__":
    cli()
