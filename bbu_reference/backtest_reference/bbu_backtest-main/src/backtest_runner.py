#!/usr/bin/env python3
"""
Backtest Runner

Core backtesting functionality using historical data from the ticker_data table.
This module provides the main backtest execution logic with realistic grid bot simulations.
"""

import traceback
from datetime import datetime

from src.backtest_session import BacktestSession
from src.controller import Controller


def run_backtest_with_real_data(symbol, initial_balance=10000, verbose=True, export_results=False, start_datetime=None):  # noqa: C901
    """
    Run backtest with your actual historical data

    Args:
        symbol: Trading symbol (e.g., 'BTCUSDT', 'ETHUSDT')
        initial_balance: Starting balance in USD
        verbose: Print detailed output
        export_results: Export results to CSV files
        start_datetime: Start datetime in YYYY-MM-DD HH:MM:SS format
    """

    print("🚀 Starting Grid Bot Backtest")
    print(f"Symbol: {symbol}")
    print(f"Initial Balance: ${initial_balance:,.2f}")
    print("=" * 50)

    try:
        print("✅ Loading backtest system...")

        # Generate unique session ID
        session_id = f"PROD_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backtest_session = BacktestSession(session_id)
        backtest_session.initial_balance = initial_balance
        backtest_session.current_balance = initial_balance
        backtest_session.start_datetime = start_datetime

        print(f"✅ Created session: {session_id}")

        # Initialize controller for the symbol
        # This will use your existing config.yaml and ticker_data table
        print(f"✅ Initializing controller for {symbol}...")
        controller = Controller(symbol, start_datetime=start_datetime)

        # Initialize backtest mode for all market makers
        print(f"✅ Setting up {len(controller.bms)} market makers for backtesting...")
        for bm in controller.bms:
            bm.init_backtest_mode(backtest_session)
            print(f"   📊 {bm.name} initialized with amount {bm.amount}")

        print(f"✅ Initialized {len(controller.strats)} strategies")
        for strat in controller.strats:
            print(f"   🎯 Strategy {strat.strat_name} (ID: {strat.id})")
            print(f"      Greed: {strat._greed_count} levels, {strat._greed_step}% step")

        print("\n🔄 Starting backtest execution...")
        print("   This will process all historical data for", symbol)
        print("   Press Ctrl+C to stop early if needed")

        # Run the backtest!
        # This will use your existing Strat50._check_pair_step() which iterates through
        # your ticker_data table and processes each price tick
        job_start_time = datetime.now()

        try:
            controller.check_job()
        except KeyboardInterrupt:
            print("\n⚠️  Backtest interrupted by user")
        except Exception as e:
            print(f"\n❌ Backtest error: {e}")
            if verbose:
                traceback.print_exc()  # noqa: F823
            return None

        job_end_time = datetime.now()
        job_duration = job_end_time - job_start_time

        print(f"\n✅ Backtest completed in {job_duration}")

        # Generate and display results
        print("\n📊 BACKTEST RESULTS")
        print("=" * 50)

        final_metrics = backtest_session.get_final_metrics()
        session_summary = backtest_session.get_summary()

        # Display summary
        print("💰 Financial Performance:")
        print(f"   Initial Balance: ${session_summary['current_balance'] - session_summary['total_pnl']:,.2f}")
        print(f"   Final Balance:   ${session_summary['current_balance']:,.2f}")
        print(f"   Total PnL:       ${session_summary['total_pnl']:+,.2f}")
        print(f"   Return:          {session_summary['return_pct']:+.2f}%")

        print("\n📈 Trading Statistics:")
        print(f"   Total Trades:    {session_summary['total_trades']}")
        print(f"   Winning Trades:  {session_summary['winning_trades']}")
        print(f"   Win Rate:        {session_summary['win_rate']:.1f}%")

        # Show detailed metrics if available
        if symbol in final_metrics:
            metrics = final_metrics[symbol]
            print(f"\n🎯 Detailed Metrics for {symbol}:")
            print(f"   Max Drawdown:    {metrics.max_drawdown:.2f}%")
            print(f"   Max Profit:      ${metrics.max_profit:.2f}")
            if metrics.profit_factor > 0:
                print(f"   Profit Factor:   {metrics.profit_factor:.2f}")

        # Show order statistics
        for bm in controller.bms:
            if hasattr(bm, 'backtest_order_manager') and bm.backtest_order_manager:
                stats = bm.backtest_order_manager.get_statistics()
                print(f"\n🎯 Order Statistics ({bm.name}):")
                print(f"   Orders Created:  {stats['total_orders_created']}")
                print(f"   Orders Filled:   {stats['filled_orders']}")
                print(f"   Fill Rate:       {stats['fill_rate'] * 100:.1f}%")
                print(f"   Slippage:        {stats['slippage_bps']} bps")
                break

        # Export results if requested
        if export_results:
            from src.backtest_reporter import BacktestReporter
            reporter = BacktestReporter(backtest_session)

            output_dir = f"./backtest_results/{symbol}_{session_id}"
            reporter.export_to_csv(output_dir)
            print(f"\n📁 Results exported to: {output_dir}")

            # Also generate detailed report
            if verbose:
                print("\n📋 Generating detailed report...")
                reporter.print_detailed_report()

        print("\n🎉 Backtest completed successfully!")
        return backtest_session

    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("   Make sure all dependencies are installed:")
        print("   pip install psycopg2-binary pyyaml pybit")
        return None
    except Exception as e:
        print(f"❌ Backtest failed: {e}")
        if verbose:
            import traceback
            traceback.print_exc()
        return None