"""Comparator configuration."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class ComparatorConfig(BaseModel):
    """Configuration for backtest-vs-live comparison."""

    run_id: str
    database_url: str = "sqlite:///gridbot.db"
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None
    symbol: Optional[str] = None
    output_dir: str = "results/comparison"
    price_tolerance: Decimal = Decimal("0")
    qty_tolerance: Decimal = Decimal("0.001")
