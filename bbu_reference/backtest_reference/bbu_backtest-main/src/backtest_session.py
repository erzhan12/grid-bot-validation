"""
Backtest Session Management

This module provides in-memory data structures for storing backtest results
during execution. All data lives in memory and can be optionally exported
after the backtest completes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple


@dataclass
class BacktestTrade:
    """Represents an executed trade during backtesting"""
    trade_id: str
    symbol: str
    side: str  # 'Buy' or 'Sell'
    size: float
    price: float
    direction: str  # 'long' or 'short'
    executed_at: datetime
    order_id: str
    strategy_id: int
    bm_name: str
    realized_pnl: float = 0.0


@dataclass
class BacktestPositionSnapshot:
    """Position state at a specific timestamp"""
    timestamp: datetime
    symbol: str
    direction: str  # 'long' or 'short'
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    margin: float
    liquidation_price: float


@dataclass
class BacktestMetrics:
    """Performance metrics for a symbol/strategy"""
    symbol: str
    strategy_id: int
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_profit: float = 0.0
    start_balance: float = 0.0
    end_balance: float = 0.0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    win_rate: float = 0.0
    profit_factor: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)
    position_history: List[BacktestPositionSnapshot] = field(default_factory=list)


class BacktestSession:
    """In-memory storage for single backtest run"""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.start_time = datetime.now()
        
        # In-memory storage
        self.trades: List[BacktestTrade] = []
        self.position_snapshots: List[BacktestPositionSnapshot] = []
        self.metrics: Dict[str, BacktestMetrics] = {}
        
        # Current state
        self.current_timestamp: Optional[datetime] = None
        self.equity_curve: List[Tuple[datetime, float]] = []
        
        # Configuration
        self.initial_balance = 10000.0  # Default, can be overridden
        self.current_balance = self.initial_balance
        
    def record_trade(self, trade: BacktestTrade):
        """Record executed trade"""
        self.trades.append(trade)
        
        # Update current balance with realized PnL
        self.current_balance += trade.realized_pnl
        
        # Determine action based on direction and side
        if trade.direction == 'long' and trade.side == 'Buy':
            action = "Open Long"
        elif trade.direction == 'long' and trade.side == 'Sell':
            action = "Close Long"
        elif trade.direction == 'short' and trade.side == 'Sell':
            action = "Open Short"
        elif trade.direction == 'short' and trade.side == 'Buy':
            action = "Close Short"
        else:
            action = f"{trade.direction} {trade.side}"
        
        print(f"Trade recorded: {trade.trade_id} [{action}] {trade.size} {trade.symbol} @ {trade.price} (PnL: {trade.realized_pnl:.5f})")
        
    def record_position_snapshot(self, snapshot: BacktestPositionSnapshot):
        """Record position state at specific timestamp"""
        self.position_snapshots.append(snapshot)
        
    def update_equity(self, timestamp: datetime, total_equity: float):
        """Update equity curve"""
        self.equity_curve.append((timestamp, total_equity))
        
    def get_final_metrics(self) -> Dict[str, BacktestMetrics]:
        """Calculate and return final performance metrics"""
        # Group trades by symbol
        symbols = set(trade.symbol for trade in self.trades)
        
        for symbol in symbols:
            self._calculate_metrics(symbol)
            
        return self.metrics
    
    def _calculate_metrics(self, symbol: str):
        """Calculate performance metrics for a symbol"""
        symbol_trades = [t for t in self.trades if t.symbol == symbol]
        
        if not symbol_trades:
            return
            
        winning_trades = [t for t in symbol_trades if t.realized_pnl > 0]
        losing_trades = [t for t in symbol_trades if t.realized_pnl < 0]
        
        total_pnl = sum(t.realized_pnl for t in symbol_trades)
        total_winning_pnl = sum(t.realized_pnl for t in winning_trades)
        total_losing_pnl = sum(t.realized_pnl for t in losing_trades)
        
        # Calculate equity curve for this symbol
        equity_points = []
        running_pnl = self.initial_balance
        for trade in sorted(symbol_trades, key=lambda x: x.executed_at):
            running_pnl += trade.realized_pnl
            equity_points.append(running_pnl)
        
        # Calculate maximum drawdown
        max_drawdown = 0.0
        peak = self.initial_balance
        for equity in equity_points:
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak * 100
            max_drawdown = max(max_drawdown, drawdown)
        
        metrics = BacktestMetrics(
            symbol=symbol,
            strategy_id=symbol_trades[0].strategy_id if symbol_trades else 0,
            total_trades=len(symbol_trades),
            winning_trades=len(winning_trades),
            total_pnl=total_pnl,
            max_drawdown=max_drawdown,
            max_profit=max(equity_points) - self.initial_balance if equity_points else 0,
            start_balance=self.initial_balance,
            end_balance=self.initial_balance + total_pnl,
            start_time=symbol_trades[0].executed_at if symbol_trades else None,
            end_time=symbol_trades[-1].executed_at if symbol_trades else None,
            trades=symbol_trades,
            position_history=[p for p in self.position_snapshots if p.symbol == symbol]
        )
        
        # Calculate additional metrics
        if len(symbol_trades) > 0:
            metrics.win_rate = len(winning_trades) / len(symbol_trades) * 100
            if total_losing_pnl != 0:
                metrics.profit_factor = abs(total_winning_pnl / total_losing_pnl)
        
        self.metrics[symbol] = metrics
    
    def get_summary(self) -> dict:
        """Get a quick summary of the backtest session"""
        total_trades = len(self.trades)
        total_pnl = sum(trade.realized_pnl for trade in self.trades)
        winning_trades = len([t for t in self.trades if t.realized_pnl > 0])
        
        return {
            'session_id': self.session_id,
            'start_time': self.start_time,
            'current_time': self.current_timestamp,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'win_rate': (winning_trades / total_trades * 100) if total_trades > 0 else 0,
            'total_pnl': total_pnl,
            'current_balance': self.current_balance,
            'return_pct': ((self.current_balance - self.initial_balance) / self.initial_balance * 100),
            'symbols': list(set(trade.symbol for trade in self.trades))
        }
    
    def print_summary(self):
        """Print a formatted summary of the backtest"""
        summary = self.get_summary()
        
        print("\n=== BACKTEST SESSION SUMMARY ===")
        print(f"Session ID: {summary['session_id']}")
        print(f"Started: {summary['start_time']}")
        print(f"Current Time: {summary['current_time']}")
        print(f"Symbols: {', '.join(summary['symbols'])}")
        print(f"Total Trades: {summary['total_trades']}")
        print(f"Winning Trades: {summary['winning_trades']}")
        print(f"Win Rate: {summary['win_rate']:.2f}%")
        print(f"Total PnL: ${summary['total_pnl']:.2f}")
        print(f"Current Balance: ${summary['current_balance']:.2f}")
        print(f"Return: {summary['return_pct']:.2f}%")
        print("================================\n")
