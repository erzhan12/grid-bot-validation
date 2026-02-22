"""Configuration models for pnl_checker.

Loads PnL checker configuration from YAML file with Pydantic validation.
"""

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class AccountConfig(BaseModel):
    """Bybit account credentials.

    Values can be provided via YAML config or environment variables.
    Env vars (BYBIT_API_KEY, BYBIT_API_SECRET) take precedence over
    config file values when set.
    """

    api_key: str = Field(default="", description="Bybit API key")
    api_secret: str = Field(default="", description="Bybit API secret")

    @model_validator(mode="after")
    def apply_env_overrides(self):
        """Override credentials with env vars when available."""
        env_key = os.environ.get("BYBIT_API_KEY")
        if env_key:
            self.api_key = env_key
        env_secret = os.environ.get("BYBIT_API_SECRET")
        if env_secret:
            self.api_secret = env_secret
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "API credentials required. Set BYBIT_API_KEY/BYBIT_API_SECRET "
                "env vars or provide api_key/api_secret in config file."
            )
        return self


_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{4,20}$")


class SymbolConfig(BaseModel):
    """Per-symbol configuration."""

    symbol: str = Field(..., description="Trading pair (e.g., BTCUSDT)")
    tick_size: Decimal = Field(..., description="Price tick size")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        if not _SYMBOL_PATTERN.match(v):
            raise ValueError(
                f"Invalid symbol format '{v}'. "
                "Expected uppercase alphanumeric, 4-20 chars (e.g., BTCUSDT)."
            )
        return v

    @field_validator("tick_size", mode="before")
    @classmethod
    def parse_tick_size(cls, v):
        value = Decimal(v) if isinstance(v, str) else v
        if value <= 0:
            raise ValueError("tick_size must be positive")
        return value


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
