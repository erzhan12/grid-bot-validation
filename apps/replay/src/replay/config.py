"""Configuration models for replay engine.

Loads replay configuration from YAML file with Pydantic validation.
"""

import os
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from backtest.config import WindDownMode


def _parse_decimal(v):
    """Convert string/numeric to Decimal for Pydantic field validators."""
    if isinstance(v, str):
        return Decimal(v)
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return v


class ReplayStrategyConfig(BaseModel):
    """Grid strategy configuration for replay simulation."""

    tick_size: Decimal = Field(..., description="Price tick size for rounding")

    # Grid parameters
    grid_count: int = Field(default=50, ge=4, description="Total grid levels")
    grid_step: float = Field(default=0.2, gt=0, description="Grid step percentage")

    # Position sizing
    amount: str = Field(
        default="x0.001",
        description="Order amount: fixed USDT, 'x0.001' wallet fraction, 'b0.001' BTC equivalent",
    )
    max_margin: float = Field(default=8.0, gt=0, description="Maximum margin per position")
    long_koef: float = Field(default=1.0, gt=0, description="Long/short bias multiplier")

    # Commission
    commission_rate: Decimal = Field(
        default=Decimal("0.0002"),
        description="Commission rate per trade (0.0002 = 0.02% maker fee)",
    )

    @field_validator("tick_size", "commission_rate", mode="before")
    @classmethod
    def parse_decimal_fields(cls, v):
        """Convert string/numeric to Decimal."""
        return _parse_decimal(v)


class ReplayConfig(BaseModel):
    """Root configuration for replay engine."""

    database_url: str = Field(
        default="sqlite:///recorder.db",
        description="Path to recorder SQLite database",
    )

    run_id: Optional[str] = Field(
        default=None,
        description="Recorder run_id for ground-truth executions; auto-discovers latest if omitted",
    )

    symbol: str = Field(..., description="Symbol to replay (e.g., BTCUSDT)")

    start_ts: Optional[datetime] = Field(
        default=None,
        description="Replay start (defaults to run's start_ts)",
    )
    end_ts: Optional[datetime] = Field(
        default=None,
        description="Replay end (defaults to run's end_ts)",
    )

    strategy: ReplayStrategyConfig = Field(
        ..., description="Grid strategy configuration"
    )

    # Backtest parameters
    initial_balance: Decimal = Field(
        default=Decimal("10000"),
        gt=0,
        description="Initial wallet balance in USDT",
    )
    enable_funding: bool = Field(
        default=True,
        description="Enable funding payment simulation",
    )
    funding_rate: Decimal = Field(
        default=Decimal("0.0001"),
        description="Default funding rate (0.0001 = 0.01%)",
    )
    wind_down_mode: WindDownMode = Field(
        default=WindDownMode.LEAVE_OPEN,
        description="What to do with positions at end",
    )

    # Comparison parameters
    output_dir: str = Field(
        default="results/replay",
        description="Output directory for comparison reports",
    )
    price_tolerance: Decimal = Field(
        default=Decimal("0"),
        description="Price tolerance for breach detection",
    )
    qty_tolerance: Decimal = Field(
        default=Decimal("0.001"),
        description="Quantity tolerance for breach detection",
    )

    @field_validator(
        "initial_balance", "funding_rate", "price_tolerance", "qty_tolerance",
        mode="before",
    )
    @classmethod
    def parse_decimal_fields(cls, v):
        """Convert string/numeric to Decimal."""
        return _parse_decimal(v)


def load_config(config_path: Optional[str] = None) -> ReplayConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, checks:
            1. REPLAY_CONFIG_PATH environment variable
            2. conf/replay.yaml
            3. replay.yaml

    Returns:
        Validated ReplayConfig.

    Raises:
        FileNotFoundError: If no config file found.
    """
    if config_path is None:
        config_path = os.environ.get("REPLAY_CONFIG_PATH")

    if config_path is None:
        search_paths = [
            Path("conf/replay.yaml"),
            Path("replay.yaml"),
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Set REPLAY_CONFIG_PATH or create conf/replay.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return ReplayConfig(**data)
