"""Configuration models for gridbot.

Loads trading bot configuration from YAML file with Pydantic validation.
"""

import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class AccountConfig(BaseModel):
    """Exchange account configuration."""

    name: str = Field(..., description="Unique account identifier")
    api_key: str = Field(..., description="Bybit API key")
    api_secret: str = Field(..., description="Bybit API secret")
    testnet: bool = Field(default=True, description="Use testnet endpoints")


class StrategyConfig(BaseModel):
    """Grid trading strategy configuration."""

    strat_id: str = Field(..., description="Unique strategy identifier")
    account: str = Field(..., description="Account name reference")
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
    max_margin: float = Field(default=8.0, gt=0, description="Maximum margin per position")
    long_koef: float = Field(default=1.0, gt=0, description="Long/short bias multiplier")

    # Risk parameters (for Position)
    min_liq_ratio: float = Field(default=0.8, description="Minimum liquidation ratio")
    max_liq_ratio: float = Field(default=1.2, description="Maximum liquidation ratio")
    min_total_margin: float = Field(default=0.15, description="Minimum total margin")

    # Mode
    shadow_mode: bool = Field(default=False, description="Log intents without executing")

    @field_validator("tick_size", mode="before")
    @classmethod
    def parse_tick_size(cls, v):
        """Convert string tick_size to Decimal."""
        if isinstance(v, str):
            return Decimal(v)
        return v


class TelegramConfig(BaseModel):
    """Telegram notification configuration."""

    bot_token: str = Field(..., description="Telegram bot token")
    chat_id: str = Field(..., description="Telegram chat ID for alerts")


class NotificationConfig(BaseModel):
    """Notification configuration."""

    telegram: Optional[TelegramConfig] = None


class GridbotConfig(BaseModel):
    """Root configuration for gridbot."""

    accounts: list[AccountConfig] = Field(default_factory=list)
    strategies: list[StrategyConfig] = Field(default_factory=list)

    # Database
    database_url: str = Field(
        default="sqlite:///gridbot.db",
        description="Database connection URL",
    )

    # Timing
    position_check_interval: float = Field(
        default=63.0,
        description="Seconds between position checks",
    )

    # Notifications
    notification: Optional[NotificationConfig] = None

    @model_validator(mode="after")
    def validate_account_references(self):
        """Ensure all strategy account references exist."""
        account_names = {acc.name for acc in self.accounts}
        for strategy in self.strategies:
            if strategy.account not in account_names:
                raise ValueError(
                    f"Strategy '{strategy.strat_id}' references unknown account '{strategy.account}'"
                )
        return self

    def get_account(self, name: str) -> Optional[AccountConfig]:
        """Get account config by name."""
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return None

    def get_strategies_for_account(self, account_name: str) -> list[StrategyConfig]:
        """Get all strategies for an account."""
        return [s for s in self.strategies if s.account == account_name]


def load_config(config_path: Optional[str] = None) -> GridbotConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, checks:
            1. GRIDBOT_CONFIG_PATH environment variable
            2. conf/gridbot.yaml
            3. gridbot.yaml

    Returns:
        Validated GridbotConfig

    Raises:
        FileNotFoundError: If no config file found
        ValueError: If config validation fails
    """
    if config_path is None:
        config_path = os.environ.get("GRIDBOT_CONFIG_PATH")

    if config_path is None:
        # Search default locations
        search_paths = [
            Path("conf/gridbot.yaml"),
            Path("gridbot.yaml"),
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Set GRIDBOT_CONFIG_PATH or create conf/gridbot.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return GridbotConfig(**data)
