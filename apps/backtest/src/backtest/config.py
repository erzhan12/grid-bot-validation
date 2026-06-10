"""Configuration models for backtest.

Loads backtest configuration from YAML file with Pydantic validation.
"""

import os
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


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
        description="Order amount: fixed USDT, or 'x0.001' for wallet fraction",
    )
    max_margin: float = Field(default=8.0, gt=0, description="Maximum margin per position")
    early_imbalance_multiplier: float = Field(
        default=1.0,
        gt=0,
        le=100.0,
        description="Multiplier applied to next order qty when long dominates short by 1.1-10x AND both positions are pre-liquidation (liq_price==0). Inherited asymmetric trigger from bbu2 (no short-dominant mirror). Upper bound 100 also rejects float('inf') from misconfigured YAML.",
    )

    # Risk parameters (for Position)
    min_liq_ratio: float = Field(default=0.8, description="Minimum liquidation ratio")
    max_liq_ratio: float = Field(default=1.2, description="Maximum liquidation ratio")
    min_total_margin: float = Field(default=0.15, description="Minimum total margin")
    increase_same_position_on_low_margin: bool = Field(
        default=False,
        description=(
            "When equal positions AND total_margin < min_total_margin: "
            "True = boost own side (x2), False = reduce opposite side (x0.5)"
        ),
    )
    leverage: int = Field(default=10, ge=1, le=125, description="Position leverage for liq price estimation (typically 1-125 for perpetuals)")
    maintenance_margin_rate: float = Field(
        default=0.005, ge=0, description="Maintenance margin rate for liq price estimation"
    )
    enable_risk_multipliers: bool = Field(
        default=True, description="Enable risk-based order size multipliers (A/B toggle)"
    )
    risk_limits_cache_path: Optional[str] = Field(
        default=None, description="Path to risk_limits_cache.json for tiered MMR (None = auto-discover conf/risk_limits_cache.json, then hardcoded defaults)"
    )

    # Commission
    commission_rate: Decimal = Field(
        default=Decimal("0.0002"),
        description="Commission rate per trade (0.0002 = 0.02% maker fee)",
    )

    # Feature 0045: taker fee + hedge buffer for IM/MM parity with Bybit UTA.
    # Bybit publishes positionIM / positionMM INCLUDING the estimated
    # fee-to-close (per "Initial Margin USDT Contract" and "Maintenance
    # Margin USDT Contract" help-center articles). Backtest must replicate
    # that fee to match live snapshots.
    taker_fee_rate: Decimal = Field(
        default=Decimal("0.00075"),
        description="Taker fee rate used in Bybit's fee-to-close component of "
                    "positionIM/positionMM (default 0.075% = Bybit USDT-Perp "
                    "non-VIP taker). Account-specific; for VIP tiers reduce "
                    "accordingly.",
    )
    hedge_smaller_buffer_factor: Decimal = Field(
        default=Decimal("5.657"),
        description="Empirical Bybit hedge-mode buffer factor C used by the "
                    "smaller leg in 0045 helper: "
                    "buffer = MMR * hedged_size * |L_entry - S_entry| * C. "
                    "Derived from LTCUSDT live data at 10x leverage; needs "
                    "per-symbol re-calibration. C's closed form is not yet "
                    "documented by Bybit.",
    )

    @field_validator("tick_size", mode="before")
    @classmethod
    def parse_tick_size(cls, v):
        """Convert string tick_size to Decimal."""
        if isinstance(v, str):
            return Decimal(v)
        return v

    @model_validator(mode="before")
    @classmethod
    def reject_renamed_long_koef(cls, data):
        """Catch legacy `long_koef` config name and force migration.

        Pydantic ignores unknown fields by default, so a config with
        `long_koef: 1.5` would silently load and the renamed
        `early_imbalance_multiplier` would stay at default 1.0 — the user
        would believe the multiplier is active when it is not. Reject
        explicitly with a migration message instead of silent acceptance.
        Renamed in feature 0028 (D-3) for clearer semantics.
        """
        if isinstance(data, dict) and "long_koef" in data:
            raise ValueError(
                "Config field 'long_koef' was renamed to "
                "'early_imbalance_multiplier' in feature 0028. The semantic "
                "is unchanged (multiplier on next-order qty when long "
                "dominates short by 1.1-10x AND both positions pre-"
                "liquidation). Rename the field in your YAML."
            )
        return data

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
