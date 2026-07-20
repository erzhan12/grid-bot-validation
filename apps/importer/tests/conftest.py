"""Shared fixtures for importer tests (hermetic — no network, no real Bybit).

Helpers are exposed as fixtures returning callables — pytest runs with
``--import-mode=importlib``, so ``from conftest import ...`` is not
available (matches the repo-wide convention).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from importer.fetch_source_db import ticker_data


def _src_row(
    row_id: int,
    ts: datetime,
    symbol: str = "BTCUSDT",
    last_price: float | None = 100.0,
    **overrides,
) -> dict:
    """Build one synthetic source ticker_data row."""
    row = {
        "id": row_id,
        "symbol": symbol,
        "timestamp": ts,
        "last_price": last_price,
        "mark_price": last_price,
        "bid1_price": last_price,
        "ask1_price": last_price,
        "funding_rate": 0.0001,
    }
    row.update(overrides)
    return row


def _seed_source_db(path: Path, rows: list[dict]) -> None:
    """Create/extend a synthetic sqlite source DB with ticker_data rows."""
    engine = create_engine(f"sqlite:///{path}")
    ticker_data.metadata.create_all(engine)
    if rows:
        with engine.begin() as conn:
            conn.execute(ticker_data.insert(), rows)
    engine.dispose()


@pytest.fixture
def src_row():
    """Row-builder callable for synthetic source rows."""
    return _src_row


@pytest.fixture
def seed_source_db():
    """Callable that seeds a synthetic sqlite source DB file."""
    return _seed_source_db
