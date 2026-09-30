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
    GridStateSnapshot,
    PrivateStreamGap,
    PrivateStreamSession,
)
from grid_db.enums import RecoveryStatus, RunType
from grid_db.data_quality import RecordedDataQualityError
from grid_db.identity import (
    UUID_NAMESPACE,
    account_id_for,
    strategy_id_for,
    user_id_for,
)
from grid_db.utils import redact_db_url
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
    PrivateStreamGapRepository,
    PrivateStreamSessionRepository,
    PositionSnapshotRepository,
    WalletSnapshotRepository,
    GridStateSnapshotRepository,
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
    "GridStateSnapshot",
    "PrivateStreamGap",
    "PrivateStreamSession",
    "RecoveryStatus",
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
    "PrivateStreamGapRepository",
    "PrivateStreamSessionRepository",
    "PositionSnapshotRepository",
    "WalletSnapshotRepository",
    "GridStateSnapshotRepository",
    # Identity
    "UUID_NAMESPACE",
    "account_id_for",
    "strategy_id_for",
    "user_id_for",
    # Utils
    "redact_db_url",
    # Data quality
    "RecordedDataQualityError",
]
