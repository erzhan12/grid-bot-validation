"""Tests for backtest runner."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from gridcore import TickerEvent, EventType, PlaceLimitIntent, DirectionType, SideType
from gridcore.instrument_info import InstrumentInfo
from gridcore.pnl import calc_maintenance_margin

from backtest.runner import BacktestRunner
from backtest.config import BacktestStrategyConfig
from backtest.fill_simulator import FillMode, TradeThroughFillSimulator
from backtest.order_manager import BacktestOrderManager
from backtest.executor import BacktestExecutor
from backtest.session import BacktestSession


class TestBacktestRunner:
    """Tests for BacktestRunner."""

    @pytest.fixture
    def runner(self, sample_strategy_config, session):
        """Create a backtest runner with a simple qty calculator."""
        fill_simulator = TradeThroughFillSimulator()
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=sample_strategy_config.commission_rate,
        )

        # Fixed 100 USDT per order (GridEngine emits qty=0, so we need a calculator)
        def qty_from_usdt(intent, wallet_balance):
            if intent.price <= 0:
                return Decimal("0")
            return Decimal("100") / intent.price

        executor = BacktestExecutor(order_manager=order_manager, qty_calculator=qty_from_usdt)

        return BacktestRunner(
            strategy_config=sample_strategy_config,
            executor=executor,
            session=session,
        )

    def test_init(self, runner, sample_strategy_config):
        """Runner initializes correctly."""
        assert runner.strat_id == sample_strategy_config.strat_id
        assert runner.symbol == sample_strategy_config.symbol
        assert runner.engine is not None
        assert runner.long_tracker is not None
        assert runner.short_tracker is not None

    def test_process_tick_builds_grid(self, runner, sample_ticker_event):
        """First tick builds the grid."""
        intents = runner.process_tick(sample_ticker_event)

        # Should have generated place intents for grid
        assert len(intents) > 0
        assert runner._grid_built is True

    def test_process_tick_fills_order(self, runner, sample_timestamp):
        """Order fills when price crosses."""
        # First tick builds grid at 100000
        tick1 = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=Decimal("100000"),
            mark_price=Decimal("100000"),
            bid1_price=Decimal("99999"),
            ask1_price=Decimal("100001"),
            funding_rate=Decimal("0.0001"),
        )
        runner.process_tick(tick1)

        # Get order prices — grid must have produced buy orders
        limit_orders = runner.order_manager.get_limit_orders()
        assert len(limit_orders["long"]) > 0, "Grid should produce buy orders"
        buy_price = Decimal(limit_orders["long"][0]["price"])

        # Second tick drops below buy price
        tick2 = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=buy_price - Decimal("100"),
            mark_price=buy_price - Decimal("100"),
            bid1_price=buy_price - Decimal("101"),
            ask1_price=buy_price - Decimal("99"),
            funding_rate=Decimal("0.0001"),
        )
        runner.process_tick(tick2)

        # Should have recorded a trade
        assert len(runner._session.trades) >= 1

    def test_book_touch_fills_on_ask_touch_without_last_penetration(
        self,
        sample_strategy_config,
        session,
        sample_timestamp,
    ):
        """Runner passes full ticker data to the fill simulator."""
        fill_simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=sample_strategy_config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_manager, qty_calculator=None)
        runner = BacktestRunner(
            strategy_config=sample_strategy_config,
            executor=executor,
            session=session,
        )
        order_manager.place_order(
            client_order_id="book-touch-buy",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("58.60"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )
        tick = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=Decimal("58.61"),
            mark_price=Decimal("58.61"),
            bid1_price=Decimal("58.59"),
            ask1_price=Decimal("58.60"),
            funding_rate=Decimal("0"),
        )

        post_fill_intents = runner.process_fills(tick)

        assert len(session.trades) == 1
        assert session.trades[0].price == Decimal("58.60")
        assert order_manager.total_active_orders == 0
        assert post_fill_intents == []

    def test_apply_funding(self, runner, sample_ticker_event):
        """Funding is applied to positions."""
        # Build grid first
        runner.process_tick(sample_ticker_event)

        # Manually set a position (normally would be from fills)
        runner._long_tracker.process_fill(
            side="Buy",
            qty=Decimal("0.1"),
            price=Decimal("100000"),
        )

        # Apply funding
        funding = runner.apply_funding(Decimal("0.0001"), Decimal("100000"))

        # Long pays when rate > 0
        # Position value = 0.1 * 100000 = 10000
        # Funding = 10000 * 0.0001 = 1
        assert funding == Decimal("-1")

    def test_get_total_pnl(self, runner, sample_ticker_event):
        """Total PnL from both trackers."""
        runner.process_tick(sample_ticker_event)

        # Simulate some PnL (set directly for testing)
        runner._long_tracker.state.realized_pnl = Decimal("100")
        runner._short_tracker.state.realized_pnl = Decimal("50")

        total = runner.get_total_pnl()

        # Total should include both directions
        # (When set directly, no commission is deducted)
        assert total == Decimal("150")


class TestBacktestRunnerRiskMultipliers:
    """Tests for risk multiplier integration in BacktestRunner."""

    @pytest.fixture
    def risk_config(self):
        """Strategy config with risk multipliers enabled."""
        return BacktestStrategyConfig(
            strat_id="test_btc_risk",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=50,
            grid_step=0.2,
            amount="x0.001",
            max_margin=8.0,
            commission_rate=Decimal("0.0002"),
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            min_total_margin=0.15,
            leverage=10,
            maintenance_margin_rate=0.005,
            enable_risk_multipliers=True,
        )

    @pytest.fixture
    def no_risk_config(self):
        """Strategy config with risk multipliers disabled."""
        return BacktestStrategyConfig(
            strat_id="test_btc_norisk",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=50,
            grid_step=0.2,
            amount="x0.001",
            max_margin=8.0,
            commission_rate=Decimal("0.0002"),
            enable_risk_multipliers=False,
        )

    @pytest.fixture
    def risk_runner(self, risk_config):
        """Runner with risk multipliers enabled."""
        session = BacktestSession(session_id="test_risk", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=risk_config.commission_rate,
        )

        def qty_from_usdt(intent, wallet_balance):
            if intent.price <= 0:
                return Decimal("0")
            return Decimal("100") / intent.price

        executor = BacktestExecutor(order_manager=order_mgr, qty_calculator=qty_from_usdt)
        return BacktestRunner(
            strategy_config=risk_config,
            executor=executor,
            session=session,
        )

    @pytest.fixture
    def no_risk_runner(self, no_risk_config):
        """Runner with risk multipliers disabled."""
        session = BacktestSession(session_id="test_norisk", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=no_risk_config.commission_rate,
        )

        def qty_from_usdt(intent, wallet_balance):
            if intent.price <= 0:
                return Decimal("0")
            return Decimal("100") / intent.price

        executor = BacktestExecutor(order_manager=order_mgr, qty_calculator=qty_from_usdt)
        return BacktestRunner(
            strategy_config=no_risk_config,
            executor=executor,
            session=session,
        )

    def test_risk_enabled_creates_position_pair(self, risk_runner):
        """Risk-enabled runner creates linked Position objects."""
        assert risk_runner._long_position is not None
        assert risk_runner._short_position is not None
        assert risk_runner._long_position._opposite is risk_runner._short_position
        assert risk_runner._short_position._opposite is risk_runner._long_position

    def test_risk_disabled_no_position_pair(self, no_risk_runner):
        """Risk-disabled runner has no Position objects."""
        assert no_risk_runner._long_position is None
        assert no_risk_runner._short_position is None

    def test_risk_enabled_wires_qty_calculator(self, risk_runner):
        """Risk-enabled runner wires qty_calculator to executor."""
        assert risk_runner._executor.qty_calculator is not None

    def test_risk_disabled_no_risk_wrapper(self, no_risk_runner):
        """Risk-disabled runner keeps base qty_calculator without risk wrapper."""
        assert no_risk_runner._executor.qty_calculator is not None
        assert not hasattr(no_risk_runner, "_base_qty_calculator")

    def test_get_amount_multiplier_default(self, risk_runner):
        """Default multipliers are 1.0 before any fills."""
        assert risk_runner.get_amount_multiplier(DirectionType.LONG, SideType.BUY) == 1.0
        assert risk_runner.get_amount_multiplier(DirectionType.LONG, SideType.SELL) == 1.0
        assert risk_runner.get_amount_multiplier(DirectionType.SHORT, SideType.BUY) == 1.0
        assert risk_runner.get_amount_multiplier(DirectionType.SHORT, SideType.SELL) == 1.0

    def test_get_amount_multiplier_disabled_always_1(self, no_risk_runner):
        """Disabled risk always returns 1.0."""
        assert no_risk_runner.get_amount_multiplier(DirectionType.LONG, SideType.BUY) == 1.0
        assert no_risk_runner.get_amount_multiplier(DirectionType.SHORT, SideType.SELL) == 1.0

    # 0043: per-leg `_estimate_liquidation_price` removed; the pair function
    # `_estimate_pair_liq_prices` is the single source of truth. The single-leg
    # tests below feed an empty opposite leg so the pair formula collapses to
    # the per-leg result, and the hedge-scenario tests live in TestPairLiqHedge
    # below.

    def _make_state(self, size: Decimal, entry: Decimal) -> SimpleNamespace:
        """Lightweight tracker-state stand-in for pair-formula unit tests."""
        return SimpleNamespace(size=size, avg_entry_price=entry)

    def test_pair_liq_long_only(self, risk_runner):
        """Single long leg: pair formula collapses to per-leg long behaviour."""
        long_state = self._make_state(Decimal("0.1"), Decimal("100000"))
        short_state = self._make_state(Decimal("0"), Decimal("0"))
        equity = Decimal("10000")
        liq_long, liq_short = risk_runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )
        pv = Decimal("10000")
        qty = Decimal("0.1")
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected = Decimal("100000") - (equity - mm) / qty
        assert liq_long == max(expected, Decimal("0"))
        assert liq_short == Decimal("0")

    def test_pair_liq_short_only(self, risk_runner):
        """Single short leg: pair formula collapses to per-leg short behaviour."""
        long_state = self._make_state(Decimal("0"), Decimal("0"))
        short_state = self._make_state(Decimal("0.1"), Decimal("100000"))
        equity = Decimal("10000")
        liq_long, liq_short = risk_runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )
        pv = Decimal("10000")
        qty = Decimal("0.1")
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected = Decimal("100000") + (equity - mm) / qty
        assert liq_long == Decimal("0")
        assert liq_short == expected

    def test_pair_liq_short_scales_with_equity(self, risk_runner):
        """0042+0043: larger total_equity raises short liq linearly with 1/qty."""
        long_state = self._make_state(Decimal("0"), Decimal("0"))
        short_state = self._make_state(Decimal("10"), Decimal("100"))
        low_eq = Decimal("50")
        high_eq = Decimal("100")
        _, low_liq = risk_runner._estimate_pair_liq_prices(
            long_state, short_state, low_eq,
        )
        _, high_liq = risk_runner._estimate_pair_liq_prices(
            long_state, short_state, high_eq,
        )
        # Δliq = Δequity / qty (mm cancels because position is identical).
        assert high_liq - low_liq == (high_eq - low_eq) / Decimal("10")

    def test_pair_liq_tiered_long_large_position(self, risk_runner):
        """Large long uses tiered MM via combined notional; below-entry liq."""
        long_state = self._make_state(Decimal("50"), Decimal("100000"))
        short_state = self._make_state(Decimal("0"), Decimal("0"))
        equity = Decimal("600000")
        liq_long, _ = risk_runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )
        pv = Decimal("5000000")
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected = Decimal("100000") - (equity - mm) / Decimal("50")
        assert liq_long == expected
        assert liq_long > 0
        assert liq_long < Decimal("100000")

    def test_pair_liq_falls_back_to_flat_mmr(self):
        """When no tiers loaded, pair formula uses flat maintenance_margin_rate."""
        from backtest.session import BacktestSession
        config = BacktestStrategyConfig(
            strat_id="test_flat",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            maintenance_margin_rate=0.008,
            enable_risk_multipliers=False,
        )
        session = BacktestSession(session_id="test_flat", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim, commission_rate=config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_mgr)
        runner = BacktestRunner(strategy_config=config, executor=executor, session=session)
        runner._mm_tiers = None  # force flat-MMR path

        long_state = SimpleNamespace(size=Decimal("0.1"), avg_entry_price=Decimal("100000"))
        short_state = SimpleNamespace(size=Decimal("0"), avg_entry_price=Decimal("0"))
        equity = Decimal("10000")
        liq_long, _ = runner._estimate_pair_liq_prices(long_state, short_state, equity)

        pv = Decimal("10000")
        mm = pv * Decimal("0.008")
        expected = Decimal("100000") - (equity - mm) / Decimal("0.1")
        assert liq_long == expected

    def test_build_position_state_uses_tiered_liq(self, risk_runner):
        """0043: _build_position_state plumbs the passed liq through to PositionState.

        Liq computation moved to _estimate_pair_liq_prices (called by
        _update_risk_multipliers). _build_position_state itself now just
        forwards the value, so this test asserts the plumbing.
        """
        risk_runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("50"), price=Decimal("100000")
        )
        wallet = Decimal("600000")
        explicit_liq = Decimal("99500.5")
        state = risk_runner._build_position_state(
            risk_runner._long_tracker, wallet, DirectionType.LONG, explicit_liq
        )
        assert state.liquidation_price == explicit_liq

    def test_load_mm_tiers_from_cache_file(self, tmp_path):
        """Cache file with valid tiers is loaded correctly."""
        import json
        cache = {
            "BTCUSDT": {
                "tiers": [
                    {"max_value": "1000000", "mmr_rate": "0.005", "deduction": "0"},
                    {"max_value": "Infinity", "mmr_rate": "0.02", "deduction": "5000"},
                ],
                "cached_at": "2026-01-01T00:00:00Z",
            }
        }
        cache_file = tmp_path / "risk_cache.json"
        cache_file.write_text(json.dumps(cache))

        tiers = BacktestRunner._load_mm_tiers("BTCUSDT", str(cache_file))
        assert len(tiers) == 2
        assert tiers[0][1] == Decimal("0.005")
        assert tiers[1][0] == Decimal("Infinity")

    def test_load_mm_tiers_missing_symbol_falls_back(self, tmp_path):
        """Cache file exists but missing symbol falls back to hardcoded."""
        import json
        cache = {"ETHUSDT": {"tiers": [{"max_value": "Infinity", "mmr_rate": "0.01", "deduction": "0"}]}}
        cache_file = tmp_path / "risk_cache.json"
        cache_file.write_text(json.dumps(cache))

        tiers = BacktestRunner._load_mm_tiers("BTCUSDT", str(cache_file))
        # Should fall back to hardcoded BTCUSDT (7 tiers)
        assert len(tiers) == 7

    def test_load_mm_tiers_malformed_file_falls_back(self, tmp_path):
        """Malformed JSON falls back to hardcoded tiers."""
        cache_file = tmp_path / "bad_cache.json"
        cache_file.write_text("{invalid json")

        tiers = BacktestRunner._load_mm_tiers("BTCUSDT", str(cache_file))
        assert len(tiers) == 7  # hardcoded BTCUSDT

    def test_load_mm_tiers_invalid_numeric_falls_back(self, tmp_path):
        """Invalid numeric values in cache fall back to hardcoded tiers."""
        import json
        cache = {
            "BTCUSDT": {
                "tiers": [
                    {"max_value": "1000000", "mmr_rate": "not_a_number", "deduction": "0"},
                ]
            }
        }
        cache_file = tmp_path / "bad_numeric.json"
        cache_file.write_text(json.dumps(cache))

        tiers = BacktestRunner._load_mm_tiers("BTCUSDT", str(cache_file))
        assert len(tiers) == 7  # hardcoded BTCUSDT fallback

    def test_load_mm_tiers_null_values_falls_back(self, tmp_path):
        """Null tier values in cache fall back to hardcoded tiers."""
        import json
        cache = {
            "BTCUSDT": {
                "tiers": [
                    {"max_value": "1000000", "mmr_rate": None, "deduction": "0"},
                ]
            }
        }
        cache_file = tmp_path / "null_cache.json"
        cache_file.write_text(json.dumps(cache))

        tiers = BacktestRunner._load_mm_tiers("BTCUSDT", str(cache_file))
        assert len(tiers) == 7  # hardcoded BTCUSDT fallback

    def test_build_position_state_empty(self, risk_runner):
        """Empty tracker produces zero-state PositionState."""
        state = risk_runner._build_position_state(
            risk_runner._long_tracker, Decimal("10000"), DirectionType.LONG,
            Decimal("0"),
        )
        assert state.size == Decimal("0")
        assert state.margin == Decimal("0")
        assert state.liquidation_price == Decimal("0")

    def test_build_position_state_with_position(self, risk_runner):
        """Tracker with position produces correct PositionState; liq plumbed."""
        risk_runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.1"), price=Decimal("100000")
        )

        wallet = Decimal("10000")
        explicit_liq = Decimal("90123.45")
        state = risk_runner._build_position_state(
            risk_runner._long_tracker, wallet, DirectionType.LONG, explicit_liq
        )
        assert state.size == Decimal("0.1")
        assert state.entry_price == Decimal("100000")
        # position_value = 0.1 * 100000 = 10000, margin = 10000/10000 = 1.0
        assert state.margin == Decimal("1")
        assert state.position_value == Decimal("10000")
        # 0043: liq comes straight from the caller, not from per-leg formula.
        assert state.liquidation_price == explicit_liq
        assert state.leverage == 10

    def test_build_position_state_zero_wallet_with_position_raises(self, risk_runner):
        """Zero wallet balance with non-zero position raises ValueError."""
        # Manually add a position
        risk_runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.1"), price=Decimal("100000")
        )

        # Should raise ValueError when wallet is zero but position exists
        with pytest.raises(ValueError, match="wallet_balance is zero"):
            risk_runner._build_position_state(
                risk_runner._long_tracker, Decimal("0"), DirectionType.LONG,
                Decimal("0"),
            )

    def test_multiplier_updates_after_fill(self, risk_runner):
        """Risk multipliers recalculate after a fill via _process_fill path."""
        # Manually add a large long position to trigger risk rules
        # margin = position_value / wallet = (1.0 * 100000) / 10000 = 10.0
        # This is a huge margin, so position_ratio and total_margin will be large
        risk_runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("1.0"), price=Decimal("100000")
        )
        risk_runner._update_risk_multipliers(100000.0)

        # With only a long position and no short, position_ratio is very high
        # The exact multiplier depends on risk rules, but they should have been calculated
        long_mult = risk_runner._long_position.get_amount_multiplier()
        assert "Buy" in long_mult
        assert "Sell" in long_mult

    def test_apply_risk_to_qty_with_base_calculator(self, risk_runner):
        """Risk callback composes with base qty_calculator."""
        # Wire a base calculator that computes qty from wallet fraction
        def base_calc(intent, wallet_balance):
            return wallet_balance * Decimal("0.001") / intent.price

        risk_runner._base_qty_calculator = base_calc
        risk_runner._long_position.set_amount_multiplier(SideType.BUY, 2.0)

        # intent.qty=0 like real GridEngine intents
        intent = PlaceLimitIntent(
            symbol="BTCUSDT",
            side=SideType.BUY,
            price=Decimal("100000"),
            qty=Decimal("0"),
            direction=DirectionType.LONG,
            grid_level=5,
            reduce_only=False,
            client_order_id="test_buy_001",
        )

        # base_qty = 10000 * 0.001 / 100000 = 0.0001, then * 2.0 = 0.0002
        result_qty = risk_runner._apply_risk_to_qty(intent, Decimal("10000"))
        assert result_qty == Decimal("0.0002")

    def test_apply_risk_to_qty_no_base_calculator(self, risk_runner):
        """Without base calculator, falls back to intent.qty."""
        risk_runner._base_qty_calculator = None
        risk_runner._long_position.set_amount_multiplier(SideType.BUY, 0.5)

        intent = PlaceLimitIntent(
            symbol="BTCUSDT",
            side=SideType.BUY,
            price=Decimal("99000"),
            qty=Decimal("0.01"),
            direction=DirectionType.LONG,
            grid_level=5,
            reduce_only=False,
            client_order_id="test_buy_002",
        )

        result_qty = risk_runner._apply_risk_to_qty(intent, Decimal("10000"))
        assert result_qty == Decimal("0.005")

    def test_apply_risk_re_rounds_to_qty_step(self, risk_config):
        """Risk multiplier result is re-rounded to instrument qty_step.

        Regression: base_qty=0.001 (rounded) * multiplier=0.5 = 0.0005,
        which is not a valid qty_step=0.001. Must re-round to 0.001.
        """
        session = BacktestSession(session_id="test_round", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=risk_config.commission_rate,
        )
        instrument = InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("100"),
        )

        def base_calc(intent, wallet_balance):
            return Decimal("0.001")  # Already rounded to qty_step

        executor = BacktestExecutor(order_manager=order_mgr, qty_calculator=base_calc)
        runner = BacktestRunner(
            strategy_config=risk_config,
            executor=executor,
            session=session,
            instrument_info=instrument,
        )
        runner._long_position.set_amount_multiplier(SideType.BUY, 0.5)

        intent = PlaceLimitIntent(
            symbol="BTCUSDT",
            side=SideType.BUY,
            price=Decimal("100000"),
            qty=Decimal("0"),
            direction=DirectionType.LONG,
            grid_level=5,
            reduce_only=False,
            client_order_id="test_reround",
        )

        # Without re-rounding: 0.001 * 0.5 = 0.0005 (invalid)
        # With re-rounding: round_qty(0.0005, step=0.001) = 0.001
        result_qty = runner._apply_risk_to_qty(intent, Decimal("10000"))
        assert result_qty == Decimal("0.001")

    def test_config_new_fields_defaults(self):
        """New config fields have correct defaults."""
        config = BacktestStrategyConfig(
            strat_id="test",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
        )
        assert config.leverage == 10
        assert config.maintenance_margin_rate == 0.005
        assert config.enable_risk_multipliers is True

    def test_config_new_fields_custom(self):
        """New config fields accept custom values."""
        config = BacktestStrategyConfig(
            strat_id="test",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            leverage=20,
            maintenance_margin_rate=0.01,
            enable_risk_multipliers=False,
        )
        assert config.leverage == 20
        assert config.maintenance_margin_rate == 0.01
        assert config.enable_risk_multipliers is False

    def test_risk_recalculation_uses_market_price(self, risk_runner, sample_timestamp):
        """P1: _process_fill uses ticker last_price, not fill price."""
        # Set last_price via process_fills (simulating a tick)
        tick = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=Decimal("95000"),
            mark_price=Decimal("95000"),
            bid1_price=Decimal("94999"),
            ask1_price=Decimal("95001"),
            funding_rate=Decimal("0.0001"),
        )
        risk_runner.process_fills(tick)
        assert risk_runner._last_price == Decimal("95000")

        # Manually add position and call _update_risk_multipliers
        # to verify it would be called with last_price, not fill price
        risk_runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.1"), price=Decimal("100000")
        )

        # Capture what price _update_risk_multipliers receives
        called_with = []
        original = risk_runner._update_risk_multipliers

        def capture_price(price, **kwargs):
            called_with.append(price)
            return original(price, **kwargs)

        risk_runner._update_risk_multipliers = capture_price

        # Simulate a fill event at a different price (100000) than market (95000)
        from gridcore import ExecutionEvent
        fill_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            exec_id="fill_001",
            order_id="ord_001",
            order_link_id="link_001",
            side=SideType.BUY,
            price=Decimal("100000"),
            qty=Decimal("0.05"),
            fee=Decimal("1.0"),
        )

        # Place a matching order so _process_fill can find it
        risk_runner._executor.order_manager.place_order(
            client_order_id="link_001",
            symbol="BTCUSDT",
            side=SideType.BUY,
            price=Decimal("100000"),
            qty=Decimal("0.05"),
            direction=DirectionType.LONG,
            grid_level=1,
            timestamp=sample_timestamp,
        )

        risk_runner._process_fill(fill_event)

        # Should have been called with market price (95000), not fill price (100000)
        assert len(called_with) == 1
        assert called_with[0] == 95000.0


class TestPairLiqHedge:
    """Hedge-mode pair liquidation formula (feature 0043).

    Validation table: docs/features/0043_PLAN.md Phase 2. Single quantitative
    point per scenario is anchored to the on-mainnet derivation data so any
    formula drift breaks these tests immediately.
    """

    @pytest.fixture
    def runner(self):
        config = BacktestStrategyConfig(
            strat_id="test_pair_liq",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=50,
            grid_step=0.2,
            amount="x0.001",
            max_margin=8.0,
            commission_rate=Decimal("0.0002"),
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            min_total_margin=0.15,
            leverage=10,
            maintenance_margin_rate=0.005,
            enable_risk_multipliers=True,
        )
        session = BacktestSession(session_id="test_pair_liq", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim, commission_rate=config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_mgr)
        return BacktestRunner(strategy_config=config, executor=executor, session=session)

    def _state(self, size, entry) -> SimpleNamespace:
        return SimpleNamespace(
            size=Decimal(str(size)),
            avg_entry_price=Decimal(str(entry)),
        )

    def test_zero_positions_returns_zero_zero(self, runner):
        """Empty trackers → both legs 0, no division-by-zero."""
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            self._state(0, 0), self._state(0, 0), Decimal("1000"),
        )
        assert (liq_long, liq_short) == (Decimal("0"), Decimal("0"))

    def test_fully_hedged_returns_zero_zero(self, runner):
        """L == S → q_net = 0 → both legs 0 (no real liq risk)."""
        long_state = self._state("2.5", "100")
        short_state = self._state("2.5", "100")
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            long_state, short_state, Decimal("1000"),
        )
        assert liq_long == Decimal("0")
        assert liq_short == Decimal("0")

    def test_net_long_dominant_leg_positive(self, runner):
        """Net long with q_net > equity-derived threshold gives positive liq_long.

        Pins the Phase 2 validation row q_net=2.0 (docs/features/0043_PLAN.md):
        L=4.5 @ 57.43403588, S=2.5 @ 57.67686663, equity=105.19303398.
        Live ``liqPrice`` from Bybit at that snapshot = 5.9049, formula
        f_full delta = -0.061 (i.e. formula = 5.8439). Test asserts both:
        the formula's exact output for those inputs AND that the result
        lands within the documented Δ band vs live Bybit data.

        BTCUSDT tier 1 MMR (0.005, deduction=0) is identical to LTCUSDT
        tier 1, so the BTCUSDT-shaped runner reproduces the LTCUSDT
        validation numerics for combined notional in the first tier.
        """
        L_size = Decimal("4.5")
        L_entry = Decimal("57.43403588")
        S_size = Decimal("2.5")
        S_entry = Decimal("57.67686663")
        equity = Decimal("105.19303398")

        long_state = self._state(L_size, L_entry)
        short_state = self._state(S_size, S_entry)
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )

        # Smaller (over-hedged) leg is always 0.
        assert liq_short == Decimal("0")

        # Pin the exact formula output computed from the same inputs.
        combined_pv = L_size * L_entry + S_size * S_entry
        mm_total = combined_pv * Decimal("0.005")  # tier 1 mmr, deduction 0
        pool = equity - mm_total
        expected = L_entry - pool / (L_size - S_size)
        assert liq_long == expected

        # Sanity vs live Bybit at the snapshot: |Δ| within 0.5 USDT of live.
        live = Decimal("5.9049")
        assert abs(liq_long - live) < Decimal("0.5")

    def test_net_short_dominant_leg_positive(self, runner):
        """Net short pins Phase 2 validation row q_net=-0.9.

        Inputs from docs/features/0043_PLAN.md table: L=2.2 @ 57.92552512,
        S=3.1 @ 57.95695445, equity=104.47256893. Live ``liqPrice`` for
        the dominant short = 171.7334, formula f_full delta = +0.598
        (so formula ≈ 172.33). Test pins both numerics.
        """
        L_size = Decimal("2.2")
        L_entry = Decimal("57.92552512")
        S_size = Decimal("3.1")
        S_entry = Decimal("57.95695445")
        equity = Decimal("104.47256893")

        long_state = self._state(L_size, L_entry)
        short_state = self._state(S_size, S_entry)
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )

        # Smaller (over-hedged) long leg is 0.
        assert liq_long == Decimal("0")

        # Pin the exact formula output.
        combined_pv = L_size * L_entry + S_size * S_entry
        mm_total = combined_pv * Decimal("0.005")
        pool = equity - mm_total
        expected = S_entry + pool / (S_size - L_size)
        assert liq_short == expected

        # Sanity vs live Bybit.
        live = Decimal("171.7334")
        assert abs(liq_short - live) < Decimal("1.0")

    def test_net_long_clamps_negative_to_zero(self, runner):
        """Net long with equity covering position → formula returns 0, not negative."""
        long_state = self._state("0.1", "100")
        short_state = self._state("0.05", "100")
        # Equity huge → pool/q_net >> entry → raw liq_long < 0 → clamped to 0.
        equity = Decimal("1000000")
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )
        assert liq_long == Decimal("0")
        assert liq_short == Decimal("0")

    def test_hedged_net_short_returns_raw_value_above_2x_entry(self, runner):
        """Hedged net short far above market: pair formula returns the raw liq.

        0043 derivation found Bybit DOES emit raw liq even when it sits well
        above market in hedge configurations (validation row: live=171 at
        S_entry=58 — above 2× entry). The per-leg cap was dropped FOR
        HEDGED CASES; see ``test_short_only_caps_above_2x_entry`` for the
        single-leg behaviour.
        """
        # Both legs > 0 → hedged → no cap.
        long_state = self._state("0.05", "100")
        short_state = self._state("0.1", "100")
        equity = Decimal("1000000")
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )
        assert liq_long == Decimal("0")
        # Raw formula yields a very large positive number — no cap in hedged.
        assert liq_short > Decimal("100") * 2  # well above 2× entry

    def test_short_only_caps_above_2x_entry(self, runner):
        """Short-only (L_size == 0) preserves the pre-0043 safe cap.

        Without a hedging long leg there is no mainnet evidence that Bybit
        emits absurd-magnitude liq prices; pre-0042 code documented Bybit
        returning 0 when computed liq exceeded 2× entry, and the pair
        formula preserves that defensively until Phase 4 comparator data
        contradicts it.
        """
        long_state = self._state("0", "0")
        short_state = self._state("0.1", "100")
        # Equity huge → raw liq_short would be entry + (1e6 - mm)/0.1 ≈ 1e7,
        # well above 2 × entry = 200.
        equity = Decimal("1000000")
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )
        assert liq_long == Decimal("0")
        # Short-only with huge equity → safe regime → capped to 0.
        assert liq_short == Decimal("0")

    def test_underwater_account_handles_negative_pool(self, runner, caplog):
        """0043 P0 fix: when mm_total > total_equity, return entry prices.

        The account is already past the MM threshold — raw formula would
        produce geometrically nonsensical values (e.g. long-liq above entry).
        Emit entry prices as a "liquidation imminent" signal so the
        comparator sees an obviously distressed state instead of garbage.
        """
        import logging

        # Large long, tiny equity → mm_total >> equity → pool < 0.
        long_state = self._state("50", "1000")
        short_state = self._state("0", "0")
        equity = Decimal("10")  # 50 * 1000 = 50_000 pv → mm ≈ 250 > equity

        with caplog.at_level(logging.WARNING, logger="backtest.runner"):
            liq_long, liq_short = runner._estimate_pair_liq_prices(
                long_state, short_state, equity,
            )

        # Signal: liquidation imminent — entry price emitted.
        assert liq_long == Decimal("1000")
        assert liq_short == Decimal("0")
        warnings = [r for r in caplog.records if "pool exhausted" in r.message]
        assert len(warnings) == 1

    def test_short_only_below_cap_returns_raw_value(self, runner):
        """Short-only with moderate equity (raw liq within 2× entry) keeps raw value."""
        long_state = self._state("0", "0")
        short_state = self._state("1.0", "100")  # S_pv = 100
        # Equity 80 → pool ≈ 79.5 → liq_short = 100 + 79.5 = 179.5, < 200 (2× entry).
        equity = Decimal("80")
        liq_long, liq_short = runner._estimate_pair_liq_prices(
            long_state, short_state, equity,
        )
        assert liq_long == Decimal("0")
        assert liq_short > Decimal("100")
        assert liq_short < Decimal("200")  # below 2× entry → no cap fired

    def test_uses_total_equity_not_current_balance(self, runner):
        """Pool input is the total_equity arg, not session.current_balance."""
        long_state = self._state("0.1", "100")
        short_state = self._state("0", "0")
        # Set current_balance high to verify it's NOT consulted.
        runner._session.current_balance = Decimal("999999")
        small_equity = Decimal("9")
        liq_long, _ = runner._estimate_pair_liq_prices(
            long_state, short_state, small_equity,
        )
        # With small equity vs L_pv=10, expect a positive liq close to entry.
        assert Decimal("0") < liq_long < Decimal("100")
        # Sanity: result must scale with the explicit equity arg, not the
        # large current_balance the test deliberately injected.
        big_equity = Decimal("90")
        liq_long_big, _ = runner._estimate_pair_liq_prices(
            long_state, short_state, big_equity,
        )
        assert liq_long_big < liq_long

    def test_combined_notional_drives_mm(self, runner):
        """MM term is computed from L_pv + S_pv, not from the dominant leg alone.

        This is the non-obvious 0043 choice: Bybit publishes the smaller leg's
        ``positionMM`` with a hedge discount but reverts to full MMR on the
        combined notional for liq calc. Regression guard: a per-leg or
        dominant-only MM substitution shifts the liq by ``Δmm / q_net`` and
        this test catches that drift.

        Setup keeps the dominant leg (long) as the asserted side and varies
        the hedged short side; equity is sized so the formula output is
        positive (not clamped to 0).
        """
        # L = 1.0 @ 100 dominant; S = 0.5 @ 100 hedged. q_net = +0.5 (net long).
        L_size = Decimal("1.0")
        L_entry = Decimal("100")
        S_size = Decimal("0.5")
        S_entry = Decimal("100")
        equity = Decimal("50")  # small enough so liq_long stays positive

        liq_long, liq_short = runner._estimate_pair_liq_prices(
            self._state(L_size, L_entry),
            self._state(S_size, S_entry),
            equity,
        )

        # Over-hedged smaller leg stays zero by branch.
        assert liq_short == Decimal("0")

        # Expected uses combined notional MM (correct):
        L_pv = L_size * L_entry           # 100
        S_pv = S_size * S_entry           # 50
        combined_pv = L_pv + S_pv         # 150
        mm_correct = combined_pv * Decimal("0.005")   # 0.75
        pool_correct = equity - mm_correct
        q_net = L_size - S_size
        expected = L_entry - pool_correct / q_net
        assert liq_long == expected

        # A wrong-implementation that took MM only from the dominant leg
        # would compute a different liq. Confirm the gap is real so this
        # test would fail if the formula regressed.
        mm_dominant_only = L_pv * Decimal("0.005")    # 0.50
        pool_wrong = equity - mm_dominant_only
        wrong_liq = L_entry - pool_wrong / q_net
        assert wrong_liq != liq_long
        # The drift is exactly Δmm / q_net = 0.25 / 0.5 = 0.5 USDT.
        assert (wrong_liq - liq_long) == Decimal("-0.5")


class TestEarlyImbalanceMultiplierBacktest:
    """Tests for early_imbalance_multiplier in backtest qty path.

    Mirrors apps/gridbot tests for live/backtest parity.
    bbu2 ref: bbu_reference/bbu2-master/bybit_api_usdt.py:257-261.
    """

    def _build_runner(self, early_imb: float, enable_risk: bool = True):
        config = BacktestStrategyConfig(
            strat_id="test_early_imb",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=50,
            grid_step=0.2,
            amount="x0.001",
            max_margin=8.0,
            commission_rate=Decimal("0.0002"),
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            min_total_margin=0.15,
            leverage=10,
            maintenance_margin_rate=0.005,
            enable_risk_multipliers=enable_risk,
            early_imbalance_multiplier=early_imb,
        )
        session = BacktestSession(session_id="test_early_imb", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=config.commission_rate,
        )

        def qty_from_usdt(intent, wallet_balance):
            if intent.price <= 0:
                return Decimal("0")
            return Decimal("100") / intent.price  # base = 100/50000 = 0.002

        executor = BacktestExecutor(order_manager=order_mgr, qty_calculator=qty_from_usdt)
        return BacktestRunner(
            strategy_config=config,
            executor=executor,
            session=session,
        )

    def _make_intent(self):
        return PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0"), grid_level=1, direction="long",
        )

    def _set_size_ratio(self, runner, long_size: str, short_size: str):
        """Set sizes (NOT position_ratio) — bbu2 keys early_imbalance on size."""
        runner._long_position.size = Decimal(long_size)
        runner._short_position.size = Decimal(short_size)

    def test_default_multiplier_is_no_op(self):
        runner = self._build_runner(early_imb=1.0)
        self._set_size_ratio(runner, "2", "1")
        runner._long_position.liquidation_price = Decimal("0")
        runner._short_position.liquidation_price = Decimal("0")

        result = runner._apply_risk_to_qty(self._make_intent(), Decimal("10000"))
        # base 0.002, mult 1.0, no early-imb boost
        assert result == Decimal("0.002")

    def test_fires_when_long_dominates_and_both_pre_liquidation(self):
        runner = self._build_runner(early_imb=1.5)
        self._set_size_ratio(runner, "2", "1")
        runner._long_position.liquidation_price = Decimal("0")
        runner._short_position.liquidation_price = Decimal("0")

        result = runner._apply_risk_to_qty(self._make_intent(), Decimal("10000"))
        # base 0.002 * 1.5 = 0.003
        assert result == Decimal("0.003")

    def test_no_op_when_ratio_out_of_band(self):
        runner = self._build_runner(early_imb=1.5)
        self._set_size_ratio(runner, "11", "1")
        runner._long_position.liquidation_price = Decimal("0")
        runner._short_position.liquidation_price = Decimal("0")

        result = runner._apply_risk_to_qty(self._make_intent(), Decimal("10000"))
        assert result == Decimal("0.002")

    def test_no_op_when_long_liq_price_set(self):
        runner = self._build_runner(early_imb=1.5)
        self._set_size_ratio(runner, "2", "1")
        runner._long_position.liquidation_price = Decimal("45000")
        runner._short_position.liquidation_price = Decimal("0")

        result = runner._apply_risk_to_qty(self._make_intent(), Decimal("10000"))
        assert result == Decimal("0.002")

    def test_no_op_when_short_empty_size_ratio_inf(self):
        """short.size=0, long.size>0 → size_ratio=inf, out of band → no-op."""
        runner = self._build_runner(early_imb=1.5)
        self._set_size_ratio(runner, "2", "0")  # short empty → ratio = inf
        runner._long_position.liquidation_price = Decimal("0")
        runner._short_position.liquidation_price = Decimal("0")

        result = runner._apply_risk_to_qty(self._make_intent(), Decimal("10000"))
        assert result == Decimal("0.002")

    def test_uses_size_not_margin_ratio(self):
        """Regression: must read sizes, not Position.position_ratio (margin-based after calculate_amount_multiplier)."""
        runner = self._build_runner(early_imb=1.5)
        # Equal sizes (size_ratio=1.0 — out of band) but poison position_ratio.
        self._set_size_ratio(runner, "1", "1")
        runner._long_position.position_ratio = 2.0
        runner._short_position.position_ratio = 2.0
        runner._long_position.liquidation_price = Decimal("0")
        runner._short_position.liquidation_price = Decimal("0")

        result = runner._apply_risk_to_qty(self._make_intent(), Decimal("10000"))
        assert result == Decimal("0.002")

    def test_end_to_end_through_update_risk_multipliers(self):
        """End-to-end: drive trackers via process_fill, then _update_risk_multipliers
        and _apply_risk_to_qty. Confirms backtest plumbing wires Position.size +
        liquidation_price correctly through the real pipeline (not direct field
        access). Mirrors live e2e test in apps/gridbot/tests/test_runner.py.

        Setup: long entry $25k size 2.0, short entry $50k size 1.0,
        wallet large enough that estimated liq is far above market → liq=0.
        size_ratio=2.0 (in band) but margin_ratio≈1.0 (out of band).
        """
        runner = self._build_runner(early_imb=1.5)
        # Inflate session balance so liquidation estimates land at 0
        # (long: liq <= 0 floored to 0; short: liq > 2*entry returned as 0).
        # 0043: pair-aware liq reads total_equity, so bump both baselines.
        runner._session.current_balance = Decimal("1000000")
        runner._session.total_equity = Decimal("1000000")

        # Drive long: process_fill with Buy (opens long).
        runner._long_tracker.process_fill(
            side="Buy", qty=Decimal("2.0"), price=Decimal("25000"),
        )
        # Drive short: process_fill with Sell (opens short).
        runner._short_tracker.process_fill(
            side="Sell", qty=Decimal("1.0"), price=Decimal("50000"),
        )

        # Drive the real risk-multiplier update path.
        runner._update_risk_multipliers(last_price=50000.0)

        # Plumbing assertion.
        assert runner._long_position.size == Decimal("2.0")
        assert runner._short_position.size == Decimal("1.0")
        assert runner._long_position.liquidation_price == Decimal("0")
        assert runner._short_position.liquidation_price == Decimal("0")

        # Capture position-rules multiplier and compute expected.
        strategy_mult = Decimal(str(runner.get_amount_multiplier("long", "Buy")))
        # base from fixture: 100 / 50000 = 0.002
        expected_pre_round = Decimal("0.002") * strategy_mult * Decimal("1.5")
        # Backtest fixture has no instrument_info → no rounding.
        result = runner._apply_risk_to_qty(self._make_intent(), Decimal("1000000"))
        assert result == expected_pre_round
        # Sanity: with mult=1.5 active, qty must exceed the no-boost baseline.
        assert result > Decimal("0.002") * strategy_mult

    # Note: when enable_risk=False, BacktestRunner does NOT install
    # _apply_risk_to_qty as the executor's qty_calculator (runner.py:121-137),
    # so the early_imb path is unreachable by construction. No separate test
    # needed for that case.


class TestBacktestRunnerRiskIntegration:
    """Integration test: risk-enabled runner with qty_calculator places non-zero orders."""

    def test_risk_enabled_places_nonzero_orders(self):
        """P0+P2: With composed qty_calculator, risk-enabled runner places orders."""
        config = BacktestStrategyConfig(
            strat_id="test_btc_integ",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=50,
            grid_step=0.2,
            amount="100",  # Fixed 100 USDT per order
            max_margin=8.0,
            commission_rate=Decimal("0.0002"),
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            min_total_margin=0.15,
            leverage=10,
            maintenance_margin_rate=0.005,
            enable_risk_multipliers=True,
        )

        session = BacktestSession(session_id="test_integ", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=config.commission_rate,
        )

        # Create executor with a real qty_calculator (fixed USDT amount)
        def qty_from_usdt(intent, wallet_balance):
            if intent.price <= 0:
                return Decimal("0")
            raw_qty = Decimal("100") / intent.price
            # Round to 0.001 (like a real instrument)
            step = Decimal("0.001")
            return (raw_qty / step).to_integral_value() * step

        executor = BacktestExecutor(order_manager=order_mgr, qty_calculator=qty_from_usdt)

        runner = BacktestRunner(
            strategy_config=config,
            executor=executor,
            session=session,
        )

        # The runner should have composed, not replaced, the qty_calculator
        assert runner._base_qty_calculator is qty_from_usdt

        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        tick = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=ts,
            local_ts=ts,
            last_price=Decimal("100000"),
            mark_price=Decimal("100000"),
            bid1_price=Decimal("99999"),
            ask1_price=Decimal("100001"),
            funding_rate=Decimal("0.0001"),
        )

        runner.process_tick(tick)

        # Grid must have placed orders with non-zero qty
        limit_orders = order_mgr.get_limit_orders()
        total_orders = len(limit_orders["long"]) + len(limit_orders["short"])
        assert total_orders > 0, "Risk-enabled runner must place orders"

        # Verify all orders have non-zero qty
        for side_key in ("long", "short"):
            for order in limit_orders[side_key]:
                assert Decimal(order["qty"]) > 0, (
                    f"Order qty must be > 0, got {order['qty']} for {side_key}"
                )


class TestShouldPlaceClose:
    """Tests for BacktestRunner._should_place_close close-order gating."""

    @pytest.fixture
    def runner(self, sample_strategy_config, session):
        """Runner with risk disabled and a fixed qty calculator."""
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=sample_strategy_config.commission_rate,
        )

        def qty_from_usdt(intent, wallet_balance):
            if intent.price <= 0:
                return Decimal("0")
            return Decimal("100") / intent.price

        executor = BacktestExecutor(order_manager=order_mgr, qty_calculator=qty_from_usdt)
        return BacktestRunner(
            strategy_config=sample_strategy_config,
            executor=executor,
            session=session,
        )

    @staticmethod
    def _close_intent(direction: DirectionType, side: SideType) -> PlaceLimitIntent:
        """Create a reduce_only PlaceLimitIntent for testing."""
        return PlaceLimitIntent(
            symbol="BTCUSDT",
            side=side,
            price=Decimal("100000"),
            qty=Decimal("0"),
            direction=direction,
            grid_level=1,
            reduce_only=True,
            client_order_id="test_close_001",
        )

    def test_no_position_returns_false(self, runner):
        """Close order rejected when no position exists (long)."""
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        assert runner._should_place_close(intent) is False

    def test_no_position_short_returns_false(self, runner):
        """Close order rejected when no position exists (short)."""
        intent = self._close_intent(DirectionType.SHORT, SideType.BUY)
        assert runner._should_place_close(intent) is False

    def test_position_exists_no_pending_returns_true(self, runner):
        """Close order allowed when position exists and no pending close orders."""
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.1"), price=Decimal("100000")
        )
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        assert runner._should_place_close(intent) is True

    def test_position_partially_covered_returns_true(self, runner, sample_timestamp):
        """Close order allowed when position is only partially covered."""
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.5"), price=Decimal("100000")
        )
        # Place one pending close order covering 0.2 of the 0.5 position
        runner._executor.order_manager.place_order(
            client_order_id="close_1",
            symbol="BTCUSDT",
            side=SideType.SELL,
            price=Decimal("101000"),
            qty=Decimal("0.2"),
            direction=DirectionType.LONG,
            grid_level=10,
            timestamp=sample_timestamp,
            reduce_only=True,
        )
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        assert runner._should_place_close(intent) is True

    def test_position_fully_covered_returns_false(self, runner, sample_timestamp):
        """Close order rejected when position is fully covered by pending close orders."""
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.3"), price=Decimal("100000")
        )
        # Place pending close orders exactly matching position size
        runner._executor.order_manager.place_order(
            client_order_id="close_1",
            symbol="BTCUSDT",
            side=SideType.SELL,
            price=Decimal("101000"),
            qty=Decimal("0.2"),
            direction=DirectionType.LONG,
            grid_level=10,
            timestamp=sample_timestamp,
            reduce_only=True,
        )
        runner._executor.order_manager.place_order(
            client_order_id="close_2",
            symbol="BTCUSDT",
            side=SideType.SELL,
            price=Decimal("102000"),
            qty=Decimal("0.1"),
            direction=DirectionType.LONG,
            grid_level=11,
            timestamp=sample_timestamp,
            reduce_only=True,
        )
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        assert runner._should_place_close(intent) is False

    def test_over_hedged_returns_false_and_logs_warning(self, runner, sample_timestamp, caplog):
        """Over-hedged scenario returns False and logs a warning."""
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.1"), price=Decimal("100000")
        )
        # Pending close qty (0.2) exceeds position size (0.1)
        runner._executor.order_manager.place_order(
            client_order_id="close_1",
            symbol="BTCUSDT",
            side=SideType.SELL,
            price=Decimal("101000"),
            qty=Decimal("0.2"),
            direction=DirectionType.LONG,
            grid_level=10,
            timestamp=sample_timestamp,
            reduce_only=True,
        )
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        with caplog.at_level(logging.WARNING, logger="backtest.runner"):
            result = runner._should_place_close(intent)

        assert result is False
        warning_records = [r for r in caplog.records if "Over-hedged" in r.message]
        assert len(warning_records) == 1
        assert "Active close orders:" in warning_records[0].message
        assert "0.2" in warning_records[0].message

    def test_short_direction_uses_short_tracker(self, runner, sample_timestamp):
        """Close order for short direction checks the short position tracker."""
        # Long has a position, short does not
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.5"), price=Decimal("100000")
        )
        # Short close should be rejected (no short position)
        intent = self._close_intent(DirectionType.SHORT, SideType.BUY)
        assert runner._should_place_close(intent) is False

        # Now add short position
        runner._short_tracker.process_fill(
            side=SideType.SELL, qty=Decimal("0.3"), price=Decimal("100000")
        )
        assert runner._should_place_close(intent) is True

    def test_non_reduce_only_orders_not_counted(self, runner, sample_timestamp):
        """Open (non-reduce_only) orders don't count toward pending close qty."""
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.1"), price=Decimal("100000")
        )
        # Place an open order (reduce_only=False) — should not block close placement
        runner._executor.order_manager.place_order(
            client_order_id="open_1",
            symbol="BTCUSDT",
            side=SideType.SELL,
            price=Decimal("101000"),
            qty=Decimal("0.5"),
            direction=DirectionType.LONG,
            grid_level=10,
            timestamp=sample_timestamp,
            reduce_only=False,
        )
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        assert runner._should_place_close(intent) is True

    def test_over_close_blocked_by_resolved_qty(self, runner, sample_timestamp):
        """Close order rejected when resolved qty + pending would exceed position.

        Regression: position=0.002, pending=0.001, resolved=0.001.
        Total 0.001+0.001=0.002, not > 0.002 → blocked.
        Before fix, backtest only checked 0.002 > 0.001 → allowed.
        """
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.002"), price=Decimal("100000")
        )
        runner._executor.order_manager.place_order(
            client_order_id="close_1",
            symbol="BTCUSDT",
            side=SideType.SELL,
            price=Decimal("101000"),
            qty=Decimal("0.001"),
            direction=DirectionType.LONG,
            grid_level=10,
            timestamp=sample_timestamp,
            reduce_only=True,
        )
        # qty_from_usdt resolves 100/100000 = 0.001
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        assert runner._should_place_close(intent) is False

    def test_resolved_qty_still_allows_when_room_remains(self, runner, sample_timestamp):
        """Close order allowed when resolved qty + pending still leaves room."""
        runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.5"), price=Decimal("100000")
        )
        # pending=0.2, resolved=0.001, total=0.201 < 0.5
        runner._executor.order_manager.place_order(
            client_order_id="close_1",
            symbol="BTCUSDT",
            side=SideType.SELL,
            price=Decimal("101000"),
            qty=Decimal("0.2"),
            direction=DirectionType.LONG,
            grid_level=10,
            timestamp=sample_timestamp,
            reduce_only=True,
        )
        intent = self._close_intent(DirectionType.LONG, SideType.SELL)
        assert runner._should_place_close(intent) is True


