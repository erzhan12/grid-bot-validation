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
    PublicTrade,
    PrivateExecution,
)
from grid_db.repositories import (
    BaseRepository,
    UserRepository,
    BybitAccountRepository,
    ApiCredentialRepository,
    StrategyRepository,
    RunRepository,
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
    "PublicTrade",
    "PrivateExecution",
    # Repositories
    "BaseRepository",
    "UserRepository",
    "BybitAccountRepository",
    "ApiCredentialRepository",
    "StrategyRepository",
    "RunRepository",
]
