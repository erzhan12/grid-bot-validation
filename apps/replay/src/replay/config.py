"""Configuration models for replay engine.

Loads replay configuration from YAML file with Pydantic validation.
"""

import os
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

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
        description="Order amount: fixed USDT, or 'x0.001' wallet fraction",
    )
    max_margin: float = Field(default=8.0, gt=0, description="Maximum margin per position")
    early_imbalance_multiplier: float = Field(
        default=1.0,
        gt=0,
        le=100.0,
        description="Multiplier applied to next order qty when long dominates short by 1.1-10x AND both positions are pre-liquidation (liq_price==0). Inherited asymmetric trigger from bbu2 (no short-dominant mirror). Upper bound 100 also rejects float('inf') from misconfigured YAML.",
    )

    # Risk
    enable_risk_multipliers: bool = Field(
        default=True,
        description="Enable risk-based order size multipliers.",
    )

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


class SeedConfig(BaseModel):
    """Seed-aware replay configuration (feature 0029).

    When ``enabled=True`` the replay engine reconstructs live's state at
    ``at_ts`` from the recorder DB (positions, wallet, active orders) and
    the shared ``GridStateStore`` JSON file (grid level list). This closes
    the state-gap that otherwise makes replay diverge from live on the
    very first tick even when the strategy is identical.

    All four DB-backed loaders are scoped by ``run_id`` (resolved by the
    engine from ``ReplayConfig.run_id`` / auto-discovery) and
    ``account_id``; the grid loader is keyed by ``strat_id``.

    Validation: when ``enabled=True``, ``at_ts``, ``account_id``, and
    ``strat_id`` are all required. ``grid_state_path`` and ``wallet_coin``
    have safe defaults so a minimal seed YAML stays terse.

    Default ``enabled=False`` preserves the existing blank-start replay
    flow for callers that don't have recorded private-stream data.
    """

    enabled: bool = Field(
        default=False,
        description="Enable seed-from-recorder mode (feature 0029)",
    )
    at_ts: Optional[datetime] = Field(
        default=None,
        description="Moment to seed from (typically replay window start). Required when enabled.",
    )
    account_id: Optional[str] = Field(
        default=None,
        description="Account ID for run-scoped DB queries. Required when enabled.",
    )
    strat_id: Optional[str] = Field(
        default=None,
        description="Strategy identifier used as GridStateStore key. Required when enabled.",
    )
    grid_state_path: Optional[str] = Field(
        default=None,
        description=(
            "Path to legacy grid-state JSON file (feature 0021). With 0047 "
            "the engine prefers ``grid_state_snapshots`` in DB and only "
            "falls back to this file when a path is set AND no DB snapshot "
            "covers ``at_ts``. None disables the file fallback entirely."
        ),
    )
    wallet_coin: str = Field(
        default="USDT",
        description="Wallet coin to seed initial balance from.",
    )

    @model_validator(mode="after")
    def require_seed_fields_when_enabled(self):
        """Reject incomplete seed configs at load time.

        Pydantic ``Optional`` fields default to ``None`` to keep the
        ``enabled=False`` happy path terse, but with ``enabled=True`` the
        loader queries depend on all three. Failing here surfaces the
        misconfig before the engine starts a partial seed.
        """
        if self.enabled:
            missing = [
                name for name in ("at_ts", "account_id", "strat_id")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"seed.enabled=True requires fields: {', '.join(missing)}"
                )
        return self


class FillSimulatorConfig(BaseModel):
    """Replay fill simulator configuration."""

    mode: Literal["strict_cross", "trade_through_at_limit", "book_touch"] = Field(
        default="book_touch",
        description=(
            "Fill simulator mode for replay. Default is 'book_touch' because "
            "the recorder always supplies L1 bid/ask and book_touch closes "
            "the at-limit-fill gap that strict_cross misses (feature 0033 "
            "Phase 4 smoke: match_rate 91.3% -> 97.8%). Override to "
            "'strict_cross' for backward-compat parity runs."
        ),
    )


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

    seed: SeedConfig = Field(
        default_factory=SeedConfig,
        description="Seed-from-recorder configuration (feature 0029); off by default.",
    )
    fill_simulator: FillSimulatorConfig = Field(
        default_factory=FillSimulatorConfig,
        description="Fill simulator configuration.",
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
