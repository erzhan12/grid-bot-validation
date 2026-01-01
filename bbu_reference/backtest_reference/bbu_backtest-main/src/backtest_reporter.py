"""
Backtest Reporter

Generates comprehensive reports and exports from BacktestSession data.
"""

import csv
import os

from src.backtest_session import BacktestSession


class BacktestReporter:
    """Generate reports and export data from backtest sessions"""
    
    def __init__(self, backtest_session: BacktestSession):
        self.session = backtest_session
        
    def generate_summary_report(self) -> dict:
        """Generate comprehensive backtest summary from in-memory data"""
        return {
            'session_info': {
                'session_id': self.session.session_id,
                'start_time': self.session.start_time,
                'total_trades': len(self.session.trades)
            },
            'performance_metrics': self._calculate_performance_metrics(),
            'trade_analysis': self._analyze_trades(),
            'position_analysis': self._analyze_positions()
        }
    
    def _calculate_performance_metrics(self) -> dict:
        """Calculate key performance metrics"""
        trades = self.session.trades
        if not trades:
            return {}
            
        total_pnl = sum(t.realized_pnl for t in trades)
        winning_trades = [t for t in trades if t.realized_pnl > 0]
        losing_trades = [t for t in trades if t.realized_pnl < 0]
        
        return {
            'total_trades': len(trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(trades) if trades else 0,
            'total_pnl': total_pnl,
            'avg_win': sum(t.realized_pnl for t in winning_trades) / len(winning_trades) if winning_trades else 0,
            'avg_loss': sum(t.realized_pnl for t in losing_trades) / len(losing_trades) if losing_trades else 0,
            'largest_win': max((t.realized_pnl for t in winning_trades), default=0),
            'largest_loss': min((t.realized_pnl for t in losing_trades), default=0),
            'profit_factor': (abs(sum(t.realized_pnl for t in winning_trades) / sum(t.realized_pnl for t in losing_trades))
                            if losing_trades and sum(t.realized_pnl for t in losing_trades) != 0 else 0)
        }
    
    def _analyze_trades(self) -> dict:
        """Analyze trading patterns"""
        trades = self.session.trades
        if not trades:
            return {}
        
        # Group by symbol
        symbols = {}
        for trade in trades:
            if trade.symbol not in symbols:
                symbols[trade.symbol] = []
            symbols[trade.symbol].append(trade)
        
        # Analyze by side (Buy/Sell)
        buy_trades = [t for t in trades if t.side.lower() == 'buy']
        sell_trades = [t for t in trades if t.side.lower() == 'sell']
        
        # Analyze by direction (long/short)
        long_trades = [t for t in trades if t.direction == 'long']
        short_trades = [t for t in trades if t.direction == 'short']
        
        return {
            'symbols': list(symbols.keys()),
            'trades_per_symbol': {symbol: len(symbol_trades) for symbol, symbol_trades in symbols.items()},
            'buy_trades': len(buy_trades),
            'sell_trades': len(sell_trades),
            'long_trades': len(long_trades),
            'short_trades': len(short_trades),
            'avg_trade_size': sum(t.size for t in trades) / len(trades),
            'total_volume': sum(t.size * t.price for t in trades)
        }
    
    def _analyze_positions(self) -> dict:
        """Analyze position data"""
        snapshots = self.session.position_snapshots
        if not snapshots:
            return {}
        
        # Group by symbol and direction
        positions = {}
        for snapshot in snapshots:
            key = f"{snapshot.symbol}_{snapshot.direction}"
            if key not in positions:
                positions[key] = []
            positions[key].append(snapshot)
        
        # Calculate max position sizes and PnL
        max_unrealized_pnl = max((s.unrealized_pnl for s in snapshots), default=0)
        min_unrealized_pnl = min((s.unrealized_pnl for s in snapshots), default=0)
        
        return {
            'total_snapshots': len(snapshots),
            'unique_positions': len(positions),
            'max_unrealized_pnl': max_unrealized_pnl,
            'min_unrealized_pnl': min_unrealized_pnl,
            'position_breakdown': {key: len(snapshots) for key, snapshots in positions.items()}
        }
    
    def export_to_csv(self, output_dir: str = "./backtest_results"):
        """Export detailed results to CSV files"""
        os.makedirs(output_dir, exist_ok=True)
        
        # Export trades
        self._export_trades_csv(output_dir)
        
        # Export position snapshots
        self._export_positions_csv(output_dir)
        
        # Export summary
        self._export_summary_csv(output_dir)
        
        print(f"📊 Results exported to {output_dir}")
    
    def _export_trades_csv(self, output_dir: str):
        """Export trades to CSV"""
        trades_file = os.path.join(output_dir, f"trades_{self.session.session_id}.csv")
        
        with open(trades_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Trade ID', 'Symbol', 'Side', 'Size', 'Price', 'Direction', 
                'Executed At', 'Order ID', 'Strategy ID', 'BM Name', 'Realized PnL'
            ])
            
            for trade in self.session.trades:
                writer.writerow([
                    trade.trade_id, trade.symbol, trade.side, trade.size, 
                    trade.price, trade.direction, trade.executed_at, 
                    trade.order_id, trade.strategy_id, trade.bm_name, trade.realized_pnl
                ])
    
    def _export_positions_csv(self, output_dir: str):
        """Export position snapshots to CSV"""
        positions_file = os.path.join(output_dir, f"positions_{self.session.session_id}.csv")
        
        with open(positions_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Timestamp', 'Symbol', 'Direction', 'Size', 'Entry Price', 
                'Current Price', 'Unrealized PnL', 'Margin', 'Liquidation Price'
            ])
            
            for pos in self.session.position_snapshots:
                writer.writerow([
                    pos.timestamp, pos.symbol, pos.direction, pos.size,
                    pos.entry_price, pos.current_price, pos.unrealized_pnl, 
                    pos.margin, pos.liquidation_price
                ])
    
    def _export_summary_csv(self, output_dir: str):
        """Export performance summary to CSV"""
        summary_file = os.path.join(output_dir, f"summary_{self.session.session_id}.csv")
        
        metrics = self._calculate_performance_metrics()
        trade_analysis = self._analyze_trades()
        self._analyze_positions()
        
        with open(summary_file, 'w', newline='') as f:
            writer = csv.writer(f)
            
            # Session info
            writer.writerow(['Metric', 'Value'])
            writer.writerow(['Session ID', self.session.session_id])
            writer.writerow(['Start Time', self.session.start_time])
            writer.writerow(['Initial Balance', self.session.initial_balance])
            writer.writerow(['Final Balance', self.session.current_balance])
            return_pct = ((self.session.current_balance - self.session.initial_balance)
                         / self.session.initial_balance * 100)
            writer.writerow(['Return %', return_pct])
            writer.writerow([])
            
            # Performance metrics
            writer.writerow(['Performance Metrics', ''])
            for key, value in metrics.items():
                writer.writerow([key.replace('_', ' ').title(), value])
            writer.writerow([])
            
            # Trade analysis
            writer.writerow(['Trade Analysis', ''])
            for key, value in trade_analysis.items():
                if key != 'trades_per_symbol':
                    writer.writerow([key.replace('_', ' ').title(), value])
    
    def print_detailed_report(self):
        """Print a detailed formatted report"""
        summary = self.generate_summary_report()
        
        print(f"\n{'=' * 60}")
        print("DETAILED BACKTEST REPORT")
        print(f"{'=' * 60}")
        
        # Session info
        session_info = summary['session_info']
        print("\n📋 Session Information:")
        print(f"   Session ID: {session_info['session_id']}")
        print(f"   Start Time: {session_info['start_time']}")
        print(f"   Total Trades: {session_info['total_trades']}")
        
        # Performance metrics
        metrics = summary['performance_metrics']
        if metrics:
            print("\n📊 Performance Metrics:")
            print(f"   Total Trades: {metrics['total_trades']}")
            print(f"   Winning Trades: {metrics['winning_trades']}")
            print(f"   Losing Trades: {metrics['losing_trades']}")
            print(f"   Win Rate: {metrics['win_rate'] * 100:.1f}%")
            print(f"   Total PnL: ${metrics['total_pnl']:,.2f}")
            print(f"   Average Win: ${metrics['avg_win']:,.2f}")
            print(f"   Average Loss: ${metrics['avg_loss']:,.2f}")
            print(f"   Largest Win: ${metrics['largest_win']:,.2f}")
            print(f"   Largest Loss: ${metrics['largest_loss']:,.2f}")
            print(f"   Profit Factor: {metrics['profit_factor']:.2f}")
        
        # Trade analysis
        trade_analysis = summary['trade_analysis']
        if trade_analysis:
            print("\n🔄 Trade Analysis:")
            print(f"   Symbols Traded: {', '.join(trade_analysis['symbols'])}")
            print(f"   Buy Trades: {trade_analysis['buy_trades']}")
            print(f"   Sell Trades: {trade_analysis['sell_trades']}")
            print(f"   Long Trades: {trade_analysis['long_trades']}")
            print(f"   Short Trades: {trade_analysis['short_trades']}")
            print(f"   Average Trade Size: {trade_analysis['avg_trade_size']:.6f}")
            print(f"   Total Volume: ${trade_analysis['total_volume']:,.2f}")
        
        # Position analysis
        position_analysis = summary['position_analysis']
        if position_analysis:
            print("\n📈 Position Analysis:")
            print(f"   Total Snapshots: {position_analysis['total_snapshots']}")
            print(f"   Unique Positions: {position_analysis['unique_positions']}")
            print(f"   Max Unrealized PnL: ${position_analysis['max_unrealized_pnl']:,.2f}")
            print(f"   Min Unrealized PnL: ${position_analysis['min_unrealized_pnl']:,.2f}")
        
        print(f"\n{'=' * 60}")


def create_report(backtest_session: BacktestSession) -> BacktestReporter:
    """Convenience function to create a reporter"""
    return BacktestReporter(backtest_session)
