"""Tests for gridbot configuration module."""

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gridbot.config import (
    AccountConfig,
    StrategyConfig,
    SafetyCapsConfig,
    GridbotConfig,
    load_config,
)


class TestAccountConfig:
    """Tests for AccountConfig model."""

    def test_basic_account(self):
        """Test basic account creation."""
        account = AccountConfig(
            name="test",
            api_key="key123",
            api_secret="secret456",
        )
        assert account.name == "test"
        assert account.api_key == "key123"
        assert account.api_secret == "secret456"
        assert account.testnet is True  # default

    def test_mainnet_account(self):
        """Test account with testnet=False."""
        account = AccountConfig(
            name="prod",
            api_key="key",
            api_secret="secret",
            testnet=False,
        )
        assert account.testnet is False


class TestStrategyConfig:
    """Tests for StrategyConfig model."""

    def test_basic_strategy(self):
        """Test basic strategy creation."""
        strategy = StrategyConfig(
            strat_id="btc_main",
            account="main",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        assert strategy.strat_id == "btc_main"
        assert strategy.account == "main"
        assert strategy.symbol == "BTCUSDT"
        assert strategy.tick_size == Decimal("0.1")
        assert strategy.grid_count == 50  # default
        assert strategy.grid_step == 0.2  # default
        assert strategy.shadow_mode is False  # default

    def test_truncate_breaker_defaults(self):
        """Feature 0064 — dirty-refresh + circuit-breaker config defaults."""
        strategy = StrategyConfig(
            strat_id="btc_main",
            account="main",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        assert strategy.dirty_refresh_enabled is True
        assert strategy.dirty_rest_refresh_min_interval_seconds == 10.0
        assert strategy.truncate_breaker_max_consecutive == 3
        assert strategy.truncate_breaker_window_seconds == 60.0
        assert strategy.truncate_breaker_cooldown_seconds == 60.0
        assert strategy.truncate_breaker_reconcile is True

    def test_truncate_breaker_overrides(self):
        """Feature 0064 — config values are overridable from YAML."""
        strategy = StrategyConfig(
            strat_id="btc_main",
            account="main",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            dirty_refresh_enabled=False,
            dirty_rest_refresh_min_interval_seconds=5.0,
            truncate_breaker_max_consecutive=5,
            truncate_breaker_window_seconds=30.0,
            truncate_breaker_cooldown_seconds=120.0,
            truncate_breaker_reconcile=False,
        )
        assert strategy.dirty_refresh_enabled is False
        assert strategy.dirty_rest_refresh_min_interval_seconds == 5.0
        assert strategy.truncate_breaker_max_consecutive == 5
        assert strategy.truncate_breaker_window_seconds == 30.0
        assert strategy.truncate_breaker_cooldown_seconds == 120.0
        assert strategy.truncate_breaker_reconcile is False

    def test_truncate_breaker_invalid_values_rejected(self):
        """Feature 0064 — pydantic enforces the declared constraints (F6)."""
        base = dict(
            strat_id="btc_main", account="main", symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        with pytest.raises(ValidationError):
            StrategyConfig(**base, truncate_breaker_max_consecutive=0)  # ge=1
        with pytest.raises(ValidationError):
            StrategyConfig(**base, truncate_breaker_window_seconds=0)  # gt=0
        with pytest.raises(ValidationError):
            StrategyConfig(**base, truncate_breaker_cooldown_seconds=-1)  # gt=0
        with pytest.raises(ValidationError):
            StrategyConfig(**base, dirty_rest_refresh_min_interval_seconds=0)  # gt=0

    def test_divergence_detector_defaults(self):
        """Feature 0069 — defaults ship on and load cleanly (issue #151)."""
        strategy = StrategyConfig(
            strat_id="btc_main", account="main", symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        assert strategy.divergence_detector_enabled is True
        assert strategy.divergence_failure_mix_threshold == 10
        assert strategy.divergence_failure_mix_window_seconds == 60.0
        assert strategy.divergence_retry_budget == 5
        assert strategy.divergence_size_check_interval_seconds == 300.0
        assert strategy.divergence_size_delta_qty_step_multiplier == 5.0
        assert strategy.divergence_reconcile_min_interval_seconds == 300.0

    def test_divergence_detector_invalid_values_rejected(self):
        """Feature 0069 — counts/budgets use ge=1; intervals/windows/multiplier
        use gt=0; degenerate YAML is rejected at load."""
        base = dict(
            strat_id="btc_main", account="main", symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        with pytest.raises(ValidationError):
            StrategyConfig(**base, divergence_failure_mix_threshold=0)  # ge=1
        with pytest.raises(ValidationError):
            StrategyConfig(**base, divergence_retry_budget=0)  # ge=1
        with pytest.raises(ValidationError):
            StrategyConfig(**base, divergence_failure_mix_window_seconds=0)  # gt=0
        with pytest.raises(ValidationError):
            StrategyConfig(**base, divergence_size_check_interval_seconds=0)  # gt=0
        with pytest.raises(ValidationError):
            StrategyConfig(**base, divergence_reconcile_min_interval_seconds=0)  # gt=0
        with pytest.raises(ValidationError):
            StrategyConfig(**base, divergence_size_delta_qty_step_multiplier=0)  # gt=0

    def test_increase_same_position_on_low_margin_defaults_false(self):
        """Test low-margin equal-position boost flag defaults off."""
        strategy = StrategyConfig(
            strat_id="btc_main",
            account="main",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        assert strategy.increase_same_position_on_low_margin is False

    def test_increase_same_position_on_low_margin_explicit_true(self):
        """Test low-margin equal-position boost flag can be enabled."""
        strategy = StrategyConfig(
            strat_id="btc_main",
            account="main",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            increase_same_position_on_low_margin=True,
        )
        assert strategy.increase_same_position_on_low_margin is True

    def test_increase_same_position_on_low_margin_invalid_type(self):
        """Test non-coercible boost flag values are rejected."""
        with pytest.raises(ValidationError):
            StrategyConfig(
                strat_id="btc_main",
                account="main",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                increase_same_position_on_low_margin=[],
            )

    def test_tick_size_from_string(self):
        """Test tick_size parsed from string."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="BTCUSDT",
            tick_size="0.01",
        )
        assert strategy.tick_size == Decimal("0.01")

    def test_tick_size_absent_defaults_none(self):
        """Feature 0090: tick_size is optional (sourced from the exchange)."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="BTCUSDT",
        )
        assert strategy.tick_size is None

    def test_tick_size_from_float_is_exact_decimal(self):
        """Feature 0090: unquoted YAML float coerces via Decimal(str(v)) —
        exact-equal to Decimal("0.1"), no binary artifact that would trip the
        exchange cross-check."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="BTCUSDT",
            tick_size=0.1,
        )
        assert strategy.tick_size == Decimal("0.1")

    def test_tick_size_from_int_is_decimal(self):
        """Feature 0090: an int tick_size coerces to Decimal."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="BTCUSDT",
            tick_size=1,
        )
        assert strategy.tick_size == Decimal("1")

    def test_custom_grid_params(self):
        """Test strategy with custom grid parameters."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="ETHUSDT",
            tick_size=Decimal("0.01"),
            grid_count=100,
            grid_step=0.5,
        )
        assert strategy.grid_count == 100
        assert strategy.grid_step == 0.5

    def test_shadow_mode(self):
        """Test shadow mode configuration."""
        strategy = StrategyConfig(
            strat_id="test",
            account="test",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            shadow_mode=True,
        )
        assert strategy.shadow_mode is True

    def test_invalid_grid_count(self):
        """Test validation rejects grid_count < 4."""
        with pytest.raises(ValueError):
            StrategyConfig(
                strat_id="test",
                account="test",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                grid_count=2,
            )

    def test_invalid_grid_step(self):
        """Test validation rejects grid_step <= 0."""
        with pytest.raises(ValueError):
            StrategyConfig(
                strat_id="test",
                account="test",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                grid_step=0,
            )

    def test_legacy_long_koef_rejected_with_migration_message(self):
        """Pydantic ignores unknown fields by default — without an explicit
        guard, a config with `long_koef: 1.5` would silently load and the
        renamed `early_imbalance_multiplier` would stay at default 1.0.
        Reject explicitly with a migration message."""
        with pytest.raises(ValueError, match="renamed to 'early_imbalance_multiplier'"):
            StrategyConfig(
                strat_id="test",
                account="test",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                long_koef=1.5,
            )


class TestGridbotConfig:
    """Tests for GridbotConfig model."""

    def test_basic_config(self, sample_account_config, sample_strategy_config):
        """Test basic config creation."""
        config = GridbotConfig(
            accounts=[sample_account_config],
            strategies=[sample_strategy_config],
        )
        assert len(config.accounts) == 1
        assert len(config.strategies) == 1

    def test_account_reference_validation(self):
        """Test validation catches invalid account references."""
        account = AccountConfig(
            name="real_account",
            api_key="key",
            api_secret="secret",
        )
        strategy = StrategyConfig(
            strat_id="test",
            account="nonexistent_account",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        with pytest.raises(ValueError, match="unknown account"):
            GridbotConfig(
                accounts=[account],
                strategies=[strategy],
            )

    def test_get_account(self, sample_gridbot_config):
        """Test get_account helper."""
        account = sample_gridbot_config.get_account("test_account")
        assert account is not None
        assert account.name == "test_account"

        missing = sample_gridbot_config.get_account("nonexistent")
        assert missing is None

    def test_get_strategies_for_account(self, sample_gridbot_config):
        """Test get_strategies_for_account helper."""
        strategies = sample_gridbot_config.get_strategies_for_account("test_account")
        assert len(strategies) == 1
        assert strategies[0].strat_id == "btcusdt_test"

        empty = sample_gridbot_config.get_strategies_for_account("other")
        assert len(empty) == 0

    def test_multiple_strategies_per_account(self):
        """Test multiple strategies for same account."""
        account = AccountConfig(
            name="multi",
            api_key="key",
            api_secret="secret",
        )
        strategies = [
            StrategyConfig(
                strat_id="btc",
                account="multi",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
            ),
            StrategyConfig(
                strat_id="eth",
                account="multi",
                symbol="ETHUSDT",
                tick_size=Decimal("0.01"),
            ),
        ]
        config = GridbotConfig(accounts=[account], strategies=strategies)
        assert len(config.get_strategies_for_account("multi")) == 2

    def test_shared_symbol_rejected(self):
        """Two strategies on same (account, symbol) are still rejected.

        Feature 0080 resolved the orderLinkId-prefix-collision reason, but the
        guard stays because positionIdx/cancel-on-mismatch sharing remains a
        blocker — the message must reflect that updated rationale.
        """
        account = AccountConfig(name="acc", api_key="k", api_secret="s")
        strategies = [
            StrategyConfig(strat_id="s1", account="acc", symbol="BTCUSDT", tick_size=Decimal("0.1")),
            StrategyConfig(strat_id="s2", account="acc", symbol="BTCUSDT", tick_size=Decimal("0.1")),
        ]
        with pytest.raises(ValueError, match="share account.*symbol"):
            GridbotConfig(accounts=[account], strategies=strategies)
        # New rationale wording (feature 0080): prefix collision resolved, but
        # positionIdx sharing remains the blocker.
        with pytest.raises(ValueError, match="positionIdx"):
            GridbotConfig(accounts=[account], strategies=strategies)

    def test_same_symbol_different_accounts_ok(self):
        """Same symbol on different accounts is fine."""
        accounts = [
            AccountConfig(name="a1", api_key="k1", api_secret="s1"),
            AccountConfig(name="a2", api_key="k2", api_secret="s2"),
        ]
        strategies = [
            StrategyConfig(strat_id="s1", account="a1", symbol="BTCUSDT", tick_size=Decimal("0.1")),
            StrategyConfig(strat_id="s2", account="a2", symbol="BTCUSDT", tick_size=Decimal("0.1")),
        ]
        config = GridbotConfig(accounts=accounts, strategies=strategies)
        assert len(config.strategies) == 2

    @pytest.mark.parametrize("field", ["wallet_cache_interval", "order_sync_interval"])
    def test_negative_interval_rejected(self, field):
        """Negative interval values are rejected by validator."""
        with pytest.raises(ValueError, match="must be >= 0"):
            GridbotConfig(**{field: -1.0})

    @pytest.mark.parametrize("field", ["wallet_cache_interval", "order_sync_interval"])
    def test_zero_interval_accepted(self, field):
        """Zero is valid (disables the feature)."""
        config = GridbotConfig(**{field: 0.0})
        assert getattr(config, field) == 0.0


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_from_yaml(self):
        """Test loading config from YAML file."""
        config_data = {
            "accounts": [
                {
                    "name": "test",
                    "api_key": "key",
                    "api_secret": "secret",
                    "testnet": True,
                }
            ],
            "strategies": [
                {
                    "strat_id": "btc_test",
                    "account": "test",
                    "symbol": "BTCUSDT",
                    "tick_size": "0.1",
                    "grid_count": 50,
                    "grid_step": 0.2,
                    "increase_same_position_on_low_margin": True,
                }
            ],
            "database_url": "sqlite:///test.db",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(config_path)
            assert len(config.accounts) == 1
            assert config.accounts[0].name == "test"
            assert len(config.strategies) == 1
            assert config.strategies[0].tick_size == Decimal("0.1")
            assert config.strategies[0].increase_same_position_on_low_margin is True
            assert config.database_url == "sqlite:///test.db"
        finally:
            Path(config_path).unlink()

    def test_load_missing_file(self):
        """Test error when config file not found."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_load_invalid_yaml(self):
        """Test error when YAML is invalid."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("accounts:\n  - name: test\n    # missing required fields")
            config_path = f.name

        try:
            with pytest.raises(ValueError):
                load_config(config_path)
        finally:
            Path(config_path).unlink()


class TestSafetyCapsConfig:
    """Feature 0079 (issue #182) — production safety caps config."""

    _BASE = dict(
        strat_id="btc_main", account="main", symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
    )

    def test_safety_caps_defaults_disabled(self):
        """An existing YAML (no safety_caps block) loads with every cap disabled.

        Safe-defaults contract: enabled=True wires the machinery but every
        per-cap value is None, so upgrade is byte-for-byte the pre-0079 path
        (no order is ever rejected until the operator opts a cap in).
        """
        strategy = StrategyConfig(**self._BASE)
        caps = strategy.safety_caps
        assert isinstance(caps, SafetyCapsConfig)
        assert caps.enabled is True
        assert caps.max_notional_per_symbol is None
        assert caps.max_open_orders is None
        assert caps.session_loss_limit is None
        assert caps.session_loss_auto_reset_utc_midnight is True
        assert caps.max_orders_per_minute is None

    def test_safety_caps_explicit_values_and_decimal_coercion(self):
        """Money fields coerce str/float → Decimal; counts stay int."""
        strategy = StrategyConfig(
            **self._BASE,
            safety_caps={
                "enabled": True,
                "max_notional_per_symbol": "500.5",
                "max_open_orders": 40,
                "session_loss_limit": 25,
                "session_loss_auto_reset_utc_midnight": False,
                "max_orders_per_minute": 30,
            },
        )
        caps = strategy.safety_caps
        assert caps.max_notional_per_symbol == Decimal("500.5")
        assert isinstance(caps.max_notional_per_symbol, Decimal)
        assert caps.max_open_orders == 40
        assert caps.session_loss_limit == Decimal("25")
        assert isinstance(caps.session_loss_limit, Decimal)
        assert caps.session_loss_auto_reset_utc_midnight is False
        assert caps.max_orders_per_minute == 30

    def test_safety_caps_invalid_values_rejected(self):
        """Money caps use > 0; count caps use ge=1; degenerate YAML rejected."""
        with pytest.raises(ValidationError):
            StrategyConfig(**self._BASE, safety_caps={"max_notional_per_symbol": 0})
        with pytest.raises(ValidationError):
            StrategyConfig(**self._BASE, safety_caps={"max_notional_per_symbol": "-1"})
        with pytest.raises(ValidationError):
            StrategyConfig(**self._BASE, safety_caps={"max_open_orders": 0})  # ge=1
        with pytest.raises(ValidationError):
            StrategyConfig(**self._BASE, safety_caps={"session_loss_limit": 0})  # > 0
        with pytest.raises(ValidationError):
            StrategyConfig(**self._BASE, safety_caps={"max_orders_per_minute": 0})  # ge=1

    def test_safety_caps_loaded_from_yaml(self):
        """A safety_caps: block under a strategy round-trips through load_config."""
        yaml_text = """
accounts:
  - name: main
    api_key: k
    api_secret: s
    testnet: true
strategies:
  - strat_id: btc_main
    account: main
    symbol: BTCUSDT
    tick_size: "0.1"
    safety_caps:
      max_notional_per_symbol: "1000"
      max_open_orders: 50
      session_loss_limit: "40"
      max_orders_per_minute: 20
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_text)
            config_path = f.name
        try:
            cfg = load_config(config_path)
            caps = cfg.strategies[0].safety_caps
            assert caps.max_notional_per_symbol == Decimal("1000")
            assert caps.max_open_orders == 50
            assert caps.session_loss_limit == Decimal("40")
            assert caps.max_orders_per_minute == 20
        finally:
            Path(config_path).unlink()
