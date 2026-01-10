"""Event saver data collection service for grid bot validation.

This application captures:
- Public market data (tickers, trades) for configurable symbols
- Private account data (executions, orders, positions, wallet)
- REST reconciliation for gap detection on reconnection
"""

from event_saver.config import EventSaverConfig
from event_saver.main import EventSaver
from event_saver.reconciler import GapReconciler
from event_saver.collectors import PublicCollector, PrivateCollector, AccountContext
from event_saver.writers import TradeWriter, ExecutionWriter

__all__ = [
    "EventSaverConfig",
    "EventSaver",
    "GapReconciler",
    "PublicCollector",
    "PrivateCollector",
    "AccountContext",
    "TradeWriter",
    "ExecutionWriter",
]