class TestGetPendingCloseQty:
    """Tests for BacktestRunner._get_pending_close_qty helper."""

    @pytest.fixture
    def runner(self, sample_strategy_config, session):
        """Runner with risk disabled."""
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=sample_strategy_config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_mgr)
        return BacktestRunner(
            strategy_config=sample_strategy_config,
            executor=executor,
            session=session,
        )

    def test_no_orders_returns_zero(self, runner):
        """No active orders returns zero."""
        assert runner._get_pending_close_qty(DirectionType.LONG) == Decimal("0")

    def test_sums_reduce_only_for_direction(self, runner, sample_timestamp):
        """Sums qty of reduce_only orders for the specified direction."""
        runner._executor.order_manager.place_order(
            client_order_id="c1", symbol="BTCUSDT", side=SideType.SELL,
            price=Decimal("101000"), qty=Decimal("0.1"),
            direction=DirectionType.LONG, grid_level=10,
            timestamp=sample_timestamp, reduce_only=True,
        )
        runner._executor.order_manager.place_order(
            client_order_id="c2", symbol="BTCUSDT", side=SideType.SELL,
            price=Decimal("102000"), qty=Decimal("0.05"),
            direction=DirectionType.LONG, grid_level=11,
            timestamp=sample_timestamp, reduce_only=True,
        )
        assert runner._get_pending_close_qty(DirectionType.LONG) == Decimal("0.15")

    def test_ignores_non_reduce_only(self, runner, sample_timestamp):
        """Non-reduce_only orders are not counted."""
        runner._executor.order_manager.place_order(
            client_order_id="open_1", symbol="BTCUSDT", side=SideType.BUY,
            price=Decimal("99000"), qty=Decimal("0.5"),
            direction=DirectionType.LONG, grid_level=5,
            timestamp=sample_timestamp, reduce_only=False,
        )
        runner._executor.order_manager.place_order(
            client_order_id="close_1", symbol="BTCUSDT", side=SideType.SELL,
            price=Decimal("101000"), qty=Decimal("0.1"),
            direction=DirectionType.LONG, grid_level=10,
            timestamp=sample_timestamp, reduce_only=True,
        )
        assert runner._get_pending_close_qty(DirectionType.LONG) == Decimal("0.1")

    def test_ignores_opposite_direction(self, runner, sample_timestamp):
        """Reduce_only orders from the opposite direction are not counted."""
        runner._executor.order_manager.place_order(
            client_order_id="short_close", symbol="BTCUSDT", side=SideType.BUY,
            price=Decimal("99000"), qty=Decimal("0.3"),
            direction=DirectionType.SHORT, grid_level=5,
            timestamp=sample_timestamp, reduce_only=True,
        )
        assert runner._get_pending_close_qty(DirectionType.LONG) == Decimal("0")
        assert runner._get_pending_close_qty(DirectionType.SHORT) == Decimal("0.3")

    def test_mixed_directions_and_types(self, runner, sample_timestamp):
        """Correctly filters among mixed directions and reduce_only flags."""
        orders = [
            ("l_open", SideType.BUY, Decimal("0.1"), DirectionType.LONG, False),
            ("l_close1", SideType.SELL, Decimal("0.05"), DirectionType.LONG, True),
            ("l_close2", SideType.SELL, Decimal("0.03"), DirectionType.LONG, True),
            ("s_open", SideType.SELL, Decimal("0.2"), DirectionType.SHORT, False),
            ("s_close", SideType.BUY, Decimal("0.07"), DirectionType.SHORT, True),
        ]
        for cid, side, qty, direction, ro in orders:
            runner._executor.order_manager.place_order(
                client_order_id=cid, symbol="BTCUSDT", side=side,
                price=Decimal("100000"), qty=qty, direction=direction,
                grid_level=1, timestamp=sample_timestamp, reduce_only=ro,
            )
        assert runner._get_pending_close_qty(DirectionType.LONG) == Decimal("0.08")
        assert runner._get_pending_close_qty(DirectionType.SHORT) == Decimal("0.07")


