"""
Multi-tenant database layer for grid-bot-validation.

Supports SQLite (development) and PostgreSQL (production).
"""

from grid_db.settings import DatabaseSettings
from grid_db.database import DatabaseFactory, get_db, init_db
from grid_db.models import (
    Base,
    User,
    BybitAccount,
    ApiCredential,
    Strategy,
    Run,
    TickerSnapshot,
    PublicTrade,
    PrivateExecution,
    Order,
    PositionSnapshot,
    WalletSnapshot,
)
from grid_db.enums import RunType
from grid_db.repositories import (
    BaseRepository,
    UserRepository,
    BybitAccountRepository,
    ApiCredentialRepository,
    StrategyRepository,
    RunRepository,
    TickerSnapshotRepository,
    PublicTradeRepository,
    PrivateExecutionRepository,
    OrderRepository,
    PositionSnapshotRepository,
    WalletSnapshotRepository,
)

__all__ = [
    # Settings
    "DatabaseSettings",
    # Database
    "DatabaseFactory",
    "get_db",
    "init_db",
    # Models
    "Base",
    "User",
    "BybitAccount",
    "ApiCredential",
    "Strategy",
    "Run",
    "RunType",
    "TickerSnapshot",
    "PublicTrade",
    "PrivateExecution",
    "Order",
    "PositionSnapshot",
    "WalletSnapshot",
    # Repositories
    "BaseRepository",
    "UserRepository",
    "BybitAccountRepository",
    "ApiCredentialRepository",
    "StrategyRepository",
    "RunRepository",
    "TickerSnapshotRepository",
    "PublicTradeRepository",
    "PrivateExecutionRepository",
    "OrderRepository",
    "PositionSnapshotRepository",
    "WalletSnapshotRepository",
]
