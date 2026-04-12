"""Tests for backtest runner."""

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gridcore import TickerEvent, EventType, PlaceLimitIntent, DirectionType, SideType
from gridcore.instrument_info import InstrumentInfo
from gridcore.pnl import calc_maintenance_margin

from backtest.runner import BacktestRunner
from backtest.config import BacktestStrategyConfig
from backtest.fill_simulator import TradeThroughFillSimulator
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

    def test_estimate_liq_price_long(self, risk_runner):
        """Long liq price: cross-margin formula."""
        entry = Decimal("100000")
        # qty = pv / entry = 10000 / 100000 = 0.1
        # mm = 10000 * 0.005 = 50 (tier 1 from cache, small position)
        # liq = (0.1 * 100000 - 10000 + 50) / 0.1 = 500 / 0.1 = 5000
        # With large wallet, liq is very low (far from liquidation) — correct for cross margin
        pv = Decimal("10000")
        wallet = Decimal("10000")
        liq = risk_runner._estimate_liquidation_price(entry, DirectionType.LONG, pv, wallet)
        qty = pv / entry  # 0.1
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected = (qty * entry - wallet + mm) / qty
        assert liq == expected

    def test_estimate_liq_price_short(self, risk_runner):
        """Short liq price: cross-margin formula."""
        entry = Decimal("100000")
        pv = Decimal("10000")
        wallet = Decimal("10000")
        liq = risk_runner._estimate_liquidation_price(entry, DirectionType.SHORT, pv, wallet)
        qty = pv / entry  # 0.1
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected = (qty * entry + wallet - mm) / qty
        assert liq == expected

    def test_estimate_liq_price_tiered_large_position(self, risk_runner):
        """Large position uses tiered MM from cache in cross-margin formula."""
        entry = Decimal("100000")
        # position_value=5M → cache tier: mmr=0.0077, ded=8460
        # mm_amount = 5M * 0.0077 - 8460 = 30040
        pv = Decimal("5000000")
        # Wallet smaller than position → meaningful liq price
        wallet = Decimal("600000")
        liq = risk_runner._estimate_liquidation_price(entry, DirectionType.LONG, pv, wallet)
        qty = pv / entry  # 50
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected = (qty * entry - wallet + mm) / qty
        assert liq == expected
        assert liq > 0  # liq price is positive
        assert liq < entry  # liq price below entry for long

    def test_estimate_liq_price_tiered_short_large(self, risk_runner):
        """Large short position uses tiered MM in cross-margin formula."""
        entry = Decimal("100000")
        pv = Decimal("5000000")
        wallet = Decimal("600000")
        liq = risk_runner._estimate_liquidation_price(entry, DirectionType.SHORT, pv, wallet)
        qty = pv / entry  # 50
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected = (qty * entry + wallet - mm) / qty
        assert liq == expected
        assert liq > entry  # liq price above entry for short

    def test_estimate_liq_price_falls_back_to_flat_mmr(self):
        """When no tiers loaded, falls back to flat maintenance_margin_rate."""
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
        # Force no tiers
        runner._mm_tiers = None
        entry = Decimal("100000")
        pv = Decimal("10000")
        wallet = Decimal("10000")
        liq = runner._estimate_liquidation_price(entry, DirectionType.LONG, pv, wallet)
        # qty = 0.1, mm = 10000 * 0.008 = 80
        # liq = (0.1 * 100000 - 10000 + 80) / 0.1 = 80 / 0.1 = 800
        qty = pv / entry
        mm = pv * Decimal("0.008")
        expected = (qty * entry - wallet + mm) / qty
        assert liq == expected

    def test_estimate_liq_price_with_hardcoded_tiers(self):
        """When cache file missing, hardcoded tiers are used with cross-margin formula."""
        from backtest.session import BacktestSession
        config = BacktestStrategyConfig(
            strat_id="test_hc",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            leverage=10,
            maintenance_margin_rate=0.005,
            enable_risk_multipliers=True,
            risk_limits_cache_path="/nonexistent/path.json",
        )
        session = BacktestSession(session_id="test_hc", initial_balance=Decimal("10000"))
        fill_sim = TradeThroughFillSimulator()
        order_mgr = BacktestOrderManager(
            fill_simulator=fill_sim, commission_rate=config.commission_rate,
        )
        executor = BacktestExecutor(order_manager=order_mgr)
        runner = BacktestRunner(strategy_config=config, executor=executor, session=session)
        # Should have fallen back to hardcoded BTCUSDT tiers
        assert runner._mm_tiers is not None
        # 5M → hardcoded tier 2: mmr=0.01, ded=10000
        # mm=5M*0.01-10000=40000
        entry = Decimal("100000")
        pv = Decimal("5000000")
        wallet = Decimal("600000")
        liq = runner._estimate_liquidation_price(entry, DirectionType.LONG, pv, wallet)
        qty = pv / entry  # 50
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=runner._mm_tiers)
        expected = (qty * entry - wallet + mm) / qty
        assert liq == expected
        assert liq > 0

    def test_build_position_state_uses_tiered_liq(self, risk_runner):
        """_build_position_state passes position_value and wallet to cross-margin liq."""
        # 50 BTC at 100000 = 5M position_value → cache tier with deduction
        risk_runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("50"), price=Decimal("100000")
        )
        wallet = Decimal("600000")
        state = risk_runner._build_position_state(
            risk_runner._long_tracker, wallet, DirectionType.LONG
        )
        pv = Decimal("5000000")
        qty = Decimal("50")
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected_liq = (qty * Decimal("100000") - wallet + mm) / qty
        assert state.liquidation_price == expected_liq
        assert state.liquidation_price > 0

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
            risk_runner._long_tracker, Decimal("10000"), DirectionType.LONG
        )
        assert state.size == Decimal("0")
        assert state.margin == Decimal("0")
        assert state.liquidation_price == Decimal("0")

    def test_build_position_state_with_position(self, risk_runner):
        """Tracker with position produces correct PositionState."""
        # Manually add a position
        risk_runner._long_tracker.process_fill(
            side=SideType.BUY, qty=Decimal("0.1"), price=Decimal("100000")
        )

        wallet = Decimal("10000")
        state = risk_runner._build_position_state(
            risk_runner._long_tracker, wallet, DirectionType.LONG
        )
        assert state.size == Decimal("0.1")
        assert state.entry_price == Decimal("100000")
        # position_value = 0.1 * 100000 = 10000, margin = 10000/10000 = 1.0
        assert state.margin == Decimal("1")
        assert state.position_value == Decimal("10000")
        # Cross-margin: liq = (qty*entry - wallet + mm) / qty
        pv = Decimal("10000")
        qty = pv / Decimal("100000")  # 0.1
        mm, _ = calc_maintenance_margin(pv, "BTCUSDT", tiers=risk_runner._mm_tiers)
        expected_liq = (qty * Decimal("100000") - wallet + mm) / qty
        assert state.liquidation_price == expected_liq
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
                risk_runner._long_tracker, Decimal("0"), DirectionType.LONG
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

        def capture_price(price):
            called_with.append(price)
            return original(price)

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