class TestSeedAwareConstruction:
    """Tests for replay-driven seeding of trackers, order manager, and runner.

    Covers feature 0029 Phase 2B: replay constructs the runner with
    pre-populated position state, active orders on the exchange, and a
    restored grid layout. Seed dataclasses live in
    ``apps/replay/src/replay/snapshot_loader.py`` (Phase 2A); these tests
    use lightweight stand-ins matching the documented attribute set so the
    backtest side stays decoupled from the loader module's import path.
    """

    # --- minimal seed stand-ins (duck typed; mirror Phase 2A dataclasses) ---

    @dataclass
    class _PositionStateSeed:
        direction: str
        size: Decimal
        entry_price: Decimal
        liquidation_price: Decimal

    @dataclass
    class _ActiveOrderSeed:
        client_id: str
        exchange_order_id: str
        symbol: str
        side: str
        direction: str
        price: Decimal
        remaining_qty: Decimal
        reduce_only: bool
        exchange_ts: datetime

    # --- fixtures ---

    @pytest.fixture
    def seed_strategy_config(self):
        """Strategy config with risk multipliers enabled (matches replay use)."""
        return BacktestStrategyConfig(
            strat_id="test_seed",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=50,
            grid_step=0.2,
            amount="x0.001",
            max_margin=8.0,
            commission_rate=Decimal("0.0002"),
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            min_total_margin=0.15,
            leverage=10,
            maintenance_margin_rate=0.005,
            enable_risk_multipliers=True,
        )

    @pytest.fixture
    def seed_runner_components(self, seed_strategy_config):
        """Build trackers/executor/session/order_manager wired together."""
        session = BacktestSession(
            session_id="test_seed", initial_balance=Decimal("10000"),
        )
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim,
            commission_rate=seed_strategy_config.commission_rate,
        )

        def qty_from_usdt(intent, wallet_balance):
            if intent.price <= 0:
                return Decimal("0")
            return Decimal("100") / intent.price

        executor = BacktestExecutor(
            order_manager=order_mgr, qty_calculator=qty_from_usdt,
        )
        return seed_strategy_config, executor, session, order_mgr

    # --- A. tracker.seed_state ---

    def test_seed_state_writes_position_fields(self):
        """seed_state writes size/entry/liq and zeroes the accounting fields."""
        from backtest.position_tracker import BacktestPositionTracker

        tracker = BacktestPositionTracker(
            direction="long", commission_rate=Decimal("0.0002"),
        )
        # Pre-populate the accounting fields so we can prove they get reset.
        tracker.state.realized_pnl = Decimal("123")
        tracker.state.commission_paid = Decimal("4.5")
        tracker.state.funding_paid = Decimal("0.7")

        seed = self._PositionStateSeed(
            direction="long",
            size=Decimal("2.0"),
            entry_price=Decimal("25000"),
            liquidation_price=Decimal("20000"),
        )
        tracker.seed_state(seed)

        assert tracker.state.size == Decimal("2.0")
        assert tracker.state.avg_entry_price == Decimal("25000")
        assert tracker.state.liquidation_price == Decimal("20000")
        assert tracker.state.realized_pnl == Decimal("0")
        assert tracker.state.commission_paid == Decimal("0")
        assert tracker.state.funding_paid == Decimal("0")

    # --- B. order_manager.seed_active_orders ---

    def test_seed_active_orders_register_in_store(self):
        """Both seeded orders land in active_orders + their client_ids."""
        from backtest.order_manager import BacktestOrderManager

        order_mgr = BacktestOrderManager(
            fill_simulator=TradeThroughFillSimulator(),
            commission_rate=Decimal("0.0002"),
        )
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        seed1 = self._ActiveOrderSeed(
            client_id="LX-001",
            exchange_order_id="exch-A",
            symbol="BTCUSDT",
            side="Buy",
            direction="long",
            price=Decimal("49000"),
            remaining_qty=Decimal("0.05"),
            reduce_only=False,
            exchange_ts=ts,
        )
        # client_id deliberately equals exchange_order_id to mirror the
        # `client_id = order_link_id or order_id` fallback case (live order
        # placed before the orderLinkId fix where order_link_id is NULL).
        seed2 = self._ActiveOrderSeed(
            client_id="exch-B",
            exchange_order_id="exch-B",
            symbol="BTCUSDT",
            side="Sell",
            direction="short",
            price=Decimal("51000"),
            remaining_qty=Decimal("0.04"),
            reduce_only=False,
            exchange_ts=ts,
        )

        order_mgr.seed_active_orders([seed1, seed2])

        assert "exch-A" in order_mgr.active_orders
        assert "exch-B" in order_mgr.active_orders
        assert order_mgr.active_orders["exch-A"].client_order_id == "LX-001"
        assert order_mgr.active_orders["exch-B"].client_order_id == "exch-B"
        assert order_mgr.active_orders["exch-A"].grid_level == 0
        assert "LX-001" in order_mgr._client_order_ids
        assert "exch-B" in order_mgr._client_order_ids

    # --- C. seeded order participates in fill check ---

    def test_seed_active_order_fills_on_strict_cross(self):
        """Seeded Buy@50000 does NOT fill at 50001, DOES fill at 49999."""
        from backtest.order_manager import BacktestOrderManager

        order_mgr = BacktestOrderManager(
            fill_simulator=TradeThroughFillSimulator(),
            commission_rate=Decimal("0.0002"),
        )
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        seed = self._ActiveOrderSeed(
            client_id="LX-buy",
            exchange_order_id="exch-buy",
            symbol="BTCUSDT",
            side="Buy",
            direction="long",
            price=Decimal("50000"),
            remaining_qty=Decimal("0.1"),
            reduce_only=False,
            exchange_ts=ts,
        )
        order_mgr.seed_active_orders([seed])

        # Above limit + at limit: no fill (strict <).
        fills_above = order_mgr.check_fills(
            current_price=Decimal("50001"), timestamp=ts, symbol=None,
        )
        assert fills_above == []
        assert "exch-buy" in order_mgr.active_orders

        # Below limit: fill.
        fills_below = order_mgr.check_fills(
            current_price=Decimal("49999"), timestamp=ts, symbol=None,
        )
        assert len(fills_below) == 1
        assert fills_below[0].order_id == "exch-buy"
        assert fills_below[0].order_link_id == "LX-buy"
        assert fills_below[0].price == Decimal("50000")
        assert "exch-buy" not in order_mgr.active_orders

    # --- D. runner propagates restored_grid ---

    def test_runner_propagates_restored_grid(self, seed_runner_components):
        """restored_grid is plumbed through GridEngine and rebuilt on Grid."""
        config, executor, session, _ = seed_runner_components
        restored = [
            {"side": "Buy", "price": "49000"},
            {"side": "Sell", "price": "51000"},
        ]

        runner = BacktestRunner(
            strategy_config=config,
            executor=executor,
            session=session,
            restored_grid=restored,
        )

        grid = runner._engine.grid.grid
        assert len(grid) == 2
        # Grid.restore_grid coerces side to GridSideType and price to float.
        assert str(grid[0]["side"]) in ("GridSideType.BUY", "Buy")
        assert grid[0]["price"] == 49000.0
        assert grid[1]["price"] == 51000.0

    # --- E. runner copies seeded state into gridcore.Position ---

    def test_runner_copies_seeded_state_to_gridcore_position(
        self, seed_runner_components,
    ):
        """Pre-seeded tracker state lands on gridcore.Position via runner ctor."""
        from backtest.position_tracker import BacktestPositionTracker

        config, executor, session, _ = seed_runner_components

        long_tracker = BacktestPositionTracker(
            direction="long", commission_rate=config.commission_rate,
        )
        short_tracker = BacktestPositionTracker(
            direction="short", commission_rate=config.commission_rate,
        )
        # Pre-seed (mirrors what ReplayEngine does in Phase 3 before
        # constructing the runner).
        long_tracker.seed_state(self._PositionStateSeed(
            direction="long",
            size=Decimal("2"),
            entry_price=Decimal("25000"),
            liquidation_price=Decimal("0"),
        ))

        runner = BacktestRunner(
            strategy_config=config,
            executor=executor,
            session=session,
            long_tracker=long_tracker,
            short_tracker=short_tracker,
        )

        assert runner._long_position is not None
        assert runner._long_position.size == Decimal("2")
        assert runner._long_position.liquidation_price == Decimal("0")
        # Short was not seeded → remains zero on the gridcore side.
        assert runner._short_position.size == Decimal("0")
        assert runner._short_position.liquidation_price == Decimal("0")


