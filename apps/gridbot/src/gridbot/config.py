"""Configuration models for gridbot.

Loads trading bot configuration from YAML file with Pydantic validation.
"""

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
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
    order_sync_interval: float = Field(
        default=61.0,
        description="Seconds between periodic order reconciliation (0 to disable)",
    )
    wallet_cache_interval: float = Field(
        default=300.0,
        description="Seconds to cache wallet balance (0 to disable caching)",
    )
    rest_fetch_timeout: float = Field(
        default=10.0,
        description="Seconds to wait for REST API calls (positions, wallet balance)",
    )

    # Auth error cooldown
    auth_cooldown_minutes: int = Field(
        default=30,
        description="Minutes to wait between auth error retry cycles (per strategy)",
    )

    # Event saver
    enable_event_saver: bool = Field(
        default=False,
        description="Start embedded EventSaver for live data capture",
    )

    # Notifications
    notification: Optional[NotificationConfig] = None

    # Safety
    allow_shared_symbol: bool = Field(
        default=False,
        description=(
            "IMPORTANT: Allow multiple strategies on the same (account, symbol) pair. "
            "Since orderLinkId is not sent to Bybit, reconcile_startup assumes "
            "ALL open orders for a symbol belong to one strategy. Risk: strategies "
            "sharing a symbol will interfere with each other's orders. Do not enable "
            "unless you have a specific need and understand the order cross-contamination "
            "risk. Also ensure no manual orders are placed for symbols used by the bot. "
            "Note: During migration from orderLinkId-based tracking to orderId-based "
            "tracking, you may need to temporarily enable this flag for one restart "
            "cycle, or close all existing orders before deploying this change."
        ),
    )

    @field_validator("wallet_cache_interval", "order_sync_interval")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("must be >= 0 (use 0 to disable)")
        return v

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

    @model_validator(mode="after")
    def validate_no_shared_symbol(self):
        """Prevent multiple strategies on the same (account, symbol) unless explicitly allowed."""
        if self.allow_shared_symbol:
            return self
        seen: dict[tuple[str, str], str] = {}
        for strategy in self.strategies:
            key = (strategy.account, strategy.symbol)
            if key in seen:
                raise ValueError(
                    f"Strategies '{seen[key]}' and '{strategy.strat_id}' share "
                    f"account='{strategy.account}' symbol='{strategy.symbol}'. "
                    f"Since orderLinkId is not sent to Bybit, reconcile_startup "
                    f"assumes ALL open orders for a symbol belong to one strategy. "
                    f"Set allow_shared_symbol=true if you understand the risk."
                )
            seen[key] = strategy.strat_id
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

    load_dotenv()

    with open(path) as f:
        raw = f.read()

    # Expand ${VAR_NAME} placeholders from environment variables
    def _expand_env(match: re.Match) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(f"Environment variable '{var_name}' not set (referenced in config)")
        return value

    expanded = re.sub(r"\$\{(\w+)}", _expand_env, raw)
    data = yaml.safe_load(expanded)

    return GridbotConfig(**data)
