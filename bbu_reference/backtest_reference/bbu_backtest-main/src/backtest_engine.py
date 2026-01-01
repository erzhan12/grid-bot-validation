"""
Backtest Engine

Main orchestrator for running backtests using the existing architecture.
Integrates with Controller, Strat50, and BybitApiUsdt to provide
realistic backtesting simulation.
"""

from datetime import datetime, timedelta
from typing import Dict, Optional

from src.backtest_session import BacktestSession
from src.constants import DEFAULT_FUNDING_RATE, FUNDING_INTERVAL_HOURS
from src.controller import Controller


class BacktestEngine:
    """
    Main backtesting engine that orchestrates the entire backtest process.
    
    Leverages the existing architecture:
    - Controller manages strategies and market makers
    - Strat50 processes historical data and manages grid orders
    - BybitApiUsdt handles order management in backtest mode
    - BacktestSession stores all results in memory
    """
    
    def __init__(self, config=None, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None):
        self.config = config
        self.start_date = start_date
        self.end_date = end_date
        self.session_id = self._generate_session_id()
        self.backtest_session = BacktestSession(self.session_id)
        self.controllers: Dict[str, Controller] = {}
        self.results: Dict[str, dict] = {}

        # Configuration
        self.initial_balance = 10000.0  # Default balance
        self.verbose = True

        # Funding configuration
        self.enable_funding = True
        self.funding_rate = DEFAULT_FUNDING_RATE
        self.funding_interval_hours = FUNDING_INTERVAL_HOURS
        self.last_funding_time = None
        self.total_funding_paid = 0.0
        
    def run_backtest(self, symbol: str) -> Dict[str, dict]:
        """
        Main backtesting loop for a single symbol.
        
        Args:
            symbol: Trading symbol to backtest (e.g., 'BTCUSDT')
            
        Returns:
            Dictionary containing backtest results and metrics
        """
        if self.verbose:
            print(f"🚀 Starting backtest for {symbol}")
            print(f"Session ID: {self.session_id}")
            print(f"Initial Balance: ${self.initial_balance:,.2f}")
        
        try:
            # Initialize controller for symbol
            controller = Controller(symbol)
            self.controllers[symbol] = controller
            
            # Initialize backtest mode for all market makers
            self._initialize_backtest_mode(controller)
            
            if self.verbose:
                print(f"✅ Initialized {len(controller.bms)} market makers")
                print(f"✅ Initialized {len(controller.strats)} strategies")
            
            # Run the backtest by leveraging existing architecture
            # The Strat50._check_pair_step will iterate through historical data
            # and our enhanced methods will handle order fills and position tracking
            controller.check_job()
            
            # Generate final metrics
            final_metrics = self.backtest_session.get_final_metrics()
            
            # Create comprehensive results
            results = self._create_results_summary(symbol, final_metrics)
            self.results[symbol] = results
            
            if self.verbose:
                self._print_results_summary(symbol, results)
                
            return final_metrics
            
        except Exception as e:
            print(f"❌ Backtest failed for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            return {}
    
    def run_multiple_symbols(self, symbols: list) -> Dict[str, dict]:
        """
        Run backtests for multiple symbols.
        
        Args:
            symbols: List of trading symbols
            
        Returns:
            Dictionary with results for each symbol
        """
        all_results = {}
        
        for symbol in symbols:
            print(f"\n{'=' * 50}")
            symbol_results = self.run_backtest(symbol)
            all_results[symbol] = symbol_results
            
            # Create new session for next symbol
            if symbol != symbols[-1]:  # Not the last symbol
                self.session_id = self._generate_session_id()
                self.backtest_session = BacktestSession(self.session_id)
        
        return all_results

    def check_and_apply_funding(self, current_time: datetime, symbol: str):
        """
        Check if funding should be applied and apply it to all positions

        Args:
            current_time: Current backtest timestamp
            symbol: Trading symbol
        """
        if not self.enable_funding:
            return

        # Initialize last funding time if not set
        if self.last_funding_time is None:
            # Set to the most recent funding time before current_time
            self.last_funding_time = self._get_previous_funding_time(current_time)
            if self.verbose:
                print(f"💰 Funding tracking initialized at {self.last_funding_time}")
            return

        # Check if it's time for funding payment
        next_funding_time = self._get_next_funding_time(self.last_funding_time)

        if current_time >= next_funding_time:
            self._apply_funding_payments(current_time, symbol)
            self.last_funding_time = next_funding_time

    def _get_next_funding_time(self, current_time: datetime) -> datetime:
        """Get the next funding time (00:00, 08:00, 16:00 UTC)"""
        funding_hours = [0, 8, 16]
        current_hour = current_time.hour

        # Find next funding hour
        next_hour = None
        for hour in funding_hours:
            if hour > current_hour:
                next_hour = hour
                break

        if next_hour is None:
            # Next funding is tomorrow at 00:00
            next_time = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
            next_time = next_time + timedelta(days=1)
        else:
            next_time = current_time.replace(hour=next_hour, minute=0, second=0, microsecond=0)

        return next_time

    def _get_previous_funding_time(self, current_time: datetime) -> datetime:
        """Get the most recent funding time before current_time"""
        funding_hours = [0, 8, 16]
        current_hour = current_time.hour

        # Find previous funding hour
        prev_hour = None
        for hour in reversed(funding_hours):
            if hour <= current_hour:
                prev_hour = hour
                break

        if prev_hour is None:
            # Previous funding was yesterday at 16:00
            prev_time = current_time.replace(hour=16, minute=0, second=0, microsecond=0)
            prev_time = prev_time - timedelta(days=1)
        else:
            prev_time = current_time.replace(hour=prev_hour, minute=0, second=0, microsecond=0)

        return prev_time

    def _apply_funding_payments(self, current_time: datetime, symbol: str):
        """Apply funding payments to all open positions"""
        if symbol not in self.controllers:
            return

        controller = self.controllers[symbol]
        total_funding_this_cycle = 0.0

        # Apply funding to all market makers
        for bm in controller.bms:
            if hasattr(bm, 'apply_funding_payments'):
                # Get current price for funding calculation
                current_price = self._get_current_price(bm)

                if current_price > 0:
                    funding_paid = bm.apply_funding_payments(
                        current_price=current_price,
                        funding_rate=self.funding_rate,
                        timestamp=current_time
                    )
                    total_funding_this_cycle += funding_paid

        # Track total funding paid
        self.total_funding_paid += total_funding_this_cycle

        # Update session with funding costs
        if hasattr(self.backtest_session, 'total_funding_paid'):
            self.backtest_session.total_funding_paid = self.total_funding_paid

        if self.verbose and abs(total_funding_this_cycle) > 0.01:
            print(f"💰 Funding payment applied at {current_time.strftime('%Y-%m-%d %H:%M')}: "
                  f"${total_funding_this_cycle:+.4f} (Total: ${self.total_funding_paid:+.2f})")

    def _get_current_price(self, market_maker) -> float:
        """Get current price from market maker for funding calculations"""
        try:
            # Try to get price from ticker data
            if hasattr(market_maker, 'ticker_data') and market_maker.ticker_data:
                return float(market_maker.ticker_data.get('lastPrice', 0))

            # Try to get from position data
            for direction in ['LONG', 'SHORT']:
                if hasattr(market_maker, 'position') and market_maker.position:
                    from src.enums import Direction
                    pos = market_maker.position.get(Direction(direction.lower()))
                    if pos and hasattr(pos, 'entry_price') and pos.entry_price > 0:
                        return pos.entry_price

            return 0.0
        except Exception:
            return 0.0

    def set_funding_configuration(self, enable: bool = True, rate: float = None):
        """
        Configure funding payments for backtest

        Args:
            enable: Whether to enable funding payments
            rate: Custom funding rate (None to use default)
        """
        self.enable_funding = enable
        if rate is not None:
            self.funding_rate = rate

        if self.verbose:
            if enable:
                print(f"💰 Funding payments enabled: {self.funding_rate:.6f} "
                      f"({self.funding_rate * 100:.4f}%) every {self.funding_interval_hours}h")
            else:
                print("💰 Funding payments disabled")

    def get_funding_summary(self) -> Dict[str, float]:
        """Get summary of funding payments"""
        return {
            'total_funding_paid': self.total_funding_paid,
            'funding_rate_used': self.funding_rate,
            'funding_enabled': self.enable_funding
        }

    def _initialize_backtest_mode(self, controller: Controller):
        """Initialize backtest mode for all market makers"""
        for bm in controller.bms:
            bm.init_backtest_mode(self.backtest_session)
            
        # Set initial balance in session
        self.backtest_session.initial_balance = self.initial_balance
        self.backtest_session.current_balance = self.initial_balance

    def _create_results_summary(self, symbol: str, metrics: Dict) -> dict:
        """Create comprehensive results summary"""
        session_summary = self.backtest_session.get_summary()

        results = {
            'symbol': symbol,
            'session_id': self.session_id,
            'backtest_info': {
                'start_time': self.backtest_session.start_time,
                'end_time': datetime.now(),
                'initial_balance': self.initial_balance,
                'final_balance': session_summary['current_balance'],
                'total_return_pct': session_summary['return_pct']
            },
            'trading_metrics': {
                'total_trades': session_summary['total_trades'],
                'winning_trades': session_summary['winning_trades'],
                'win_rate_pct': session_summary['win_rate'],
                'total_pnl': session_summary['total_pnl']
            },
            'funding_metrics': {
                'total_funding_paid': self.total_funding_paid,
                'funding_rate_used': self.funding_rate,
                'funding_enabled': self.enable_funding,
                'net_pnl_after_funding': session_summary['total_pnl'] - self.total_funding_paid
            },
            'detailed_metrics': metrics.get(symbol, {}),
            'session_summary': session_summary
        }
        
        # Add order management statistics if available
        if self.controllers.get(symbol):
            controller = self.controllers[symbol]
            for bm in controller.bms:
                if hasattr(bm, 'backtest_order_manager') and bm.backtest_order_manager:
                    results['order_stats'] = bm.backtest_order_manager.get_statistics()
                    break
        
        return results
    
    def _print_results_summary(self, symbol: str, results: dict):
        """Print formatted results summary"""
        print(f"\n🎯 BACKTEST RESULTS for {symbol}")
        print("=" * 50)

        backtest_info = results['backtest_info']
        trading_metrics = results['trading_metrics']
        funding_metrics = results.get('funding_metrics', {})

        print("💰 Financial Performance:")
        print(f"   Initial Balance: ${backtest_info['initial_balance']:,.2f}")
        print(f"   Final Balance:   ${backtest_info['final_balance']:,.2f}")
        print(f"   Total Return:    {backtest_info['total_return_pct']:+.2f}%")
        print(f"   Total PnL:       ${trading_metrics['total_pnl']:+,.2f}")

        # Include funding metrics if available
        if funding_metrics.get('funding_enabled'):
            total_funding = funding_metrics.get('total_funding_paid', 0)
            net_pnl = funding_metrics.get('net_pnl_after_funding', trading_metrics['total_pnl'])
            print(f"   Funding Paid:    ${total_funding:+,.2f}")
            print(f"   Net PnL (after funding): ${net_pnl:+,.2f}")

        print("\n📊 Trading Statistics:")
        print(f"   Total Trades:    {trading_metrics['total_trades']}")
        print(f"   Winning Trades:  {trading_metrics['winning_trades']}")
        print(f"   Win Rate:        {trading_metrics['win_rate_pct']:.1f}%")
        
        if 'order_stats' in results:
            order_stats = results['order_stats']
            print("\n🎯 Order Management:")
            print(f"   Orders Created:  {order_stats['total_orders_created']}")
            print(f"   Orders Filled:   {order_stats['filled_orders']}")
            print(f"   Fill Rate:       {order_stats['fill_rate'] * 100:.1f}%")
            print(f"   Slippage:        {order_stats['slippage_bps']} bps")
        
        print("=" * 50)
    
    def export_results(self, output_dir: str = "./backtest_results"):
        """Export all results to CSV files"""
        from src.backtest_reporter import BacktestReporter
        
        reporter = BacktestReporter(self.backtest_session)
        reporter.export_to_csv(output_dir)
        
        print(f"📁 Results exported to {output_dir}")
    
    def get_session_summary(self) -> dict:
        """Get current session summary"""
        return self.backtest_session.get_summary()
    
    def set_initial_balance(self, balance: float):
        """Set initial balance for backtesting"""
        self.initial_balance = balance
        
    def set_verbose(self, verbose: bool):
        """Enable/disable verbose output"""
        self.verbose = verbose
    
    def _generate_session_id(self) -> str:
        """Generate unique session ID with microsecond precision"""
        return f"BT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


class BacktestRunner:
    """
    Simplified runner for quick backtests.
    
    Usage:
        runner = BacktestRunner()
        results = runner.run("BTCUSDT")
    """
    
    def __init__(self, initial_balance: float = 10000.0, verbose: bool = True):
        self.initial_balance = initial_balance
        self.verbose = verbose
    
    def run(self, symbol: str) -> dict:
        """Run a quick backtest for a single symbol"""
        engine = BacktestEngine()
        engine.set_initial_balance(self.initial_balance)
        engine.set_verbose(self.verbose)
        
        return engine.run_backtest(symbol)
    
    def run_multiple(self, symbols: list) -> Dict[str, dict]:
        """Run backtests for multiple symbols"""
        engine = BacktestEngine()
        engine.set_initial_balance(self.initial_balance)
        engine.set_verbose(self.verbose)
        
        return engine.run_multiple_symbols(symbols)


# Convenience function for quick backtests
def quick_backtest(symbol: str, initial_balance: float = 10000.0) -> dict:
    """
    Run a quick backtest for a single symbol.
    
    Args:
        symbol: Trading symbol (e.g., 'BTCUSDT')
        initial_balance: Starting balance in USD
        
    Returns:
        Dictionary containing backtest results
    """
    runner = BacktestRunner(initial_balance=initial_balance)
    return runner.run(symbol)