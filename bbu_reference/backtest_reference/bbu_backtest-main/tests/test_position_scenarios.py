"""
Test Position Scenarios

Complex real-world position scenario tests including multi-funding periods,
margin tier transitions, near-liquidation situations, and partial closures.
"""

from datetime import datetime, timedelta

from src.bybit_calculations import BybitCalculator
from src.constants import COMMISSION_RATE
from src.enums import Direction, MarginMode
from src.position_tracker import PositionManager, PositionTracker


class TestPositionScenarios:
    """Test complex real-world position scenarios"""

    def setup_method(self):
        """Set up test fixtures"""
        self.calculator = BybitCalculator()
        self.base_time = datetime(2024, 1, 1, 0, 0, 0)  # Start at funding time

    def test_position_through_multiple_funding_periods(self):
        """Test position held through multiple 8-hour funding periods"""
        print("\n🧪 Testing position through multiple funding periods...")

        # Create long position
        tracker = PositionTracker(
            direction=Direction.LONG,
            symbol="BTCUSDT",
            leverage=10,
            commission_rate=COMMISSION_RATE
        )

        # Open position at 50k
        entry_time = self.base_time
        tracker.add_position(1.0, 50000, entry_time, "ENTRY_1")

        print("📊 Position opened: 1.0 BTC at $50,000")
        print(f"   Entry time: {entry_time}")

        # Simulate funding payments every 8 hours for 3 days (9 funding cycles)
        funding_rate = 0.0001  # 0.01% funding rate
        current_time = entry_time
        current_price = 50000

        funding_payments = []
        for i in range(9):  # 9 funding cycles (3 days)
            current_time += timedelta(hours=8)
            # Vary price slightly over time
            current_price += 500 if i % 2 == 0 else -300

            funding_payment = tracker.apply_funding_payment(
                funding_rate=funding_rate,
                current_price=current_price,
                timestamp=current_time
            )

            funding_payments.append({
                'cycle': i + 1,
                'time': current_time,
                'price': current_price,
                'payment': funding_payment,
                'cumulative': tracker.state.funding_payments
            })

            print(f"   Funding {i + 1}: ${funding_payment:.4f} (Price: ${current_price:,.0f})")

        # Verify funding payment calculations
        total_expected_funding = 0
        for payment_info in funding_payments:
            position_value = tracker.calculator.calculate_position_value(1.0, payment_info['price'])
            expected_payment = position_value * funding_rate
            total_expected_funding += expected_payment

            assert abs(payment_info['payment'] - expected_payment) < 0.01

        # Verify cumulative funding
        assert abs(tracker.state.funding_payments - total_expected_funding) < 0.01

        # Verify funding payments reduced realized PnL
        assert tracker.state.realized_pnl < 0  # Should be negative due to funding costs

        print(f"✅ Total funding paid over 9 cycles: ${tracker.state.funding_payments:.2f}")

    def test_margin_tier_transitions(self):
        """Test position growing through different maintenance margin tiers"""
        print("\n🧪 Testing margin tier transitions...")

        tracker = PositionTracker(
            direction=Direction.LONG,
            symbol="BTCUSDT",
            leverage=5,  # Lower leverage for larger positions
            commission_rate=COMMISSION_RATE
        )

        # BTCUSDT Tiers:
        # Tier 1: 0-2M (0.5% MMR)
        # Tier 2: 2M-10M (1% MMR)
        # Tier 3: 10M-20M (2.5% MMR)

        test_scenarios = [
            {"size": 10.0, "price": 50000, "expected_tier": 1, "expected_mmr": 0.005},    # 500k position (Tier 1)
            {"size": 20.0, "price": 60000, "expected_tier": 1, "expected_mmr": 0.005},    # 1.8M total (still Tier 1)
            {"size": 50.0, "price": 70000, "expected_tier": 2, "expected_mmr": 0.01},     # 5.6M total (Tier 2)
            {"size": 100.0, "price": 80000, "expected_tier": 3, "expected_mmr": 0.025},   # 14.4M total (Tier 3)
        ]

        cumulative_mm_values = []

        for i, scenario in enumerate(test_scenarios):
            # Add position entry
            entry_time = self.base_time + timedelta(hours=i)
            tracker.add_position(scenario["size"], scenario["price"], entry_time, f"ENTRY_{i + 1}")

            # Calculate current position metrics
            current_price = scenario["price"]
            position_value = tracker.calculator.calculate_position_value(tracker.state.total_size, current_price)
            maintenance_margin = tracker.calculate_maintenance_margin(current_price)

            # Get tier information
            tier_info = tracker.calculator.get_maintenance_margin_tier(position_value, "BTCUSDT")

            cumulative_mm_values.append({
                'entry': i + 1,
                'total_size': tracker.state.total_size,
                'position_value': position_value,
                'maintenance_margin': maintenance_margin,
                'mmr': tier_info['mmr'],
                'tier_mmr': scenario['expected_mmr']
            })

            print(f"📊 Entry {i + 1}: {scenario['size']} BTC at ${scenario['price']:,}")
            print(f"   Total size: {tracker.state.total_size} BTC")
            print(f"   Position value: ${position_value:,.0f}")
            print(f"   Maintenance margin: ${maintenance_margin:,.2f}")
            print(f"   MMR: {tier_info['mmr']:.3f} ({tier_info['mmr'] * 100:.1f}%)")

            # Verify we're in expected tier
            assert abs(tier_info['mmr'] - scenario['expected_mmr']) < 0.001

        # Verify tier progression
        assert cumulative_mm_values[0]['mmr'] == 0.005  # Tier 1
        assert cumulative_mm_values[1]['mmr'] == 0.005  # Still Tier 1
        assert cumulative_mm_values[2]['mmr'] == 0.01   # Tier 2
        assert cumulative_mm_values[3]['mmr'] == 0.025  # Tier 3

        print("✅ Successfully transitioned through margin tiers")

    def test_near_liquidation_scenarios(self):
        """Test positions approaching liquidation at various risk levels"""
        print("\n🧪 Testing near-liquidation scenarios...")

        scenarios = [
            {"name": "Safe Position", "entry": 50000, "current": 51000, "expected_risk": False},
            {"name": "Medium Risk", "entry": 50000, "current": 48000, "expected_risk": False},  # Still safe
            {"name": "High Risk", "entry": 50000, "current": 46000, "expected_risk": True},    # Higher risk
            {"name": "Near Liquidation", "entry": 50000, "current": 45000, "expected_risk": True}  # Very risky
        ]

        for scenario in scenarios:
            print(f"\n📊 Testing: {scenario['name']}")

            tracker = PositionTracker(
                direction=Direction.LONG,
                symbol="BTCUSDT",
                leverage=10,
                commission_rate=COMMISSION_RATE,
                margin_mode=MarginMode.ISOLATED
            )

            # Open position
            tracker.add_position(1.0, scenario['entry'], self.base_time, "TEST_ENTRY")

            # Calculate liquidation price
            wallet_balance = 10000
            liquidation_price = tracker.calculate_liquidation_price()
            unrealized_pnl = tracker.calculate_unrealized_pnl(scenario['current'])
            margin_ratio = tracker.calculate_margin_ratio(scenario['current'], wallet_balance)
            at_risk = tracker.is_position_at_risk(scenario['current'], wallet_balance, risk_threshold=0.1)

            print(f"   Entry: ${scenario['entry']:,}")
            print(f"   Current: ${scenario['current']:,}")
            print(f"   Liquidation: ${liquidation_price:,.2f}")
            print(f"   Distance to liq: ${scenario['current'] - liquidation_price:,.2f}")
            print(f"   Unrealized PnL: ${unrealized_pnl:,.2f}")
            print(f"   Margin ratio: {margin_ratio:.4f}")
            print(f"   At risk: {at_risk}")

            # Verify risk assessment matches expectation
            below_liquidation = scenario['current'] <= liquidation_price
            if scenario['expected_risk']:
                assert at_risk or margin_ratio < 0.15 or below_liquidation, f"Expected {scenario['name']} to be at risk"

            # Verify liquidation price is reasonable
            assert liquidation_price > 0
            assert liquidation_price < scenario['entry']  # Long position

            # Verify margin ratio calculation
            if unrealized_pnl > -8000:  # Not completely underwater
                assert margin_ratio is not None

        print("✅ Risk assessment scenarios completed")

    def test_partial_position_closures(self):
        """Test partial position closures and average price impacts"""
        print("\n🧪 Testing partial position closures...")

        tracker = PositionTracker(
            direction=Direction.LONG,
            symbol="BTCUSDT",
            leverage=10,
            commission_rate=COMMISSION_RATE
        )

        # Build up position with multiple entries
        entries = [
            {"size": 0.5, "price": 48000},
            {"size": 0.3, "price": 50000},
            {"size": 0.2, "price": 52000}
        ]

        for i, entry in enumerate(entries):
            realized_pnl = tracker.add_position(
                entry["size"], entry["price"],
                self.base_time + timedelta(hours=i), f"BUILD_{i + 1}"
            )

        # Calculate expected average price
        total_value = sum(e["size"] * e["price"] for e in entries)
        total_size = sum(e["size"] for e in entries)
        expected_avg_price = total_value / total_size

        print("📊 Built position:")
        print(f"   Total size: {total_size} BTC")
        print(f"   Expected avg price: ${expected_avg_price:,.2f}")
        print(f"   Actual avg price: ${tracker.state.average_entry_price:,.2f}")

        # Verify average price calculation
        assert abs(tracker.state.average_entry_price - expected_avg_price) < 0.01

        # Test partial closures at different prices
        closures = [
            {"size": 0.2, "price": 55000, "expected_profit": True},   # Profitable close
            {"size": 0.3, "price": 47000, "expected_profit": False},  # Loss close
            {"size": 0.5, "price": 51000, "expected_profit": True}   # Final close (profit)
        ]

        for i, closure in enumerate(closures):
            print(f"\n📉 Partial closure {i + 1}:")

            before_size = tracker.state.total_size
            before_avg = tracker.state.average_entry_price

            realized_pnl = tracker.reduce_position(
                closure["size"], closure["price"],
                self.base_time + timedelta(days=1, hours=i), f"CLOSE_{i + 1}"
            )

            after_size = tracker.state.total_size
            after_avg = tracker.state.average_entry_price

            print(f"   Closed: {closure['size']} BTC at ${closure['price']:,}")
            print(f"   Realized PnL: ${realized_pnl:,.2f}")
            print(f"   Size: {before_size:.1f} → {after_size:.1f}")
            print(f"   Avg price: ${before_avg:,.2f} → ${after_avg:,.2f}")

            # Verify profit/loss expectation
            if closure["expected_profit"]:
                assert realized_pnl > 0, f"Expected profit for closure {i + 1}"
            else:
                assert realized_pnl < 0, f"Expected loss for closure {i + 1}"

            # Verify average price stays the same for remaining position
            if after_size > 0:
                assert abs(after_avg - before_avg) < 0.01, "Average price should not change"

        # Verify final state
        assert tracker.state.total_size == 0, "Position should be fully closed"

        print("✅ Partial closure scenarios completed")

    def test_mixed_long_short_positions(self):
        """Test position manager with both long and short positions"""
        print("\n🧪 Testing mixed long/short positions...")

        manager = PositionManager(commission_rate=COMMISSION_RATE)

        # Build long position
        long_entries = [
            {"size": 0.5, "price": 50000},
            {"size": 0.3, "price": 52000}
        ]

        for i, entry in enumerate(long_entries):
            manager.get_tracker(Direction.LONG).add_position(
                entry["size"], entry["price"],
                self.base_time + timedelta(hours=i), f"LONG_{i + 1}"
            )

        # Build short position
        short_entries = [
            {"size": 0.4, "price": 51000},
            {"size": 0.6, "price": 49000}
        ]

        for i, entry in enumerate(short_entries):
            manager.get_tracker(Direction.SHORT).add_position(
                entry["size"], entry["price"],
                self.base_time + timedelta(hours=i + 2), f"SHORT_{i + 1}"
            )

        # Test at various market prices
        test_prices = [48000, 50000, 52000, 54000]

        for price in test_prices:
            print(f"\n📊 Market Price: ${price:,}")

            combined_pnl = manager.get_combined_pnl(price)

            long_unrealized = manager.long_tracker.calculate_unrealized_pnl(price)
            short_unrealized = manager.short_tracker.calculate_unrealized_pnl(price)

            print(f"   Long PnL: ${long_unrealized:,.2f}")
            print(f"   Short PnL: ${short_unrealized:,.2f}")
            print(f"   Combined PnL: ${combined_pnl['total_unrealized_pnl']:,.2f}")
            print(f"   Net position: {combined_pnl['net_position_size']:.1f} BTC")

            # Verify combined calculations
            expected_total = long_unrealized + short_unrealized
            assert abs(combined_pnl['total_unrealized_pnl'] - expected_total) < 0.01

            # Net position should be long_size - short_size
            expected_net = 0.8 - 1.0  # -0.2 (net short)
            assert abs(combined_pnl['net_position_size'] - expected_net) < 0.01

        print("✅ Mixed position scenarios completed")

    def test_cross_margin_scenarios(self):
        """Test cross margin calculations with available balance"""
        print("\n🧪 Testing cross margin scenarios...")

        tracker = PositionTracker(
            direction=Direction.LONG,
            symbol="BTCUSDT",
            leverage=10,
            margin_mode=MarginMode.CROSS,
            commission_rate=COMMISSION_RATE
        )

        # Open position
        tracker.add_position(2.0, 50000, self.base_time, "CROSS_ENTRY")

        # Test liquidation price with different available balances
        test_balances = [0, 5000, 10000, 20000]

        print("📊 Cross margin liquidation prices:")
        print("   Position: 2.0 BTC at $50,000 (10x leverage)")

        isolated_liq = None
        cross_liq_prices = []

        for balance in test_balances:
            if balance == 0:
                # This is essentially isolated margin
                liq_price = tracker.calculate_liquidation_price(balance)
                isolated_liq = liq_price
                print(f"   Isolated (balance=0): ${liq_price:,.2f}")
            else:
                liq_price = tracker.calculate_liquidation_price(balance)
                cross_liq_prices.append(liq_price)
                print(f"   Cross (balance=${balance:,}): ${liq_price:,.2f}")

        # Verify cross margin liquidation prices are lower (safer) than isolated
        for cross_liq in cross_liq_prices:
            assert cross_liq < isolated_liq, "Cross margin should have lower liquidation price"

        # Verify higher balance = lower liquidation price (safer)
        assert cross_liq_prices[0] > cross_liq_prices[1] > cross_liq_prices[2]

        print("✅ Cross margin scenarios completed")

    def test_high_frequency_trading_simulation(self):
        """Test position through high-frequency entry/exit cycles"""
        print("\n🧪 Testing high-frequency trading simulation...")

        tracker = PositionTracker(
            direction=Direction.LONG,
            symbol="BTCUSDT",
            leverage=20,  # High leverage for scalping
            commission_rate=COMMISSION_RATE
        )

        # Simulate 50 rapid trades over 2 hours
        base_price = 50000
        num_trades = 50
        total_commission = 0
        total_realized_pnl = 0

        for i in range(num_trades):
            # Alternate between entries and partial exits
            if i % 3 == 0:  # Entry every 3rd trade
                size = 0.1
                price = base_price + (i * 10)  # Gradually increasing price

                tracker.add_position(
                    size, price,
                    self.base_time + timedelta(minutes=i * 2), f"HFT_ENTRY_{i}"
                )

                commission = size * price * COMMISSION_RATE
                total_commission += commission

            elif tracker.state.total_size > 0.1:  # Partial exit if position exists
                exit_size = min(0.05, tracker.state.total_size / 2)
                exit_price = base_price + (i * 12)  # Slightly higher exit

                pnl = tracker.reduce_position(
                    exit_size, exit_price,
                    self.base_time + timedelta(minutes=i * 2), f"HFT_EXIT_{i}"
                )
                total_realized_pnl += pnl  # Only accumulate PnL from exits

                commission = exit_size * exit_price * COMMISSION_RATE
                total_commission += commission

        print("📊 High-frequency trading results:")
        print(f"   Total trades: {num_trades}")
        print(f"   Final position size: {tracker.state.total_size:.3f} BTC")
        print(f"   Final avg price: ${tracker.state.average_entry_price:,.2f}")
        print(f"   Total realized PnL: ${tracker.state.realized_pnl:,.2f}")
        print(f"   Total commission paid: ${total_commission:.2f}")
        print(f"   Entry count: {len([e for e in tracker.state.entries if e.is_increase])}")
        print(f"   Exit count: {len([e for e in tracker.state.entries if not e.is_increase])}")

        # Verify commission tracking
        expected_commission = tracker.state.commission_paid
        assert abs(total_commission - expected_commission) < 0.01

        # Verify realized PnL tracking
        assert abs(total_realized_pnl - tracker.state.realized_pnl) < 0.01

        # Verify entry/exit tracking (should be close to num_trades but may vary due to conditions)
        assert len(tracker.state.entries) > 0
        assert len(tracker.state.entries) <= num_trades

        print("✅ High-frequency trading simulation completed")

    def test_stress_test_extreme_scenarios(self):
        """Stress test with extreme market conditions"""
        print("\n🧪 Testing extreme stress scenarios...")

        # Test 1: Flash crash scenario
        print("\n⚡ Flash crash scenario:")
        tracker = PositionTracker(Direction.LONG, symbol="BTCUSDT", leverage=10)
        tracker.add_position(1.0, 50000, self.base_time, "CRASH_ENTRY")

        # Simulate flash crash - 20% drop in minutes
        crash_prices = [48000, 45000, 42000, 40000]
        liq_price = tracker.calculate_liquidation_price()

        for i, price in enumerate(crash_prices):
            unrealized_pnl = tracker.calculate_unrealized_pnl(price)

            print(f"   Price ${price:,}: PnL ${unrealized_pnl:,}, Liq ${liq_price:,.0f}")

            # Check if position would be liquidated
            liquidated = price <= liq_price
            print(f"   Liquidated: {liquidated}")

            if not liquidated:
                assert liq_price > 0 and liq_price < price

        # Test 2: Extreme leverage scenario
        print("\n💥 Extreme leverage scenario:")
        extreme_tracker = PositionTracker(Direction.LONG, symbol="BTCUSDT", leverage=100)
        extreme_tracker.add_position(0.1, 50000, self.base_time, "EXTREME_ENTRY")

        # Even small price moves should have huge impact
        small_move_pnl = extreme_tracker.calculate_unrealized_pnl(50500)  # $500 move
        liq_price = extreme_tracker.calculate_liquidation_price()

        print(f"   100x leverage, $500 move: PnL ${small_move_pnl:.2f}")
        print(f"   Liquidation price: ${liq_price:,.2f}")
        print(f"   Distance to liquidation: ${50000 - liq_price:.2f}")

        # With 100x leverage, liquidation should be very close to entry
        assert (50000 - liq_price) < 300  # Less than $300 from entry

        # Test 3: Maximum position value (tier boundary testing)
        print("\n🏔️  Maximum position value scenario:")
        max_tracker = PositionTracker(Direction.LONG, symbol="BTCUSDT", leverage=2)

        # Create position worth 50M (highest tier)
        huge_size = 1000  # 1000 BTC
        max_tracker.add_position(huge_size, 50000, self.base_time, "MAX_ENTRY")

        position_value = huge_size * 50000  # 50M
        mm, mmr = max_tracker.calculator.calculate_maintenance_margin(position_value, "BTCUSDT")

        print(f"   Position value: ${position_value:,}")
        print(f"   Maintenance margin: ${mm:,.2f}")
        print(f"   MMR: {mmr:.1%}")

        # Should be in highest tier
        assert mmr >= 0.1  # 10% MMR for highest tier

        print("✅ Extreme stress scenarios completed")


def run_all_scenario_tests():
    """Run all position scenario tests with detailed output"""
    print("🚀 Position Scenario Test Suite")
    print("=" * 80)

    test_instance = TestPositionScenarios()
    test_instance.setup_method()

    scenarios = [
        ("Funding Periods", test_instance.test_position_through_multiple_funding_periods),
        ("Margin Tiers", test_instance.test_margin_tier_transitions),
        ("Near Liquidation", test_instance.test_near_liquidation_scenarios),
        ("Partial Closures", test_instance.test_partial_position_closures),
        ("Mixed Positions", test_instance.test_mixed_long_short_positions),
        ("Cross Margin", test_instance.test_cross_margin_scenarios),
        ("High Frequency", test_instance.test_high_frequency_trading_simulation),
        ("Stress Tests", test_instance.test_stress_test_extreme_scenarios)
    ]

    passed_tests = 0
    for name, test_func in scenarios:
        try:
            test_func()
            print(f"✅ {name} - PASSED")
            passed_tests += 1
        except Exception as e:
            print(f"❌ {name} - FAILED: {e}")

    print(f"\n🎉 Scenario Tests Completed: {passed_tests}/{len(scenarios)} passed")


if __name__ == "__main__":
    run_all_scenario_tests()