class TestPositionSnapshotEmission:
    """Feature 0034 — backtest emits parity-checkable PositionSnapshots."""

    @pytest.fixture
    def runner(self, sample_strategy_config, session):
        fill_simulator = TradeThroughFillSimulator()
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=sample_strategy_config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_manager, qty_calculator=None)
        return BacktestRunner(
            strategy_config=sample_strategy_config,
            executor=executor,
            session=session,
        )

    def test_emits_after_long_fill(self, runner, sample_timestamp):
        # Manually drive a fill via the long tracker, then emit.
        runner.long_tracker.process_fill(
            side="Buy", qty=Decimal("0.01"), price=Decimal("100"),
        )
        runner._last_price = Decimal("101")
        snap = runner._emit_position_snapshot(
            DirectionType.LONG, sample_timestamp, Decimal("101"),
        )
        assert snap.side == "Buy"
        assert snap.size == Decimal("0.01")
        assert snap.entry_price == Decimal("100")
        # Unrealized = (101 - 100) * 0.01 = 0.01
        assert snap.unrealised_pnl == Decimal("0.01")
        assert snap.cum_realised_pnl == Decimal("0")  # opening fill
        assert snap.mark_price == Decimal("101")

    def test_emits_after_short_fill_correct_sign(self, runner, sample_timestamp):
        runner.short_tracker.process_fill(
            side="Sell", qty=Decimal("0.01"), price=Decimal("100"),
        )
        snap = runner._emit_position_snapshot(
            DirectionType.SHORT, sample_timestamp, Decimal("90"),
        )
        assert snap.side == "Sell"
        # Short: (entry - mark) * size = (100 - 90) * 0.01 = 0.1 (positive!)
        assert snap.unrealised_pnl == Decimal("0.1")

    def test_cum_realised_pnl_accumulates(self, runner, sample_timestamp):
        # Open at 100, close at 110 → realized = 10 * 0.01 = 0.1
        runner.long_tracker.process_fill("Buy", Decimal("0.01"), Decimal("100"))
        runner.long_tracker.process_fill("Sell", Decimal("0.01"), Decimal("110"))
        snap = runner._emit_position_snapshot(
            DirectionType.LONG, sample_timestamp, Decimal("110"),
        )
        assert snap.cum_realised_pnl == Decimal("0.1")

        # Second cycle adds another 0.1
        runner.long_tracker.process_fill("Buy", Decimal("0.01"), Decimal("100"))
        runner.long_tracker.process_fill("Sell", Decimal("0.01"), Decimal("110"))
        snap2 = runner._emit_position_snapshot(
            DirectionType.LONG, sample_timestamp, Decimal("110"),
        )
        assert snap2.cum_realised_pnl == Decimal("0.2")

    def test_no_callback_no_emission(self, runner, sample_ticker_event):
        """Default callback=None → no error, no emission."""
        assert runner.position_snapshot_callback is None
        # Process a tick that builds grid + might fill — should not raise.
        runner.process_tick(sample_ticker_event)

    def test_seeded_cum_realised_pnl_initial(self):
        from backtest.position_tracker import BacktestPositionTracker
        from dataclasses import dataclass

        @dataclass
        class _Seed:
            size: Decimal
            entry_price: Decimal
            liquidation_price: Decimal
            cum_realised_pnl: Decimal

        tracker = BacktestPositionTracker(direction="long")
        tracker.seed_state(_Seed(
            size=Decimal("1"),
            entry_price=Decimal("100"),
            liquidation_price=Decimal("0"),
            cum_realised_pnl=Decimal("42.5"),
        ))
        assert tracker.state.cum_realised_pnl == Decimal("42.5")
        # And realized_pnl (window-scoped) is zeroed
        assert tracker.state.realized_pnl == Decimal("0")

    def test_process_fill_refreshes_session_equity_before_snapshot_emit(
        self, sample_strategy_config, sample_timestamp,
    ):
        """0043 review fix: `_process_fill` must refresh `session.total_equity`
        before the parity snapshot is emitted, otherwise the comparator sees
        post-fill position size paired with pre-fill equity in the liq input.

        Regression: a fill that records non-zero realized_pnl is processed.
        After `_process_fill` returns (before the engine's per-tick
        `update_equity`), `session.total_equity` must already reflect the
        new realized PnL.
        """
        from gridcore import ExecutionEvent
        from gridcore.events import EventType
        from backtest.order_manager import SimulatedOrder
        from backtest.session import BacktestSession

        # Distinct initial balances so the test can tell which field updated.
        session = BacktestSession(
            session_id="p1_regress",
            initial_balance=Decimal("10000"),
            initial_equity=Decimal("15000"),
        )
        fill_simulator = TradeThroughFillSimulator()
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=sample_strategy_config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_manager, qty_calculator=None)
        runner = BacktestRunner(
            strategy_config=sample_strategy_config, executor=executor, session=session,
        )

        # Open a long; tracker now has a non-trivial unrealized PnL when mark
        # moves below entry.
        runner.long_tracker.process_fill("Buy", Decimal("1.0"), Decimal("100"))
        runner._last_mark_price = Decimal("90")

        # Pre-fill snapshot of session state.
        pre_equity = session.total_equity
        pre_realized = session.total_realized_pnl
        assert pre_equity == Decimal("15000")
        assert pre_realized == Decimal("0")

        # Register the closing order so direction lookup succeeds.
        order_manager.active_orders["close-1"] = SimulatedOrder(
            order_id="ord-1",
            client_order_id="close-1",
            symbol=sample_strategy_config.symbol,
            side="Sell",
            direction=DirectionType.LONG,
            price=Decimal("110"),
            qty=Decimal("0.5"),
            reduce_only=True,
            grid_level=0,
        )

        # Close 0.5 @ 110 — realized PnL = 5.
        event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol=sample_strategy_config.symbol,
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            exec_id="exec-1",
            order_id="ord-1",
            order_link_id="close-1",
            side="Sell",
            price=Decimal("110"),
            qty=Decimal("0.5"),
            fee=Decimal("0.011"),
        )
        runner._process_fill(event)

        # `session.total_realized_pnl` was bumped by record_trade.
        assert session.total_realized_pnl == Decimal("5.0")
        # 0043 fix: total_equity should already reflect post-fill state.
        # initial_equity=15000 + realized 5 + unrealized (remaining 0.5
        # long @ avg 100, mark 90 → -5) - commission 0.011 = 14999.989.
        expected_equity = (
            Decimal("15000") + Decimal("5") + Decimal("-5") - Decimal("0.011")
        )
        assert session.total_equity == expected_equity
        # Must have changed from the pre-fill value.
        assert session.total_equity != pre_equity

    def test_emission_uses_ticker_mark_not_last_price(
        self, sample_strategy_config, session, sample_timestamp,
    ):
        """Regression: emitted snapshot.mark_price must equal ticker.mark_price,
        not ticker.last_price. The PositionSnapshot.mark_price column holds
        Bybit's markPrice on the live side; if backtest writes last_price
        there, the comparator's apples-to-apples recomputation drifts.
        """
        captured: list = []

        fill_simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)
        order_manager = BacktestOrderManager(
            fill_simulator=fill_simulator,
            commission_rate=sample_strategy_config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_manager, qty_calculator=None)
        runner = BacktestRunner(
            strategy_config=sample_strategy_config,
            executor=executor,
            session=session,
        )
        runner.position_snapshot_callback = captured.append

        # Place a buy order that will fill when bid touches it.
        order_manager.place_order(
            client_order_id="mark-vs-last",
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("100.0"),
            qty=Decimal("0.1"),
            direction="long",
            grid_level=0,
            timestamp=sample_timestamp,
        )

        # Ticker where last_price diverges from mark_price.
        tick = TickerEvent(
            event_type=EventType.TICKER,
            symbol="BTCUSDT",
            exchange_ts=sample_timestamp,
            local_ts=sample_timestamp,
            last_price=Decimal("99.0"),   # below order → fills
            mark_price=Decimal("103.5"),  # deliberately different
            bid1_price=Decimal("100.0"),
            ask1_price=Decimal("99.5"),
            funding_rate=Decimal("0"),
        )
        runner.process_fills(tick)

        assert len(captured) == 1, "expected exactly one snapshot emission"
        assert captured[0].mark_price == Decimal("103.5"), (
            "snapshot.mark_price should reflect ticker.mark_price, "
            f"not last_price (got {captured[0].mark_price})"
        )


