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


class SafetyCapsConfig(BaseModel):
    """Feature 0079 (issue #182) — production safety caps.

    Hard, last-resort caps enforced OUTSIDE strategy logic (in
    ``StrategyRunner`` / ``IntentExecutor``), additive to and independent of
    ``min_liq_ratio`` / ``max_liq_ratio`` / the low-balance preflight. Every
    per-cap value defaults to ``None`` (that cap disabled), so an existing
    deployment that has no ``safety_caps:`` block sees NO behavioral change on
    upgrade — no order is ever rejected until the operator opts a cap in.
    ``enabled`` is the master kill-switch: when False every cap is inert.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Master kill-switch. When False every cap is inert (no state, no "
            "checks, no logs) — the byte-for-byte pre-0079 path. When True the "
            "machinery is wired but each still-None cap remains disabled."
        ),
    )
    max_notional_per_symbol: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description=(
            "C1 — max live total position notional per symbol in USDT "
            "(long.position_value + short.position_value). When the cap is "
            "reached the runner suppresses new OPEN place intents; reduce-only "
            "closes always pass. None disables C1."
        ),
    )
    max_open_orders: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "C2 — max tracked (placed) open orders per strat. A pure count "
            "limit: rejects BOTH open and reduce-only place intents at/above "
            "the cap. None disables C2."
        ),
    )
    session_loss_limit: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description=(
            "C3 — session realized-loss circuit breaker, a POSITIVE USDT "
            "magnitude (tripped when session realized PnL <= -value). On trip "
            "the runner cancels working orders once and then suppresses ALL new "
            "place intents until recovery. None disables C3."
        ),
    )
    session_loss_auto_reset_utc_midnight: bool = Field(
        default=True,
        description=(
            "C3 recovery mode. True = the loss latch auto-clears on the first "
            "position update of the next UTC calendar date. False = stay "
            "latched until process restart."
        ),
    )
    max_orders_per_minute: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "C4 — max accepted real order submissions in any trailing 60s, "
            "enforced at IntentExecutor.execute_place (the single live-submit "
            "choke point, so retry-queue re-dispatch is rate-limited too). "
            "Shadow placements do not consume the window. None disables C4."
        ),
    )

    @field_validator(
        "max_notional_per_symbol", "session_loss_limit", mode="before"
    )
    @classmethod
    def _coerce_decimal(cls, v):
        """Coerce str/int/float money fields to Decimal (mirrors tick_size)."""
        if v is None:
            return v
        if isinstance(v, (str, int, float)):
            return Decimal(str(v))
        return v


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

    # Feature 0064 — 110017 retry-storm self-heal + circuit-breaker (issue #149).
    # All default-on so existing conf/*.yaml keep working unchanged.
    dirty_refresh_enabled: bool = Field(
        default=True,
        description=(
            "Master kill-switch for the dirty-mirror REST refresh-before-guard. "
            "When True, a direction flagged dirty by a prior 110017 has its "
            "position size REST-refreshed before _is_good_to_place so the guard "
            "rejects oversized reduce-only closes against fresh data."
        ),
    )
    dirty_rest_refresh_min_interval_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Minimum seconds between REST get_positions calls per direction while "
            "that direction is dirty — bounds REST when the guard keeps rejecting "
            "per-ticker re-emissions."
        ),
    )
    truncate_breaker_max_consecutive: int = Field(
        default=3,
        ge=1,
        description="Trip the 110017 circuit-breaker after this many within the window.",
    )
    truncate_breaker_window_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Sliding window for counting consecutive 110017 errors per scope key.",
    )
    truncate_breaker_cooldown_seconds: float = Field(
        default=60.0,
        gt=0,
        description="After a trip, drop further intents on that scope key for this long.",
    )
    truncate_breaker_reconcile: bool = Field(
        default=True,
        description="Trigger one forced position+order reconcile when the breaker trips.",
    )

    # Feature 0066 — 110007 low-balance preflight + retry-queue guard (issue #159).
    # All default-on so existing conf/*.yaml keep working unchanged.
    preflight_balance_check_enabled: bool = Field(
        default=True,
        description=(
            "Master kill-switch for the low-balance preflight. When True, an "
            "OPEN (non-reduce-only) order is rejected locally in "
            "_is_good_to_place when est_cost = qty*price/leverage exceeds "
            "available_balance*(1+buffer) — preventing the 110007 retry storm. "
            "Reduce-only orders always bypass the check (they free margin)."
        ),
    )
    preflight_balance_buffer: float = Field(
        default=0.05,
        ge=0,
        description=(
            "Fractional safety margin on the preflight est_cost so fee/funding/"
            "rounding slack near the affordability boundary does not still 110007."
        ),
    )
    assumed_leverage: float = Field(
        default=1.0,
        gt=0,
        description=(
            "Fallback leverage for the preflight est_cost when no live exchange "
            "leverage has been observed for the direction. Bias LOW: an "
            "under-estimate only over-rejects affordable opens, never lets an "
            "unaffordable one through."
        ),
    )
    # Low-balance predicate (shared by the moderate_liq_risk fix + chase-close).
    low_balance_fraction: float = Field(
        default=0.10,
        gt=0,
        description=(
            "Low-balance is True when available_balance is positive but below "
            "total_position_value * this fraction. Drives the moderate_liq_risk "
            "fix (3a) and chase-close (3b) — feature 0066 / issue #159."
        ),
    )
    moderate_liq_low_balance_fix_enabled: bool = Field(
        default=True,
        description=(
            "Kill-switch for the moderate_liq_risk low-balance fix. When True and "
            "low-balance, the moderate_liq_risk arm SKIPS the 0.5 close-throttle "
            "so margin-freeing closes are not slowed (the issue #159 deadlock). "
            "When False the arm is byte-for-byte the pre-0066 behavior."
        ),
    )
    # Chase-close active defense (feature 0066 / issue #159). Default OFF — it
    # actively places orders; promote to on only after the regression suite plus
    # one live low-balance stress event confirms the dominant side decreases.
    chase_close_enabled: bool = Field(
        default=False,
        description=(
            "Kill-switch for chase-close. When True AND low-balance AND the "
            "position_ratio is extreme, the bot cancels resting grow-side opens "
            "for the dominant side and places a reduce-only post-only close near "
            "the touch to trim it without market-order slippage."
        ),
    )
    chase_position_ratio_threshold: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Chase enters when low-balance and position_ratio > this (long "
            "dominant) or < 1/this (short dominant)."
        ),
    )
    chase_offset_pct: float = Field(
        default=0.0007,
        gt=0,
        description=(
            "Chase order offset from the touch (maker-safe: Sell above / Buy "
            "below). Issue #159 suggested the 0.0005–0.0010 band."
        ),
    )
    chase_replace_drift_pct: float = Field(
        default=0.0010,
        gt=0,
        description="Cancel-replace the chase order when price drifts more than this from it.",
    )
    chase_close_hysteresis: float = Field(
        default=0.1,
        ge=0,
        description=(
            "Ratio re-entry margin so the chase exits only after the imbalance "
            "recovers past the threshold by this fraction (anti-flap)."
        ),
    )

    # Feature 0067 — suppress LowBalanceSkip log spam (issue #164). Both
    # default-on and kill-switchable; with BOTH False the preflight emits the
    # per-intent DEBUG line exactly as today (byte-for-byte current behavior).
    low_balance_skip_transition_logs_enabled: bool = Field(
        default=True,
        description=(
            "Master kill-switch for LowBalanceSkip state-transition logging. "
            "When True, the per-intent DEBUG line is suppressed and the "
            "sustained-skip regime is logged only at its edges (ENTER/EXIT INFO) "
            "per (direction, side). When False, the per-intent DEBUG fires on "
            "every rejection (current behavior). The accept/reject decision is "
            "never changed — only what is logged."
        ),
    )
    low_balance_skip_exit_idle_seconds: int = Field(
        default=60,
        ge=0,
        description=(
            "Idle-timeout for the reconcile sweep that EXITs an active "
            "LowBalanceSkip key after this many seconds with no fresh blocks "
            "(resolves a key removed from the grid mid-storm without a recovery "
            "EXIT). 0 disables the sweep (pure event-driven EXIT). Far longer "
            "than the ~100ms inter-block gap during a live storm, so it never "
            "false-fires mid-storm."
        ),
    )
    low_balance_skip_summary_enabled: bool = Field(
        default=True,
        description=(
            "Kill-switch for the periodic INFO summary of LowBalanceSkip "
            "activity. Flushes on the dispatch cadence (no asyncio task); "
            "increments independently of the transition flag so the summary "
            "works even when transition logging is off."
        ),
    )
    low_balance_skip_summary_interval_sec: int = Field(
        default=60,
        gt=0,
        description="Window length (seconds) for the periodic LowBalanceSkip summary.",
    )

    # Feature 0069 — auto state-divergence detector + on-demand forced reconcile
    # (issue #151). Four signals (placement-failure union / retry-budget /
    # REST-vs-local size delta / post-WS-recovery) converge on one forced
    # reconcile, throttled SEPARATELY from the 0064 breaker cooldown. All
    # default-on so existing conf/*.yaml keep working unchanged.
    divergence_detector_enabled: bool = Field(
        default=True,
        description=(
            "Master kill-switch for the state-divergence detector. When False "
            "the detector is fully inert: no signal records, no extra REST, no "
            "forced reconcile, no WARNING. The 0064 breaker is unaffected."
        ),
    )
    divergence_failure_mix_threshold: int = Field(
        default=10,
        ge=1,
        description=(
            "Signal 1: fire when this many placement failures whose error is in "
            "the UNION of {110017, 110072, network} occur within "
            "divergence_failure_mix_window_seconds on one strat (110007 is "
            "EXCLUDED — it is an intentional low-balance drop, not divergence)."
        ),
    )
    divergence_failure_mix_window_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Signal 1: rolling window (seconds) for the placement-failure UNION count.",
    )
    divergence_retry_budget: int = Field(
        default=5,
        ge=1,
        description=(
            "Signal 2: fire once per new edge when truncate_breaker_reconcile_count "
            "reaches this many breaker-driven reconciles (backstop for the cases "
            "the breaker counts but does not auto-reconcile)."
        ),
    )
    divergence_size_check_interval_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Signal 3: cadence (seconds) of the read-only REST-vs-local "
            "position-size delta sweep. gt=0 (cannot be disabled): position size "
            "always has a periodic backstop, which is why a throttle-suppressed "
            "signal-4 reconcile can be safely dropped. NOTE: a single "
            "orchestrator-level gate drives the sweep for ALL strats at the "
            "MINIMUM interval across enabled strats — a strat configured with a "
            "longer interval is swept more often (harmless: the per-runner read "
            "is cheap and idempotent)."
        ),
    )
    divergence_size_delta_qty_step_multiplier: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Signal 3: fire when abs(rest_size - local_size) exceeds "
            "qty_step * this multiplier for either direction."
        ),
    )
    divergence_reconcile_min_interval_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Detector throttle: minimum seconds between forced reconciles per "
            "strat triggered by ANY divergence signal. SEPARATE from the 0064 "
            "breaker cooldown (truncate_breaker_cooldown_seconds)."
        ),
    )

    # Feature 0079 (issue #182) — production safety caps (exposure / order /
    # loss / rate limits) enforced outside strategy logic. default_factory so
    # existing YAMLs load unchanged and get the all-disabled SafetyCapsConfig.
    safety_caps: SafetyCapsConfig = Field(default_factory=SafetyCapsConfig)

    # Mode
    shadow_mode: bool = Field(default=False, description="Log intents without executing")

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
        description=(
            "Minimum seconds between blocking REST position fetches of the "
            "same account (floor). The steady-state rotation scheduler uses "
            "max(position_check_interval, N_accounts * _POSITION_TICK_BASE) "
            "as the actual per-account floor, so this is a lower bound."
        ),
    )
    order_sync_interval: float = Field(
        default=61.0,
        description="Seconds between periodic order reconciliation (0 to disable)",
    )
    wallet_cache_interval: float = Field(
        default=300.0,
        description="Seconds to cache wallet balance (0 to disable caching)",
    )
    # Feature 0066 (issue #159) — real-time wallet via the WS `wallet` topic.
    wallet_ws_enabled: bool = Field(
        default=True,
        description=(
            "Phase-4 master kill-switch for the real-time wallet WS feed. When "
            "True, the private WS subscribes the `wallet` topic and the runner's "
            "preflight reads a non-blocking, age-bounded peek of free margin. "
            "When False, BOTH the WS subscription AND the runner wallet_provider "
            "are skipped — the preflight reverts to the pre-Phase-4 "
            "position-cadence `_available_balance` path (no age-bounding)."
        ),
    )
    wallet_ws_max_age_seconds: float = Field(
        default=45.0,
        gt=0,
        description=(
            "Max age of a WS/REST wallet snapshot the preflight will trust "
            "before failing open. Bounds how long a silently-dead WS may pin a "
            "stale slot; well under wallet_cache_interval (300s). A quiet-but-"
            "healthy WS falling back to REST is harmless (an unchanging balance "
            "is still accurate)."
        ),
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

    # Notifications
    notification: Optional[NotificationConfig] = None

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
        """Reject multiple strategies on the same (account, symbol) pair.

        Even though orderLinkId IS sent to Bybit (deterministic prefix +
        millis suffix post-2026-05-08), it cannot disambiguate two
        strategies on the same (account, symbol). The deterministic prefix
        is a SHA of (symbol, side, price, direction) — both strategies
        would compute the SAME prefix for the same logical order, and the
        wire-form suffix only differs across re-placements, not across
        strategies. So at runtime there is no way to tell which strategy
        placed a given open order, and two strategies on the same pair
        would cancel each other's orders on every tick via the engine's
        cancel-on-mismatch pass (gridcore/engine.py:_place_grid_orders).

        bbu2 makes this configuration unrepresentable by construction: its
        amounts[].strat field is a scalar pointing at a single pair_timeframes
        entry, and each pair_timeframe has a single symbol, so the bad config
        cannot be written. Our schema is more flexible, so we reconstruct
        the same invariant as a pydantic validator. There is no escape hatch —
        if you need a second strategy on the same symbol, use a different
        account.
        """
        seen: dict[tuple[str, str], str] = {}
        for strategy in self.strategies:
            key = (strategy.account, strategy.symbol)
            if key in seen:
                raise ValueError(
                    f"Strategies '{seen[key]}' and '{strategy.strat_id}' share "
                    f"account='{strategy.account}' symbol='{strategy.symbol}'. "
                    f"This is not allowed: the deterministic client_order_id "
                    f"prefix is a SHA of (symbol, side, price, direction), so "
                    f"two strategies on the same (account, symbol) compute the "
                    f"same orderLinkId prefix and cannot be distinguished at "
                    f"runtime — they would cancel each other's orders every "
                    f"tick. bbu2 makes this unrepresentable by construction; "
                    f"here we reject it at config load. Use a different "
                    f"account for the second strategy."
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
