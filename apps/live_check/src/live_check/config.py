"""Configuration models for the live_check app.

Loads live_check configuration from YAML with Pydantic validation. Each
strat entry mirrors the LIVE geometry+risk parameters (some via engine
defaults, not the live yaml) so the event_follower replay reconciles
against recorded ground truth without config-induced divergence.
"""

import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from replay.config import ReplayStrategyConfig


def _parse_decimal(v):
    """Convert string/numeric to Decimal for Pydantic field validators."""
    if isinstance(v, str):
        return Decimal(v)
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return v


class VerdictThresholds(BaseModel):
    """Pass/fail deltas for the three numeric verdict checks.

    The fourth check (matched) is structural — ``live_only == [] AND
    backtest_only == []`` — and has no numeric threshold.
    """

    realized: Decimal = Field(
        default=Decimal("0.01"),
        description="Max |replay total_realized_pnl - SUM closed_pnl|.",
    )
    commission: Decimal = Field(
        default=Decimal("0.01"),
        description="Max |replay total_commission - SUM exec_fee|.",
    )
    unrealised: Decimal = Field(
        default=Decimal("0.50"),
        description=(
            "Max |replay unrealised - recorded net-per-pair unrealised|. "
            "Mark-price snapshot-timing jitter is benign below this "
            "(established finding)."
        ),
    )

    equity: Decimal = Field(
        default=Decimal("1.00"),
        description=(
            "Max replayed-vs-recorded futures total_equity delta. Two books' "
            "last-vs-mark basis jitter sums, so the two-strategy shared-wallet "
            "floor is ~2x the single-book 0.50 band (feature 0095: after the "
            "futures-only fix the systematic ~$8.5 offset collapsed to ~0.54 "
            "transient max with ~0 final delta on run 580ca395)."
        ),
    )
    total_margin_balance: Decimal = Field(
        default=Decimal("5.00"),
        description=(
            "Max replayed-vs-recorded FUTURES margin-balance delta. Feature "
            "0095 made shared-wallet margin/mm-rate INFORMATIONAL (not gated): "
            "the recorded side is futures-equity based, not the spot-contaminated "
            "account total_margin_balance column."
        ),
    )
    account_mm_rate: Decimal = Field(
        default=Decimal("0.01"),
        description=(
            "Max replayed-vs-recorded FUTURES mm-rate ratio delta. Informational "
            "for shared-wallet (feature 0095): recorded = totalPositionMM / "
            "futures_equity, not the spot-contaminated account_mm_rate column."
        ),
    )

    @field_validator(
        "realized",
        "commission",
        "unrealised",
        "equity",
        "total_margin_balance",
        "account_mm_rate",
        mode="before",
    )
    @classmethod
    def parse_decimal_fields(cls, v):
        """Convert string/numeric to Decimal."""
        return _parse_decimal(v)


class StratCheckConfig(BaseModel):
    """Per-strat check configuration mirroring live geometry+risk.

    ``leverage``, ``enable_risk_multipliers``, ``min_liq_ratio``,
    ``max_liq_ratio`` mirror ENGINE DEFAULTS not present in
    ``conf/gridbot_test.yaml`` — the mirror is geometry+risk parity,
    not a 1:1 copy of the live yaml.
    """

    strat_id: str = Field(..., description="Live strategy id (0080 link_id salt)")
    symbol: str = Field(..., description="Trading symbol, e.g. LTCUSDT")
    tick_size: Decimal = Field(..., description="Price tick size for rounding")
    grid_count: int = Field(..., description="Total grid levels")
    grid_step: float = Field(..., gt=0, description="Grid step percentage")
    # Required: live runs x0.0005 for both sol+ltc; omitting it would fall
    # through to the ReplayStrategyConfig default x0.001 → wrong placed qty →
    # event_follower live_only/qty-excess divergence.
    amount: str = Field(
        ..., description="Order amount: fixed USDT, or 'x0.0005' wallet fraction"
    )
    min_liq_ratio: float = Field(default=0.8, description="Minimum liquidation ratio")
    max_liq_ratio: float = Field(default=1.2, description="Maximum liquidation ratio")
    min_total_margin: float = Field(..., description="Minimum total margin (live value)")
    increase_same_position_on_low_margin: bool = Field(
        default=True,
        description="Low-margin boost direction (live: true)",
    )
    leverage: int = Field(default=10, description="Position leverage")
    enable_risk_multipliers: bool = Field(
        default=True, description="Enable risk-based order size multipliers"
    )
    # Passed through to RiskConfig by replay/backtest but NOT consumed by
    # gridcore's risk-mgmt branches beyond being stored — included only to
    # mirror live config faithfully; does not affect event_follower reconcile.
    max_margin: float = Field(..., gt=0, description="Maximum margin per position")

    @field_validator("tick_size", mode="before")
    @classmethod
    def parse_decimal_fields(cls, v):
        """Convert string/numeric to Decimal."""
        return _parse_decimal(v)

    def to_replay_strategy_config(self) -> ReplayStrategyConfig:
        """Project this strat's geometry+risk into a ReplayStrategyConfig."""
        return ReplayStrategyConfig(
            tick_size=self.tick_size,
            strat_id=self.strat_id,
            grid_count=self.grid_count,
            grid_step=self.grid_step,
            amount=self.amount,
            max_margin=self.max_margin,
            enable_risk_multipliers=self.enable_risk_multipliers,
            min_liq_ratio=self.min_liq_ratio,
            max_liq_ratio=self.max_liq_ratio,
            min_total_margin=self.min_total_margin,
            increase_same_position_on_low_margin=(
                self.increase_same_position_on_low_margin
            ),
            leverage=self.leverage,
        )


class LiveCheckConfig(BaseModel):
    """Root configuration for the live_check app."""

    database_url: str = Field(
        default="sqlite:///recorder_ltcusdt_phase4.db",
        description="Live recorder SQLite database (opened READ-ONLY)",
    )
    run_id: Optional[str] = Field(
        default="580ca395",
        description="Recorder run_id; overridable via CLI, auto-discovers latest "
        "recording run when null",
    )
    strats: list[StratCheckConfig] = Field(
        ...,
        min_length=1,
        description="Strats to check (empty list would false-green a run)",
    )
    last: str = Field(default="4h", description="Default rolling window length")
    lag: str = Field(
        default="2m",
        description="Window end lag behind now (lets recorder writes settle)",
    )
    staleness_threshold: Optional[str] = Field(
        default=None,
        description="Freshness gate trip point for --watch; None derives "
        "max(2 * lag, 5 minutes)",
    )
    thresholds: VerdictThresholds = Field(
        default_factory=VerdictThresholds,
        description="Verdict pass/fail deltas",
    )


def load_config(config_path: Optional[str] = None) -> LiveCheckConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, checks:
            1. LIVE_CHECK_CONFIG_PATH environment variable
            2. conf/live_check.yaml
            3. live_check.yaml

    Returns:
        Validated LiveCheckConfig.

    Raises:
        FileNotFoundError: If no config file found.
    """
    if config_path is None:
        config_path = os.environ.get("LIVE_CHECK_CONFIG_PATH")

    if config_path is None:
        search_paths = [
            Path("conf/live_check.yaml"),
            Path("live_check.yaml"),
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Set LIVE_CHECK_CONFIG_PATH or create "
            "conf/live_check.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if data is None:
        raise ValueError(f"Empty or invalid YAML file: {config_path}")

    return LiveCheckConfig(**data)