class TestEstimatePairImMm:
    """Feature 0045 — hedge-aware ``_estimate_pair_im_mm`` helper.

    Closed-form formula derived from Bybit help-center docs
    ("Initial Margin USDT Contract", "Maintenance Margin USDT Contract")
    plus empirical validation against 10 paired LTCUSDT live snapshots
    (see ``docs/features/0045_PLAN.md`` Phase 1 Results).
    """

    @pytest.fixture
    def runner(self):
        """LTCUSDT runner with realistic Bybit hedge-mode parameters.

        Matches the data used in Phase 1 derivation so golden values
        are reproducible.
        """
        from backtest.session import BacktestSession

        config = BacktestStrategyConfig(
            strat_id="test_0045_ltc",
            symbol="LTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=20,
            grid_step=0.3,
            amount="x0.001",
            commission_rate=Decimal("0.0002"),
            leverage=10,
            maintenance_margin_rate=0.005,
            enable_risk_multipliers=False,
            taker_fee_rate=Decimal("0.00075"),
            hedge_smaller_buffer_factor=Decimal("5.657"),
        )
        session = BacktestSession(session_id="t0045", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim, commission_rate=config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_mgr)
        return BacktestRunner(
            strategy_config=config, executor=executor, session=session,
        )

    def _state(self, size: Decimal, entry: Decimal) -> SimpleNamespace:
        return SimpleNamespace(size=size, avg_entry_price=entry)

    # ----- Collapse cases -----

    def test_long_only_matches_one_way_with_fee(self, runner):
        """Test #1: Long-only collapses to Bybit one-way formula with fee.

        New (post-0045) acceptance: helper produces
            (pv_mark/lev + fee_long, pv_mark*MMR_tier + fee_long, 0, 0)
        — same shape as Bybit's published positionIM / positionMM in
        one-way mode. This is intentionally NOT decimal-equal to the
        pre-0045 ``calc_initial_margin(L_pv)`` path because that path
        omitted the fee-to-close component (a known ~0.23 USDT gap).
        """
        L = self._state(Decimal("6.2"), Decimal("55.91368848"))
        S = self._state(Decimal("0"), Decimal("0"))
        mark = Decimal("53.70")
        im_L, mm_L, im_S, mm_S = runner._estimate_pair_im_mm(L, S, mark)

        # Tier 1 LTCUSDT (mmr=0.01, deduction=0).
        fee_long = Decimal("6.2") * Decimal("55.91368848") * (
            Decimal("1") - Decimal("1") / Decimal("10")
        ) * Decimal("0.00075")
        expected_im = Decimal("6.2") * mark / Decimal("10") + fee_long
        expected_mm = Decimal("6.2") * mark * Decimal("0.01") + fee_long

        assert im_L == expected_im
        assert mm_L == expected_mm
        assert im_S == Decimal("0")
        assert mm_S == Decimal("0")

    def test_short_only_matches_one_way_with_fee(self, runner):
        """Test #2: Short-only symmetric to long-only with (1+1/lev) fee."""
        L = self._state(Decimal("0"), Decimal("0"))
        S = self._state(Decimal("2.2"), Decimal("55.53059536"))
        mark = Decimal("53.70")
        im_L, mm_L, im_S, mm_S = runner._estimate_pair_im_mm(L, S, mark)

        fee_short = Decimal("2.2") * Decimal("55.53059536") * (
            Decimal("1") + Decimal("1") / Decimal("10")
        ) * Decimal("0.00075")
        expected_im = Decimal("2.2") * mark / Decimal("10") + fee_short
        expected_mm = Decimal("2.2") * mark * Decimal("0.01") + fee_short

        assert im_L == Decimal("0")
        assert mm_L == Decimal("0")
        assert im_S == expected_im
        assert mm_S == expected_mm

    def test_zero_positions(self, runner):
        """Test #6: both legs zero returns all zeros — no DivisionByZero."""
        L = self._state(Decimal("0"), Decimal("0"))
        S = self._state(Decimal("0"), Decimal("0"))
        result = runner._estimate_pair_im_mm(L, S, Decimal("53.70"))
        assert result == (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))

    # ----- Hedge cases — golden values from Phase 1 paired snapshots -----

    def test_imbalanced_long_dominant_golden(self, runner):
        """Test #4: imbalanced hedge, long-dominant — matches Bybit live.

        Golden fixture from feature 0045 Phase 1 Results, paired
        snapshot at 2026-05-18 08:50:43 (L=6.2, S=2.2, mark=53.70):
            live_im_L  = 33.52648911
            live_mm_L  =  2.38048911
            live_im_S  =  0.14782244
            live_mm_S  =  0.14782244
        """
        L = self._state(Decimal("6.2"), Decimal("55.91368848"))
        S = self._state(Decimal("2.2"), Decimal("55.53059536"))
        mark = Decimal("53.70")
        im_L, mm_L, im_S, mm_S = runner._estimate_pair_im_mm(L, S, mark)

        # Tolerance accounts for the gap between the configured taker
        # rate (0.00075) and the live account's effective rate
        # (~0.0007444) — under 0.005 USDT, well below the plan's 0.1
        # USDT MM threshold.
        tol = Decimal("0.005")
        assert abs(im_L - Decimal("33.52648911")) <= tol, f"im_L={im_L}"
        assert abs(mm_L - Decimal("2.38048911")) <= tol, f"mm_L={mm_L}"
        assert abs(im_S - Decimal("0.14782244")) <= tol, f"im_S={im_S}"
        assert abs(mm_S - Decimal("0.14782244")) <= tol, f"mm_S={mm_S}"

    def test_imbalanced_short_dominant_golden(self, runner):
        """Test #5: imbalanced hedge, short-dominant — mirror of long-dominant.

        Mirrors test #4 by swapping leg sizes and entries; same mark.
        Verifies the symmetric short-dominant branch produces the
        same magnitudes as the long-dominant case did under #4.
        """
        # Swap roles: short bigger now.
        L = self._state(Decimal("2.2"), Decimal("55.53059536"))
        S = self._state(Decimal("6.2"), Decimal("55.91368848"))
        mark = Decimal("53.70")
        im_L, mm_L, im_S, mm_S = runner._estimate_pair_im_mm(L, S, mark)

        # Compare to test #4's dominant-leg expectations swapped: the
        # short-dominant case picks up the (1+1/lev) fee factor on the
        # dominant short, the (1-1/lev) on the smaller long.
        lev = Decimal("10")
        inv = Decimal("1") / lev
        fee_short = Decimal("6.2") * Decimal("55.91368848") * (
            Decimal("1") + inv
        ) * Decimal("0.00075")
        unhedged_short = Decimal("6.2") - Decimal("2.2")
        expected_im_S = Decimal("6.2") * mark / lev + fee_short
        expected_mm_S = unhedged_short * mark * Decimal("0.01") + fee_short

        assert im_S == expected_im_S, f"im_S={im_S}"
        assert mm_S == expected_mm_S, f"mm_S={mm_S}"
        # Smaller (long) leg sees fee + buffer term.
        assert im_L == mm_L, "smaller leg should have IM == MM"
        assert im_L > Decimal("0"), "smaller leg has non-zero residual"

    def test_balanced_hedge(self, runner):
        """Test #3: balanced hedge — L_size == S_size, different entries.

        No unhedged portion. Dominant leg's MM term collapses to zero
        + fee_to_close; smaller-leg has only its fee + buffer.
        """
        L = self._state(Decimal("2.2"), Decimal("55.91368848"))
        S = self._state(Decimal("2.2"), Decimal("55.53059536"))
        mark = Decimal("53.70")
        im_L, mm_L, im_S, mm_S = runner._estimate_pair_im_mm(L, S, mark)

        # Long is "dominant" (L >= S branch) but unhedged_long = 0, so
        # dominant MM = fee_long only.
        fee_long = Decimal("2.2") * Decimal("55.91368848") * (
            Decimal("1") - Decimal("0.1")
        ) * Decimal("0.00075")
        expected_im_L = Decimal("2.2") * mark / Decimal("10") + fee_long
        expected_mm_L = fee_long  # unhedged_long * ... + fee = 0 + fee

        assert im_L == expected_im_L
        assert mm_L == expected_mm_L
        assert im_S > Decimal("0")
        assert im_S == mm_S

    def test_tier_boundary_uses_dominant_leg_tier(self, runner):
        """Test #7: tier-boundary crossing — Bybit tier picks per-leg pv.

        LTCUSDT tier 1 max=200,000 USDT. A position with leg pv > 200k
        crosses into tier 2 (mmr=0.015, deduction=1000). Helper looks
        up the tier on the leg's own pv (not the combined), matching
        Bybit's per-leg ``riskLimitValue`` assignment. The deduction
        term carries through to keep the per-tier MM formula
        continuous at the tier boundary.
        """
        # Long pv = 6000 LTC * 50 USDT = 300_000 USDT → tier 2 (max=400k).
        # Short pv smaller — stays in tier 1.
        L = self._state(Decimal("6000"), Decimal("50"))
        S = self._state(Decimal("100"), Decimal("50"))
        mark = Decimal("50")
        im_L, mm_L, _, _ = runner._estimate_pair_im_mm(L, S, mark)

        # Sanity: this is tier 2 (mmr=0.015, deduction=1000), not tier 1.
        mmr_long, deduction_long = runner._tier_mmr_and_deduction(
            Decimal("6000") * mark,
        )
        assert mmr_long == Decimal("0.015"), f"got tier MMR {mmr_long}"
        assert deduction_long == Decimal("1000"), f"got tier ded {deduction_long}"

        unhedged_long_pv = (Decimal("6000") - Decimal("100")) * mark  # 295_000
        fee_long = Decimal("6000") * Decimal("50") * (
            Decimal("1") - Decimal("0.1")
        ) * Decimal("0.00075")
        expected_mm = max(
            unhedged_long_pv * mmr_long - deduction_long, Decimal("0"),
        ) + fee_long
        expected_im = Decimal("6000") * mark / Decimal("10") + fee_long
        assert im_L == expected_im
        assert mm_L == expected_mm

    # ----- Integration with _emit_position_snapshot -----

    def test_emit_snapshot_long_uses_pair_helper(self, runner):
        """Test #8: long-direction snapshot uses pair-aware IM/MM."""
        runner.long_tracker.process_fill(
            side="Buy", qty=Decimal("6.2"), price=Decimal("55.91368848"),
        )
        runner.short_tracker.process_fill(
            side="Sell", qty=Decimal("2.2"), price=Decimal("55.53059536"),
        )
        ts = datetime(2026, 5, 18, 8, 50, 43, tzinfo=timezone.utc)
        snap = runner._emit_position_snapshot(
            DirectionType.LONG, ts, Decimal("53.70"),
        )
        # Should match the dominant-leg values from the golden fixture.
        tol = Decimal("0.005")
        assert abs(snap.position_im - Decimal("33.52648911")) <= tol
        assert abs(snap.position_mm - Decimal("2.38048911")) <= tol

    def test_emit_snapshot_short_uses_pair_helper(self, runner):
        """Test #9: short-direction snapshot picks the smaller-leg values."""
        runner.long_tracker.process_fill(
            side="Buy", qty=Decimal("6.2"), price=Decimal("55.91368848"),
        )
        runner.short_tracker.process_fill(
            side="Sell", qty=Decimal("2.2"), price=Decimal("55.53059536"),
        )
        ts = datetime(2026, 5, 18, 8, 50, 43, tzinfo=timezone.utc)
        snap = runner._emit_position_snapshot(
            DirectionType.SHORT, ts, Decimal("53.70"),
        )
        tol = Decimal("0.005")
        assert abs(snap.position_im - Decimal("0.14782244")) <= tol
        assert abs(snap.position_mm - Decimal("0.14782244")) <= tol
        # Smaller-leg invariant: IM == MM.
        assert snap.position_im == snap.position_mm

    def test_process_fill_emits_exactly_one_helper_call(self, runner, monkeypatch):
        """Test #10: ``_process_fill`` invokes ``_estimate_pair_im_mm``
        exactly once per fill.

        Guards against accidental double-emission or recomputation in
        future refactors.
        """
        runner.position_snapshot_callback = lambda snap: None
        runner.long_tracker.process_fill(
            side="Buy", qty=Decimal("6.2"), price=Decimal("55.91368848"),
        )
        runner.short_tracker.process_fill(
            side="Sell", qty=Decimal("2.2"), price=Decimal("55.53059536"),
        )
        runner._last_price = Decimal("53.70")
        runner._last_mark_price = Decimal("53.70")

        counter = {"n": 0}
        original = runner._estimate_pair_im_mm

        def counted(*args, **kwargs):
            counter["n"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(runner, "_estimate_pair_im_mm", counted)

        # Simulate one additional fill on long → one _emit call → one
        # _estimate_pair_im_mm call.
        from gridcore.events import ExecutionEvent
        evt = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol="LTCUSDT",
            exchange_ts=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
            local_ts=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
            exec_id="t-fill",
            order_id="t-fill",
            order_link_id="t-fill",
            side="Buy",
            price=Decimal("53.70"),
            qty=Decimal("0.1"),
            fee=Decimal("0"),
        )
        runner._process_fill(evt)

        assert counter["n"] == 1, (
            f"expected exactly one helper call per fill, got {counter['n']}"
        )
