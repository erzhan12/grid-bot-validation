"""Main entry point for pnl_checker.

Usage:
    python -m pnl_checker.main --config conf/pnl_checker.yaml
    python -m pnl_checker.main -c conf/pnl_checker.yaml --tolerance 0.001 --debug
"""

import argparse
import logging
import sys

from bybit_adapter.rest_client import BybitRestClient
from gridcore.position import RiskConfig

from pnl_checker.config import load_config
from pnl_checker.fetcher import BybitFetcher
from pnl_checker.calculator import calculate
from pnl_checker.comparator import compare
from pnl_checker.reporter import print_console, save_json


def setup_logging(debug: bool = False) -> None:
    """Set up logging with console output."""
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    console_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    root_logger.addHandler(console_handler)

    # Reduce noise from libraries
    logging.getLogger("pybit").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


def main(config_path: str = None, tolerance: float = None, output_dir: str = "output", debug: bool = False) -> int:
    """Main entry point.

    Args:
        config_path: Path to YAML config file
        tolerance: Override tolerance from config
        output_dir: Directory for JSON output
        debug: Enable debug logging

    Returns:
        Exit code: 0 if all checks pass, 1 if any fail
    """
    setup_logging(debug=debug)

    # Load config
    try:
        config = load_config(config_path)
        logger.info(f"Loaded config with {len(config.symbols)} symbols")
    except FileNotFoundError as e:
        logger.error(f"Config file not found: {e}")
        return 1
    except Exception as e:
        logger.error(f"Config error: {e}")
        return 1

    # Override tolerance if specified
    effective_tolerance = tolerance if tolerance is not None else config.tolerance

    # Create REST client (mainnet only)
    client = BybitRestClient(
        api_key=config.account.api_key,
        api_secret=config.account.api_secret,
        testnet=False,
    )

    # Build risk config from YAML params
    risk_config = RiskConfig(
        min_liq_ratio=config.risk_params.min_liq_ratio,
        max_liq_ratio=config.risk_params.max_liq_ratio,
        max_margin=config.risk_params.max_margin,
        min_total_margin=config.risk_params.min_total_margin,
    )

    # Fetch data
    logger.info("Fetching data from Bybit...")
    fetcher = BybitFetcher(client, funding_max_pages=config.funding_max_pages)
    symbols = [s.symbol for s in config.symbols]

    try:
        fetch_result = fetcher.fetch_all(symbols)
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}")
        return 1

    if not fetch_result.symbols:
        logger.warning("No open positions found for any configured symbol")
        return 0

    # Calculate our values
    logger.info("Running calculations...")
    calc_result = calculate(fetch_result, risk_config)

    # Compare
    logger.info("Comparing values...")
    comparison = compare(fetch_result, calc_result, effective_tolerance, funding_max_pages=config.funding_max_pages)

    # Report
    print_console(comparison)
    save_json(comparison, config, output_dir)

    return 0 if comparison.all_passed else 1


def cli() -> None:
    """Command-line interface entry point."""
    parser = argparse.ArgumentParser(
        description="PnL Checker — validate our PnL calculations against live Bybit data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to YAML config file (default: conf/pnl_checker.yaml)",
    )
    parser.add_argument(
        "--tolerance", "-t",
        type=float,
        default=None,
        help="Override tolerance in USDT (default: from config)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="output",
        help="Output directory for JSON results (default: output/)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    try:
        exit_code = main(
            config_path=args.config,
            tolerance=args.tolerance,
            output_dir=args.output,
            debug=args.debug,
        )
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)


if __name__ == "__main__":
    cli()
