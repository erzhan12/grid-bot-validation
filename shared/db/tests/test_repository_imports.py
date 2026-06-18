"""Feature 0081 (issue #184): repositories.py split into a package.

Guards that every repository class is importable from BOTH the public
``grid_db`` namespace AND the ``grid_db.repositories`` package, and that the two
references are the SAME object — so the split (and any future re-export) cannot
silently drop a class or create a duplicate definition.
"""

import pytest

import grid_db
import grid_db.repositories as repos

_CLASSES = [
    "BaseRepository",
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


@pytest.mark.parametrize("name", _CLASSES)
def test_repository_importable_from_both_paths(name):
    assert hasattr(grid_db, name), f"{name} not exported from grid_db"
    assert hasattr(repos, name), f"{name} not exported from grid_db.repositories"
    assert getattr(grid_db, name) is getattr(repos, name), (
        f"{name} differs between grid_db and grid_db.repositories"
    )
