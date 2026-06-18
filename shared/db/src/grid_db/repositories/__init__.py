"""Repository pattern for database operations with multi-tenant filtering.

Feature 0081 (issue #184): the former flat ``repositories.py`` module is now a
package of domain-grouped modules (``base``, ``identity``, ``market_data``,
``execution``, ``snapshots``). Every repository class is re-exported here so
both ``from grid_db import XRepository`` and
``from grid_db.repositories import XRepository`` keep resolving unchanged.
"""

from grid_db.repositories.base import BaseRepository, T
from grid_db.repositories.identity import (
    ApiCredentialRepository,
    BybitAccountRepository,
    RunRepository,
    StrategyRepository,
    UserRepository,
)
from grid_db.repositories.market_data import (
    PublicTradeRepository,
    TickerSnapshotRepository,
)
from grid_db.repositories.execution import (
    OrderRepository,
    PrivateExecutionRepository,
)
from grid_db.repositories.snapshots import (
    GridStateSnapshotRepository,
    PositionSnapshotRepository,
    WalletSnapshotRepository,
)

__all__ = [
    "BaseRepository",
    "T",
    "UserRepository",
    "BybitAccountRepository",
    "ApiCredentialRepository",
    "StrategyRepository",
    "RunRepository",
    "PublicTradeRepository",
    "TickerSnapshotRepository",
    "PrivateExecutionRepository",
    "OrderRepository",
    "PositionSnapshotRepository",
    "WalletSnapshotRepository",
    "GridStateSnapshotRepository",
]
