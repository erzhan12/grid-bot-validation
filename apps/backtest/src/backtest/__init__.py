"""
Backtest package for grid trading strategy simulation.

Uses gridcore's GridEngine with trade-through fill model.
"""

from backtest.config import BacktestConfig, BacktestStrategyConfig
from backtest.session import BacktestSession, BacktestMetrics, BacktestTrade
from backtest.fill_simulator import TradeThroughFillSimulator
from backtest.position_tracker import BacktestPositionTracker, PositionState
from backtest.order_manager import BacktestOrderManager, SimulatedOrder
from backtest.executor import BacktestExecutor
from backtest.runner import BacktestRunner
from backtest.engine import BacktestEngine

__all__ = [
    # Config
    "BacktestConfig",
    "BacktestStrategyConfig",
    # Session
    "BacktestSession",
    "BacktestMetrics",
    "BacktestTrade",
    # Core
    "TradeThroughFillSimulator",
    "BacktestPositionTracker",
    "PositionState",
    "BacktestOrderManager",
    "SimulatedOrder",
    "BacktestExecutor",
    "BacktestRunner",
    "BacktestEngine",
]
