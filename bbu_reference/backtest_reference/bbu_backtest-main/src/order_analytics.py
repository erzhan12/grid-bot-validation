"""
Order Analytics and Direction-Specific Tracking

This module provides analytics classes for tracking order performance
and direction-specific metrics in the backtest system.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src.enums import OrderStatus


@dataclass
class SlippageStats:
    """Slippage statistics for orders"""
    total_slippage: float = 0.0
    avg_slippage: float = 0.0
    max_slippage: float = 0.0
    min_slippage: float = 0.0
    slippage_count: int = 0


@dataclass
class OrderPerformanceMetrics:
    """Performance metrics for orders"""
    total_pnl: float = 0.0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0


@dataclass
class DirectionOrderAnalytics:
    """Analytics for orders in a specific direction"""
    direction: str
    total_orders: int = 0
    filled_orders: int = 0
    cancelled_orders: int = 0
    expired_orders: int = 0
    failed_orders: int = 0
    fill_rate: float = 0.0
    avg_fill_time: Optional[timedelta] = None
    total_volume: float = 0.0
    avg_order_size: float = 0.0
    price_impact: float = 0.0
    slippage_stats: SlippageStats = field(default_factory=SlippageStats)
    performance_metrics: OrderPerformanceMetrics = field(default_factory=OrderPerformanceMetrics)
    
    # Time-based tracking
    first_order_time: Optional[datetime] = None
    last_order_time: Optional[datetime] = None
    order_timestamps: List[datetime] = field(default_factory=list)
    
    def update_with_order(self, order, fill_time: Optional[datetime] = None, is_new_order: bool = True):  # noqa: C901
        """Update analytics with a new order or order status change"""
        if is_new_order:
            self.total_orders += 1
            self.total_volume += order.size
            self.avg_order_size = self.total_volume / self.total_orders
            
            # Update time tracking
            if order.created_at:
                self.order_timestamps.append(order.created_at)
                if not self.first_order_time or order.created_at < self.first_order_time:
                    self.first_order_time = order.created_at
                if not self.last_order_time or order.created_at > self.last_order_time:
                    self.last_order_time = order.created_at
        
        # Update status-specific counters
        if order.status == OrderStatus.FILLED:
            self.filled_orders += 1
            if fill_time and order.created_at:
                fill_duration = fill_time - order.created_at
                if not self.avg_fill_time:
                    self.avg_fill_time = fill_duration
                else:
                    # Simple moving average
                    self.avg_fill_time = (self.avg_fill_time + fill_duration) / 2
        elif order.status == OrderStatus.CANCELLED:
            self.cancelled_orders += 1
        elif order.status == OrderStatus.EXPIRED:
            self.expired_orders += 1
        
        # Update fill rate
        if self.total_orders > 0:
            self.fill_rate = self.filled_orders / self.total_orders
    
    def calculate_performance_metrics(self, trades: List[Dict]) -> None:
        """Calculate performance metrics from trade data"""
        if not trades:
            return
        
        direction_trades = [t for t in trades if t.get('direction') == self.direction]
        if not direction_trades:
            return
        
        pnl_values = [t.get('realized_pnl', 0.0) for t in direction_trades]
        winning_trades = [pnl for pnl in pnl_values if pnl > 0]
        losing_trades = [pnl for pnl in pnl_values if pnl < 0]
        
        self.performance_metrics.total_pnl = sum(pnl_values)
        self.performance_metrics.winning_trades = len(winning_trades)
        self.performance_metrics.losing_trades = len(losing_trades)
        
        if len(pnl_values) > 0:
            self.performance_metrics.win_rate = len(winning_trades) / len(pnl_values) * 100
        
        if winning_trades:
            self.performance_metrics.avg_win = sum(winning_trades) / len(winning_trades)
        
        if losing_trades:
            self.performance_metrics.avg_loss = sum(losing_trades) / len(losing_trades)
        
        if self.performance_metrics.avg_loss != 0:
            self.performance_metrics.profit_factor = abs(
                self.performance_metrics.avg_win / self.performance_metrics.avg_loss
            )
        
        # Calculate max drawdown
        if pnl_values:
            running_pnl = 0
            peak = 0
            max_dd = 0
            for pnl in pnl_values:
                running_pnl += pnl
                if running_pnl > peak:
                    peak = running_pnl
                drawdown = peak - running_pnl
                max_dd = max(max_dd, drawdown)
            self.performance_metrics.max_drawdown = max_dd


@dataclass
class CrossDirectionStats:
    """Cross-direction analytics comparing long vs short orders"""
    long_analytics: DirectionOrderAnalytics
    short_analytics: DirectionOrderAnalytics
    order_imbalance: float = 0.0  # Ratio of long to short orders
    volume_imbalance: float = 0.0  # Ratio of long to short volume
    performance_difference: float = 0.0  # PnL difference between directions
    
    def __post_init__(self):
        """Calculate cross-direction metrics"""
        self._calculate_imbalances()
        self._calculate_performance_difference()
    
    def _calculate_imbalances(self):
        """Calculate order and volume imbalances"""
        if self.short_analytics.total_orders > 0:
            self.order_imbalance = self.long_analytics.total_orders / self.short_analytics.total_orders
        else:
            self.order_imbalance = float('inf') if self.long_analytics.total_orders > 0 else 0.0
        
        if self.short_analytics.total_volume > 0:
            self.volume_imbalance = self.long_analytics.total_volume / self.short_analytics.total_volume
        else:
            self.volume_imbalance = float('inf') if self.long_analytics.total_volume > 0 else 0.0
    
    def _calculate_performance_difference(self):
        """Calculate performance difference between directions"""
        long_pnl = self.long_analytics.performance_metrics.total_pnl
        short_pnl = self.short_analytics.performance_metrics.total_pnl
        self.performance_difference = long_pnl - short_pnl


@dataclass
class OrderSummary:
    """Summary of orders for a specific time period"""
    direction: str
    symbol: str
    time_range: Tuple[datetime, datetime]
    total_orders: int
    filled_orders: int
    cancelled_orders: int
    total_volume: float
    avg_fill_time: Optional[timedelta]
    fill_rate: float
    total_pnl: float
    win_rate: float
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for easy serialization"""
        return {
            'direction': self.direction,
            'symbol': self.symbol,
            'time_range': {
                'start': self.time_range[0].isoformat() if self.time_range[0] else None,
                'end': self.time_range[1].isoformat() if self.time_range[1] else None
            },
            'total_orders': self.total_orders,
            'filled_orders': self.filled_orders,
            'cancelled_orders': self.cancelled_orders,
            'total_volume': self.total_volume,
            'avg_fill_time_seconds': self.avg_fill_time.total_seconds() if self.avg_fill_time else None,
            'fill_rate': self.fill_rate,
            'total_pnl': self.total_pnl,
            'win_rate': self.win_rate
        }
