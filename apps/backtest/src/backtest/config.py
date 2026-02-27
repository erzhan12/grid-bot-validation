"""Configuration models for backtest.

Loads backtest configuration from YAML file with Pydantic validation.
"""

import os
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class WindDownMode(StrEnum):
    """Wind-down mode at end of backtest."""

    LEAVE_OPEN = "leave_open"
    CLOSE_ALL = "close_all"


class BacktestStrategyConfig(BaseModel):
    """Grid trading strategy configuration for backtest."""

    strat_id: str = Field(..., description="Unique strategy identifier")
    symbol: str = Field(..., description="Trading pair (e.g., BTCUSDT)")
    tick_size: Decimal = Field(..., description="Price tick size for rounding")

    # Grid parameters
    grid_count: int = Field(default=50, ge=4, description="Total grid levels")
    grid_step: float = Field(default=0.2, gt=0, description="Grid step percentage")

    # Position sizing
    amount: str = Field(
        default="x0.001",
        description="Order amount: fixed USDT, 'x0.001' for wallet fraction, 'b0.001' for BTC equivalent",
    )
    leverage: int = Field(default=10, ge=1, description="Position leverage for margin calculations")
    max_margin: float = Field(default=8.0, gt=0, description="Maximum margin per position")
    long_koef: float = Field(default=1.0, gt=0, description="Long/short bias multiplier")

    # Risk parameters (for Position)
    min_liq_ratio: float = Field(default=0.8, description="Minimum liquidation ratio")
    max_liq_ratio: float = Field(default=1.2, description="Maximum liquidation ratio")
    min_total_margin: float = Field(default=0.15, description="Minimum total margin")

    # Commission
    commission_rate: Decimal = Field(
        default=Decimal("0.0002"),
        description="Commission rate per trade (0.0002 = 0.02% maker fee)",
    )

    @field_validator("tick_size", mode="before")
    @classmethod
    def parse_tick_size(cls, v):
        """Convert string tick_size to Decimal."""
        if isinstance(v, str):
            return Decimal(v)
        return v

    @field_validator("commission_rate", mode="before")
    @classmethod
    def parse_commission_rate(cls, v):
        """Convert string commission_rate to Decimal."""
        if isinstance(v, str):
            return Decimal(v)
        return v


class BacktestConfig(BaseModel):
    """Root configuration for backtest."""

    strategies: list[BacktestStrategyConfig] = Field(default_factory=list)

    # Database
    database_url: str = Field(
        default="sqlite:///gridbot.db",
        description="Database connection URL",
    )

    # Initial balance
    initial_balance: Decimal = Field(
        default=Decimal("10000"),
        gt=0,
        description="Initial wallet balance in USDT",
    )

    # Funding simulation
    enable_funding: bool = Field(
        default=True,
        description="Enable funding payment simulation",
    )
    funding_rate: Decimal = Field(
        default=Decimal("0.0001"),
        description="Default funding rate (0.0001 = 0.01%)",
    )

    # End-of-backtest handling
    wind_down_mode: WindDownMode = Field(
        default=WindDownMode.LEAVE_OPEN,
        description="What to do with positions at end",
    )

    # Instrument info cache
    instrument_cache_ttl_hours: int = Field(
        default=24,
        gt=0,
        description="Hours before instrument cache is refreshed from API",
    )

    @field_validator("initial_balance", mode="before")
    @classmethod
    def parse_initial_balance(cls, v):
        """Convert string initial_balance to Decimal."""
        if isinstance(v, str):
            return Decimal(v)
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        return v

    @field_validator("funding_rate", mode="before")
    @classmethod
    def parse_funding_rate(cls, v):
        """Convert string funding_rate to Decimal."""
        if isinstance(v, str):
            return Decimal(v)
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        return v

    def get_strategy(self, strat_id: str) -> Optional[BacktestStrategyConfig]:
        """Get strategy config by ID."""
        return next((s for s in self.strategies if s.strat_id == strat_id), None)

    def get_strategies_for_symbol(self, symbol: str) -> list[BacktestStrategyConfig]:
        """Get all strategies for a symbol."""
        return [s for s in self.strategies if s.symbol == symbol]


def load_config(config_path: Optional[str] = None) -> BacktestConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, checks:
            1. BACKTEST_CONFIG_PATH environment variable
            2. conf/backtest.yaml
            3. backtest.yaml

    Returns:
        Validated BacktestConfig

    Raises:
        FileNotFoundError: If no config file found
        ValueError: If config validation fails
    """
    if config_path is None:
        config_path = os.environ.get("BACKTEST_CONFIG_PATH")

    if config_path is None:
        # Search default locations
        search_paths = [
            Path("conf/backtest.yaml"),
            Path("backtest.yaml"),
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Set BACKTEST_CONFIG_PATH or create conf/backtest.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return BacktestConfig(**data)
