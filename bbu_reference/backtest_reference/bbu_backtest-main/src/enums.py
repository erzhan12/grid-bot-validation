"""
Centralized Enums for the Backtest System

This module contains all enums used throughout the backtest system to avoid
repetition and ensure consistency across the codebase.
"""

from enum import Enum, StrEnum


class PositionSide(StrEnum):
    """Position side for trading (also used for orders)"""
    BUY = 'Buy'
    SELL = 'Sell'


class Direction(StrEnum):
    """Trading direction"""
    LONG = 'long'
    SHORT = 'short'


class OrderStatus(StrEnum):
    """Status of a limit order"""
    PENDING = "pending"  # Order placed, waiting for fill
    FILLED = "filled"  # Order has been executed
    CANCELLED = "cancelled"  # Order was cancelled before fill
    EXPIRED = "expired"  # Order expired due to time limits


class ChannelType(StrEnum):
    """Trading channel type"""
    LINEAR = 'linear'
    INVERSE = 'inverse'


class OrderEventType(StrEnum):
    """Order lifecycle event types"""
    CREATED = "created"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    UPDATED = "updated"


class MarginMode(StrEnum):
    """Margin mode for positions"""
    ISOLATED = "isolated"
    CROSS = "cross"


class DateConstants(Enum):
    """Date constants for database operations"""
    MIN_DATE = '1970-01-01 00:00:00'
    MAX_DATE = '2100-01-01 00:00:00'
