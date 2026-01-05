"""
gridcore - Pure grid trading strategy logic with zero exchange dependencies.

This package contains the core grid trading strategy implementation extracted
from bbu2-master, designed to be usable by both live trading and backtesting
applications.
"""

from gridcore.events import Event, EventType, TickerEvent, PublicTradeEvent, ExecutionEvent, OrderUpdateEvent
from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridcore.config import GridConfig
from gridcore.grid import Grid
from gridcore.engine import GridEngine
from gridcore.position import PositionState, PositionRiskManager
from gridcore.persistence import GridAnchorStore

__version__ = "0.1.0"

__all__ = [
    "Event",
    "EventType",
    "TickerEvent",
    "PublicTradeEvent",
    "ExecutionEvent",
    "OrderUpdateEvent",
    "PlaceLimitIntent",
    "CancelIntent",
    "GridConfig",
    "Grid",
    "GridEngine",
    "PositionState",
    "PositionRiskManager",
    "GridAnchorStore",
]
