"""Tests for backtest runner."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gridcore import TickerEvent, EventType, PlaceLimitIntent, DirectionType, SideType

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
        executor = BacktestExecutor(order_manager=order_mgr)
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
        executor = BacktestExecutor(order_manager=order_mgr)
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

    def test_risk_disabled_no_qty_calculator(self, no_risk_runner):
        """Risk-disabled runner leaves qty_calculator as None."""
        assert no_risk_runner._executor.qty_calculator is None

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
        """Long liq price: entry * (1 - 1/lev + mmr)."""
        entry = Decimal("100000")
        liq = risk_runner._estimate_liquidation_price(entry, DirectionType.LONG)
        # 100000 * (1 - 0.1 + 0.005) = 100000 * 0.905 = 90500
        assert liq == Decimal("90500")

    def test_estimate_liq_price_short(self, risk_runner):
        """Short liq price: entry * (1 + 1/lev - mmr)."""
        entry = Decimal("100000")
        liq = risk_runner._estimate_liquidation_price(entry, DirectionType.SHORT)
        # 100000 * (1 + 0.1 - 0.005) = 100000 * 1.095 = 109500
        assert liq == Decimal("109500")

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

        state = risk_runner._build_position_state(
            risk_runner._long_tracker, Decimal("10000"), DirectionType.LONG
        )
        assert state.size == Decimal("0.1")
        assert state.entry_price == Decimal("100000")
        # position_value = 0.1 * 100000 = 10000, margin = 10000/10000 = 1.0
        assert state.margin == Decimal("1")
        assert state.position_value == Decimal("10000")
        assert state.liquidation_price == Decimal("90500")  # long liq
        assert state.leverage == 10

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
