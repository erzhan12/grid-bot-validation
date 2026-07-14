"""Configuration models for replay engine.

Loads replay configuration from YAML file with Pydantic validation.
"""

import os
from decimal import Decimal
from datetime import datetime, timedelta
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

    tick_size: Optional[Decimal] = Field(
        default=None,
        description="Price tick size for rounding. None = source from exchange "
        "(InstrumentInfoProvider); set = what-if override (wins on mismatch).",
    )

    # Feature 0080 (issue #183): the deterministic client_order_id is namespaced
    # by strat_id. To match orders recorded by the LIVE strategy, replay must salt
    # with the SAME strat_id. Set this to the recording's live strat_id when
    # comparing against recorded executions (blank-start OR seeded). When None,
    # falls back to seed.strat_id (if seeding), then a synthetic id.
    strat_id: Optional[str] = Field(
        default=None,
        description="Live strategy id used to namespace client_order_id hashes "
        "for comparator matching (feature 0080). None -> seed.strat_id -> synthetic.",
    )

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
    # Feature 0071 — pass-through to BacktestStrategyConfig. Defaults match
    # BacktestStrategyConfig so existing replay YAMLs keep today's behaviour.
    # Unset fields fall back to backtest defaults — populate ALL five to
    # mirror live risk-mgmt (live values diverge sharply, e.g.
    # min_total_margin 0.15 default vs ~3 live).
    min_liq_ratio: float = Field(default=0.8, description="Minimum liquidation ratio")
    max_liq_ratio: float = Field(default=1.2, description="Maximum liquidation ratio")
    min_total_margin: float = Field(
        default=0.15,
        description=(
            "Minimum total margin. Unset falls back to backtest default 0.15; "
            "populate all five risk fields to mirror live risk-mgmt."
        ),
    )
    increase_same_position_on_low_margin: bool = Field(
        default=False,
        description=(
            "When equal positions AND total_margin < min_total_margin: "
            "True = boost own side (x2), False = reduce opposite side (x0.5)"
        ),
    )
    leverage: int = Field(default=10, ge=1, le=125, description="Position leverage for liq price estimation (typically 1-125 for perpetuals)")

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

    # --- Feature 0065: non-USDT collateral re-marking ---
    collateral_coins: list[str] = Field(
        default_factory=list,
        description=(
            "Non-USDT collateral coins to re-mark over the replay window so "
            "backtest totalEquity floats like live (e.g. ['SOL']). Empty "
            "preserves USDT-only behaviour. Configure every coin ever held as "
            "collateral during the recording window."
        ),
    )
    collateral_symbol_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional override for the *USDT perp symbol used to read each "
            "coin's mark (default {coin}USDT, e.g. {'SOL': 'SOLUSDT'})."
        ),
    )
    collateral_value_ratios: dict[str, Decimal] = Field(
        default_factory=dict,
        description=(
            "Optional seed-time collateral value ratios per coin. NOT applied "
            "to total_equity in 0065 (Bybit totalEquity excludes the ratio); "
            "stored for a future margin-balance parity follow-up only."
        ),
    )
    collateral_wallet_max_staleness: timedelta = Field(
        default=timedelta(seconds=60),
        description=(
            "Max age of a per-coin wallet row for using its usdValue/balance as "
            "the seed mark; staler rows fall back to the ticker mark at at_ts."
        ),
    )

    @field_validator("collateral_coins")
    @classmethod
    def non_empty_collateral_coins(cls, v: list[str]) -> list[str]:
        """Reject empty/whitespace coin names (matches recorder.collateral_symbols)."""
        if any(not s.strip() for s in v):
            raise ValueError(
                "collateral_coins must be non-empty, non-whitespace strings"
            )
        return v

    @field_validator("collateral_value_ratios", mode="before")
    @classmethod
    def parse_collateral_value_ratios(cls, v):
        """Coerce per-coin ratio values to Decimal (string-exact, like YAML)."""
        if isinstance(v, dict):
            return {k: _parse_decimal(val) for k, val in v.items()}
        return v

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

    mode: Literal[
        "strict_cross",
        "trade_through_at_limit",
        "book_touch",
        "last_cross",
        "event_follower",
    ] = Field(
        default="last_cross",
        description=(
            "Fill simulator mode for replay. Default is 'last_cross' "
            "(feature 0051): transition-based aggressor detection that "
            "fires on last_price crossings of the limit, matching live "
            "fill timing far more closely than book_touch — the v7 "
            "A/B re-validation cut fill-timing |delta| from 19.0s "
            "(book_touch) to 5.1s (last_cross) at match_rate=100%, "
            "addressing the +12.6s lag observed in book_touch shadow "
            "runs (issue #117). Override to 'book_touch' for the "
            "legacy L1-touch behaviour, 'trade_through_at_limit' for "
            "the sticky-last-price model, or 'strict_cross' for "
            "backward-compat parity runs. 'event_follower' (feature "
            "0072) sources fills from the recorded live "
            "private_executions stream instead of simulating against "
            "the ticker: recorded exec_price/exec_qty/exec_fee/"
            "closed_pnl are applied as-is, backtest_only is "
            "structurally 0, and live_only measures intent-set "
            "divergence from live (not simulator misses)."
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
