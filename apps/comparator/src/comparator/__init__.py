"""
Comparator package for validating backtest results against live trade data.

Compares gridcore backtest output with real exchange executions
to validate fill accuracy, PnL, and trade coverage.
"""

from comparator.config import ComparatorConfig
from comparator.equity import EquityComparator
from comparator.loader import NormalizedTrade, LiveTradeLoader, BacktestTradeLoader
from comparator.matcher import TradeMatcher, MatchedTrade, MatchResult
from comparator.metrics import ValidationMetrics, TradeDelta, calculate_metrics
from comparator.reporter import ComparatorReporter

__all__ = [
    # Config
    "ComparatorConfig",
    # Equity
    "EquityComparator",
    # Loader
    "NormalizedTrade",
    "LiveTradeLoader",
    "BacktestTradeLoader",
    # Matcher
    "TradeMatcher",
    "MatchedTrade",
    "MatchResult",
    # Metrics
    "ValidationMetrics",
    "TradeDelta",
    "calculate_metrics",
    # Reporter
    "ComparatorReporter",
]
