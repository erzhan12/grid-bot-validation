"""Configuration models for shared-wallet multi-strategy replay."""

import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from backtest.config import WindDownMode
from replay.config import FillSimulatorConfig, ReplayStrategyConfig, _parse_decimal


class MultiReplayStrategyConfig(ReplayStrategyConfig):
    """One strategy entry for shared-wallet replay."""

    strat_id: str = Field(
        ...,
        description="Live strategy id used for client_order_id namespace.",
    )
    symbol: str = Field(..., description="Trading symbol, e.g. SOLUSDT")


class MultiSeedConfig(BaseModel):
    """Account-level seed configuration for multi-strategy replay."""

    enabled: bool = Field(default=False, description="Enable DB seed loading.")
    at_ts: Optional[datetime] = Field(
        default=None,
        description="Account seed timestamp. Required when enabled.",
    )
    account_id: Optional[str] = Field(
        default=None,
        description="Account ID for account-level seed rows. Required when enabled.",
    )
    wallet_coin: str = Field(default="USDT", description="Wallet coin to seed.")
    collateral_coins: list[str] = Field(default_factory=list)
    collateral_symbol_map: dict[str, str] = Field(default_factory=dict)
    collateral_value_ratios: dict[str, Decimal] = Field(default_factory=dict)
    collateral_wallet_max_staleness: timedelta = Field(
        default=timedelta(seconds=60)
    )

    @field_validator("collateral_coins")
    @classmethod
    def non_empty_collateral_coins(cls, v: list[str]) -> list[str]:
        """Reject empty/whitespace coin names."""
        if any(not s.strip() for s in v):
            raise ValueError(
                "collateral_coins must be non-empty, non-whitespace strings"
            )
        return v

    @field_validator("collateral_value_ratios", mode="before")
    @classmethod
    def parse_collateral_value_ratios(cls, v):
        """Coerce collateral value ratios to Decimal."""
        if isinstance(v, dict):
            return {k: _parse_decimal(val) for k, val in v.items()}
        return v

    @model_validator(mode="after")
    def require_seed_fields_when_enabled(self):
        """Require account seed identity fields when seeding is enabled."""
        if self.enabled:
            missing = [
                name for name in ("at_ts", "account_id")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"seed.enabled=True requires fields: {', '.join(missing)}"
                )
        return self


class MultiReplayConfig(BaseModel):
    """Root config for shared-wallet multi-strategy replay."""

    database_url: str = Field(default="sqlite:///recorder.db")
    run_id: Optional[str] = Field(default=None)
    start_ts: Optional[datetime] = Field(default=None)
    end_ts: Optional[datetime] = Field(default=None)
    seed: MultiSeedConfig = Field(default_factory=MultiSeedConfig)
    fill_simulator: FillSimulatorConfig = Field(default_factory=FillSimulatorConfig)
    strategies: list[MultiReplayStrategyConfig] = Field(..., min_length=1)
    initial_balance: Decimal = Field(default=Decimal("10000"), gt=0)
    enable_funding: bool = Field(default=True)
    funding_rate: Decimal = Field(default=Decimal("0.0001"))
    wind_down_mode: WindDownMode = Field(default=WindDownMode.LEAVE_OPEN)
    output_dir: str = Field(default="results/replay_multi")
    price_tolerance: Decimal = Field(default=Decimal("0"))
    qty_tolerance: Decimal = Field(default=Decimal("0.001"))

    @field_validator(
        "initial_balance", "funding_rate", "price_tolerance", "qty_tolerance",
        mode="before",
    )
    @classmethod
    def parse_decimal_fields(cls, v):
        """Convert string/numeric config values to Decimal."""
        return _parse_decimal(v)

    @model_validator(mode="after")
    def reject_duplicate_strategies(self):
        """Reject duplicate symbol / strat_id across strategies.

        The engine keys bundles/runners/seed_data by symbol, so a duplicate
        symbol would silently last-win and drop a strategy; duplicate strat_id
        would collide the client_order_id namespace.
        """
        symbols = [s.symbol for s in self.strategies]
        strat_ids = [s.strat_id for s in self.strategies]
        dup_symbols = sorted({s for s in symbols if symbols.count(s) > 1})
        dup_strats = sorted({s for s in strat_ids if strat_ids.count(s) > 1})
        if dup_symbols:
            raise ValueError(f"duplicate strategy symbol(s): {dup_symbols}")
        if dup_strats:
            raise ValueError(f"duplicate strategy strat_id(s): {dup_strats}")
        return self

    @model_validator(mode="after")
    def reject_traded_symbol_as_collateral(self):
        """Prevent double-counting traded base coins as spot collateral."""
        traded_bases = {_base_coin(s.symbol) for s in self.strategies}
        overlap = traded_bases & {coin.upper() for coin in self.seed.collateral_coins}
        if overlap:
            raise ValueError(
                "seed.collateral_coins must not include traded base coins: "
                f"{sorted(overlap)}"
            )
        return self


def _base_coin(symbol: str) -> str:
    """Return a simple linear-contract base coin for known quote suffixes."""
    upper = symbol.upper()
    for quote in ("USDT", "USDC", "USD"):
        if upper.endswith(quote):
            return upper[: -len(quote)]
    return upper


def load_multi_config(config_path: Optional[str] = None) -> MultiReplayConfig:
    """Load shared-wallet replay config from YAML.

    Args:
        config_path: Explicit path. If None, checks
            ``REPLAY_MULTI_CONFIG_PATH`` then ``conf/replay_multi.yaml``.

    Returns:
        Validated multi replay config.

    Raises:
        FileNotFoundError: If no config file exists.
        ValueError: If the YAML is empty or invalid.
    """
    if config_path is None:
        config_path = os.environ.get("REPLAY_MULTI_CONFIG_PATH")

    if config_path is None:
        path = Path("conf/replay_multi.yaml")
        if path.exists():
            config_path = str(path)

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Set REPLAY_MULTI_CONFIG_PATH or create "
            "conf/replay_multi.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if data is None:
        raise ValueError(f"Empty or invalid YAML file: {config_path}")

    return MultiReplayConfig(**data)
