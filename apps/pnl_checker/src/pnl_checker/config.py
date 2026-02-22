"""Configuration models for pnl_checker.

Loads PnL checker configuration from YAML file with Pydantic validation.
"""

import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class AccountConfig(BaseModel):
    """Bybit account credentials."""

    api_key: str = Field(..., description="Bybit API key")
    api_secret: str = Field(..., description="Bybit API secret")


class SymbolConfig(BaseModel):
    """Per-symbol configuration."""

    symbol: str = Field(..., description="Trading pair (e.g., BTCUSDT)")
    tick_size: Decimal = Field(..., description="Price tick size")

    @field_validator("tick_size", mode="before")
    @classmethod
    def parse_tick_size(cls, v):
        if isinstance(v, str):
            return Decimal(v)
        return v


class RiskParamsConfig(BaseModel):
    """Risk management parameters for position.py validation."""

    min_liq_ratio: float = Field(default=0.8, description="Minimum liquidation ratio")
    max_liq_ratio: float = Field(default=1.2, description="Maximum liquidation ratio")
    max_margin: float = Field(default=8.0, gt=0, description="Maximum margin per position")
    min_total_margin: float = Field(default=0.15, description="Minimum total margin")


class PnlCheckerConfig(BaseModel):
    """Root configuration for pnl_checker."""

    account: AccountConfig
    symbols: list[SymbolConfig] = Field(..., min_length=1)
    risk_params: RiskParamsConfig = Field(default_factory=RiskParamsConfig)
    tolerance: float = Field(default=0.01, ge=0, description="USDT tolerance for pass/fail")
    funding_max_pages: int = Field(default=20, gt=0, description="Max pages for funding tx log pagination")


def load_config(config_path: Optional[str] = None) -> PnlCheckerConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, checks:
            1. PNL_CHECKER_CONFIG_PATH environment variable
            2. conf/pnl_checker.yaml

    Returns:
        Validated PnlCheckerConfig

    Raises:
        FileNotFoundError: If no config file found
        ValueError: If config validation fails
    """
    if config_path is None:
        config_path = os.environ.get("PNL_CHECKER_CONFIG_PATH")

    if config_path is None:
        search_paths = [
            Path("conf/pnl_checker.yaml"),
            Path("pnl_checker.yaml"),
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Set PNL_CHECKER_CONFIG_PATH or create conf/pnl_checker.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return PnlCheckerConfig(**data)
