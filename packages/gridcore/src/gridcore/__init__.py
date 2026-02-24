"""
gridcore - Pure grid trading strategy logic with zero exchange dependencies.

This package contains the core grid trading strategy implementation extracted
from bbu2-master, designed to be usable by both live trading and backtesting
applications.
"""

from gridcore.events import Event, EventType, TickerEvent, PublicTradeEvent, ExecutionEvent, OrderUpdateEvent
from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridcore.config import GridConfig
from gridcore.grid import Grid, GridSideType
from gridcore.engine import GridEngine
from gridcore.position import PositionState, Position, PositionRiskManager, RiskConfig, DirectionType, SideType
from gridcore.persistence import GridAnchorStore
from gridcore.pnl import (
    calc_unrealised_pnl,
    calc_unrealised_pnl_pct,
    calc_position_value,
    calc_initial_margin,
    calc_liq_ratio,
)

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
    "GridSideType",
    "GridEngine",
    "PositionState",
    "Position",
    "PositionRiskManager",  # Alias for Position (backward compatibility)
    "RiskConfig",
    "DirectionType",
    "SideType",
    "GridAnchorStore",
    "calc_unrealised_pnl",
    "calc_unrealised_pnl_pct",
    "calc_position_value",
    "calc_initial_margin",
    "calc_liq_ratio",
]
