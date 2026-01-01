import argparse
import sys

from src.backtest_runner import run_backtest_with_real_data


def run_backtest_cli():
    """Main CLI interface"""
    parser = argparse.ArgumentParser(description='Run grid bot backtest with historical data')
    parser.add_argument('--symbol', help='Trading symbol (e.g., BTCUSDT, ETHUSDT)', default='LTCUSDT')
    parser.add_argument('--balance', type=float, default=500,
                       help='Initial balance in USD (default: 10000)')
    parser.add_argument('--export', action='store_true',
                       help='Export results to CSV files')
    parser.add_argument('--quiet', action='store_true',
                       help='Minimal output')
    parser.add_argument('--start_datetime', type=str, default='2025-09-18 13:05:49',
                       help='Start datetime in YYYY-MM-DD HH:MM:SS format')

    args = parser.parse_args()

    # Run the backtest
    result = run_backtest_with_real_data(
        symbol=args.symbol.upper(),
        initial_balance=args.balance,
        verbose=not args.quiet,
        export_results=args.export,
        start_datetime=args.start_datetime
    )

    if result is None:
        sys.exit(1)

    print(f"\n✨ Backtest session saved: {result.session_id}")


def main():
    run_backtest_cli()


if __name__ == "__main__":
    main()
