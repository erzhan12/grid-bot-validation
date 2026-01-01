"""
Bybit Validation Framework

Comprehensive validation system for comparing our Bybit calculations
against known values and expected behaviors.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List

from src.bybit_calculations import BybitCalculator
from src.enums import Direction, MarginMode
from src.position_tracker import PositionTracker


@dataclass
class ValidationScenario:
    """Test scenario for validation"""
    name: str
    description: str
    direction: Direction
    entry_price: float
    contract_qty: float
    leverage: float
    symbol: str = "BTCUSDT"
    margin_mode: MarginMode = MarginMode.ISOLATED
    expected_results: Dict[str, float] = None
    tolerance: float = 0.001  # 0.1% tolerance


@dataclass
class ValidationResult:
    """Result of a validation test"""
    scenario_name: str
    metric: str
    expected: float
    actual: float
    difference: float
    percentage_error: float
    passed: bool
    tolerance: float


class BybitValidator:
    """Validate calculations against Bybit's official values"""

    def __init__(self):
        self.calculator = BybitCalculator()
        self.results: List[ValidationResult] = []
        self.scenarios: List[ValidationScenario] = []

    def add_scenario(self, scenario: ValidationScenario):
        """Add a validation scenario"""
        self.scenarios.append(scenario)

    def load_scenarios_from_file(self, file_path: str):
        """Load validation scenarios from JSON file"""
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
                for scenario_data in data['scenarios']:
                    scenario = ValidationScenario(
                        name=scenario_data['name'],
                        description=scenario_data['description'],
                        direction=Direction(scenario_data['direction']),
                        entry_price=scenario_data['entry_price'],
                        contract_qty=scenario_data['contract_qty'],
                        leverage=scenario_data['leverage'],
                        symbol=scenario_data.get('symbol', 'BTCUSDT'),
                        margin_mode=MarginMode(scenario_data.get('margin_mode', MarginMode.ISOLATED.value)),
                        expected_results=scenario_data['expected_results'],
                        tolerance=scenario_data.get('tolerance', 0.001)
                    )
                    self.add_scenario(scenario)
        except FileNotFoundError:
            print(f"Validation file {file_path} not found. Creating default scenarios.")
            self._create_default_scenarios()

    def _create_default_scenarios(self):
        """Create default validation scenarios with known Bybit values"""
        default_scenarios = [
            ValidationScenario(
                name="Basic Long 10x",
                description="Basic long position with 10x leverage",
                direction=Direction.LONG,
                entry_price=50000,
                contract_qty=1.0,
                leverage=10,
                expected_results={
                    "initial_margin": 5000.0,
                    "liquidation_price": 49723.76,
                    "bankruptcy_price": 45000.0,
                    "maintenance_margin": 250.0,
                    "position_value": 50000.0
                }
            ),
            ValidationScenario(
                name="Basic Short 10x",
                description="Basic short position with 10x leverage",
                direction=Direction.SHORT,
                entry_price=50000,
                contract_qty=1.0,
                leverage=10,
                expected_results={
                    "initial_margin": 5000.0,
                    "liquidation_price": 50276.24,
                    "bankruptcy_price": 55000.0,
                    "maintenance_margin": 250.0,
                    "position_value": 50000.0
                }
            ),
            ValidationScenario(
                name="High Leverage Long 50x",
                description="High leverage long position",
                direction=Direction.LONG,
                entry_price=50000,
                contract_qty=0.5,
                leverage=50,
                expected_results={
                    "initial_margin": 500.0,
                    "liquidation_price": 49494.95,
                    "bankruptcy_price": 49000.0,
                    "position_value": 25000.0
                }
            ),
            ValidationScenario(
                name="Large Position Tier 2",
                description="Large position in maintenance margin tier 2",
                direction=Direction.LONG,
                entry_price=50000,
                contract_qty=100.0,  # 5M position value
                leverage=5,
                symbol="BTCUSDT",
                expected_results={
                    "position_value": 5000000.0,
                    "maintenance_margin": 40000.0,  # (5M * 0.01) - 10k
                    "initial_margin": 1000000.0
                }
            )
        ]

        for scenario in default_scenarios:
            self.add_scenario(scenario)

    def validate_scenario(self, scenario: ValidationScenario) -> List[ValidationResult]:
        """Validate a single scenario against expected results"""
        scenario_results = []

        # Calculate actual values using our implementation
        actual_values = self._calculate_scenario_values(scenario)

        # Compare each expected result
        if scenario.expected_results:
            for metric, expected in scenario.expected_results.items():
                if metric in actual_values:
                    actual = actual_values[metric]
                    difference = actual - expected
                    percentage_error = abs(difference / expected) * 100 if expected != 0 else float('inf')
                    passed = percentage_error <= (scenario.tolerance * 100)

                    result = ValidationResult(
                        scenario_name=scenario.name,
                        metric=metric,
                        expected=expected,
                        actual=actual,
                        difference=difference,
                        percentage_error=percentage_error,
                        passed=passed,
                        tolerance=scenario.tolerance
                    )

                    scenario_results.append(result)
                    self.results.append(result)

        return scenario_results

    def _calculate_scenario_values(self, scenario: ValidationScenario) -> Dict[str, float]:
        """Calculate all relevant values for a scenario"""
        values = {}

        # Basic calculations
        values["position_value"] = self.calculator.calculate_position_value(
            scenario.contract_qty, scenario.entry_price
        )

        values["initial_margin"] = self.calculator.calculate_initial_margin(
            values["position_value"], scenario.leverage
        )

        maintenance_margin, mmr = self.calculator.calculate_maintenance_margin(
            values["position_value"], scenario.symbol
        )
        values["maintenance_margin"] = maintenance_margin
        values["maintenance_margin_rate"] = mmr

        values["liquidation_price"] = self.calculator.calculate_liquidation_price(
            direction=scenario.direction,
            entry_price=scenario.entry_price,
            contract_qty=scenario.contract_qty,
            leverage=scenario.leverage,
            margin_mode=scenario.margin_mode,
            symbol=scenario.symbol
        )

        values["bankruptcy_price"] = self.calculator.calculate_bankruptcy_price(
            direction=scenario.direction,
            entry_price=scenario.entry_price,
            contract_qty=scenario.contract_qty,
            leverage=scenario.leverage
        )

        # PnL calculations at various price points
        test_prices = [
            scenario.entry_price * 0.95,  # 5% down
            scenario.entry_price,         # At entry
            scenario.entry_price * 1.05   # 5% up
        ]

        for i, price in enumerate(test_prices):
            suffix = ["down5", "entry", "up5"][i]
            values[f"unrealized_pnl_{suffix}"] = self.calculator.calculate_unrealized_pnl(
                scenario.direction, scenario.contract_qty, scenario.entry_price, price
            )

        # Funding calculations
        values["funding_payment_0001"] = self.calculator.calculate_funding_payment(
            values["position_value"], 0.0001
        )

        # Order cost calculations
        values["order_cost_taker"] = self.calculator.calculate_order_cost(
            values["position_value"], scenario.leverage, is_maker=False
        )
        values["order_cost_maker"] = self.calculator.calculate_order_cost(
            values["position_value"], scenario.leverage, is_maker=True
        )

        return values

    def validate_all_scenarios(self) -> Dict[str, Any]:
        """Validate all scenarios and return summary"""
        if not self.scenarios:
            self._create_default_scenarios()

        summary = {
            "total_scenarios": len(self.scenarios),
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "scenarios": {}
        }

        for scenario in self.scenarios:
            print(f"\n🧪 Validating: {scenario.name}")
            print(f"   {scenario.description}")

            scenario_results = self.validate_scenario(scenario)
            passed_count = sum(1 for r in scenario_results if r.passed)
            failed_count = len(scenario_results) - passed_count

            summary["total_tests"] += len(scenario_results)
            summary["passed_tests"] += passed_count
            summary["failed_tests"] += failed_count

            scenario_summary = {
                "description": scenario.description,
                "tests": len(scenario_results),
                "passed": passed_count,
                "failed": failed_count,
                "pass_rate": (passed_count / len(scenario_results)) * 100 if scenario_results else 0,
                "results": [asdict(r) for r in scenario_results]
            }

            summary["scenarios"][scenario.name] = scenario_summary

            # Print immediate results
            for result in scenario_results:
                status = "✅" if result.passed else "❌"
                print(f"   {status} {result.metric}: Expected={result.expected:.2f}, "
                      f"Actual={result.actual:.2f}, Error={result.percentage_error:.3f}%")

        # Overall statistics
        summary["overall_pass_rate"] = (summary["passed_tests"] / summary["total_tests"]) * 100 if summary["total_tests"] > 0 else 0

        return summary

    def validate_against_position_tracker(self, scenario: ValidationScenario) -> Dict[str, Any]:
        """Validate scenario using PositionTracker for integration testing"""
        tracker = PositionTracker(
            direction=scenario.direction,
            symbol=scenario.symbol,
            leverage=scenario.leverage,
            margin_mode=scenario.margin_mode
        )

        # Add position
        tracker.add_position(
            size=scenario.contract_qty,
            price=scenario.entry_price,
            timestamp=datetime.now(),
            order_id="VALIDATION_TEST"
        )

        # Get comprehensive summary
        wallet_balance = 10000  # Assume 10k wallet
        summary = tracker.get_comprehensive_summary(
            current_price=scenario.entry_price,
            wallet_balance=wallet_balance
        )

        # Compare key metrics
        comparison = {
            "calculator_vs_tracker": {},
            "differences": {},
            "max_difference": 0.0
        }

        calc_values = self._calculate_scenario_values(scenario)

        # Compare common metrics
        common_metrics = [
            "position_value", "initial_margin", "liquidation_price",
            "bankruptcy_price", "maintenance_margin"
        ]

        for metric in common_metrics:
            if metric in calc_values and metric in summary:
                calc_val = calc_values[metric]
                tracker_val = summary[metric]
                diff = abs(calc_val - tracker_val)
                comparison["calculator_vs_tracker"][metric] = {
                    "calculator": calc_val,
                    "tracker": tracker_val,
                    "difference": diff,
                    "percentage_diff": (diff / calc_val) * 100 if calc_val != 0 else 0
                }
                comparison["max_difference"] = max(comparison["max_difference"], diff)

        return comparison

    def generate_validation_report(self) -> str:
        """Generate a comprehensive validation report"""
        if not self.results:
            return "No validation results available. Run validate_all_scenarios() first."

        report = []
        report.append("=" * 80)
        report.append("🎯 BYBIT CALCULATION VALIDATION REPORT")
        report.append("=" * 80)
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        # Summary statistics
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r.passed)
        failed_tests = total_tests - passed_tests
        pass_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0

        report.append("📊 SUMMARY")
        report.append(f"Total Tests: {total_tests}")
        report.append(f"Passed: {passed_tests}")
        report.append(f"Failed: {failed_tests}")
        report.append(f"Pass Rate: {pass_rate:.2f}%")
        report.append("")

        # Group results by scenario
        scenario_groups = {}
        for result in self.results:
            if result.scenario_name not in scenario_groups:
                scenario_groups[result.scenario_name] = []
            scenario_groups[result.scenario_name].append(result)

        # Detailed results by scenario
        for scenario_name, results in scenario_groups.items():
            report.append(f"🧪 {scenario_name}")
            report.append("-" * 60)

            scenario_passed = sum(1 for r in results if r.passed)
            scenario_total = len(results)
            scenario_rate = (scenario_passed / scenario_total) * 100

            report.append(f"Pass Rate: {scenario_rate:.2f}% ({scenario_passed}/{scenario_total})")
            report.append("")

            for result in results:
                status = "PASS" if result.passed else "FAIL"
                report.append(f"  {result.metric:<25} {status:<5} "
                            f"Expected: {result.expected:>12.2f} "
                            f"Actual: {result.actual:>12.2f} "
                            f"Error: {result.percentage_error:>6.3f}%")

            report.append("")

        # Failed tests summary
        failed_results = [r for r in self.results if not r.passed]
        if failed_results:
            report.append("❌ FAILED TESTS DETAILS")
            report.append("-" * 60)
            for result in failed_results:
                report.append(f"Scenario: {result.scenario_name}")
                report.append(f"  Metric: {result.metric}")
                report.append(f"  Expected: {result.expected:.6f}")
                report.append(f"  Actual: {result.actual:.6f}")
                report.append(f"  Difference: {result.difference:+.6f}")
                report.append(f"  Error: {result.percentage_error:.3f}%")
                report.append(f"  Tolerance: {result.tolerance:.3f}")
                report.append("")

        # Accuracy analysis
        if self.results:
            errors = [r.percentage_error for r in self.results]
            avg_error = sum(errors) / len(errors)
            max_error = max(errors)
            min_error = min(errors)

            report.append("📈 ACCURACY ANALYSIS")
            report.append("-" * 60)
            report.append(f"Average Error: {avg_error:.3f}%")
            report.append(f"Maximum Error: {max_error:.3f}%")
            report.append(f"Minimum Error: {min_error:.3f}%")
            report.append("")

        report.append("=" * 80)
        report.append("🎉 VALIDATION COMPLETE")
        report.append("=" * 80)

        return "\n".join(report)

    def export_results_to_json(self, file_path: str):
        """Export validation results to JSON file"""
        export_data = {
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "total_tests": len(self.results),
                "passed_tests": sum(1 for r in self.results if r.passed),
                "failed_tests": sum(1 for r in self.results if not r.passed),
                "pass_rate": (sum(1 for r in self.results if r.passed) / len(self.results)) * 100 if self.results else 0
            },
            "scenarios": [asdict(s) for s in self.scenarios],
            "results": [asdict(r) for r in self.results]
        }

        with open(file_path, 'w') as f:
            json.dump(export_data, f, indent=2)

        print(f"📄 Validation results exported to {file_path}")

    def clear_results(self):
        """Clear all validation results"""
        self.results.clear()


# Example usage and test scenarios
def create_comprehensive_test_scenarios() -> List[ValidationScenario]:
    """Create comprehensive test scenarios covering various conditions"""
    scenarios = [
        # Basic scenarios
        ValidationScenario(
            name="Long_10x_BTCUSDT",
            description="Standard long position on BTCUSDT with 10x leverage",
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=1.0,
            leverage=10,
            symbol="BTCUSDT",
            expected_results={
                "position_value": 50000.0,
                "initial_margin": 5000.0,
                "maintenance_margin": 250.0
            }
        ),

        # High leverage scenarios
        ValidationScenario(
            name="Long_100x_Extreme",
            description="Extreme leverage long position",
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=0.1,
            leverage=100,
            expected_results={
                "position_value": 5000.0,
                "initial_margin": 50.0
            }
        ),

        # Large position scenarios (tier testing)
        ValidationScenario(
            name="Large_Position_Tier3",
            description="Large position testing tier 3 maintenance margin",
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=300.0,  # 15M position
            leverage=5,
            symbol="BTCUSDT",
            expected_results={
                "position_value": 15000000.0,
                "initial_margin": 3000000.0
            }
        ),

        # Cross margin scenarios
        ValidationScenario(
            name="Cross_Margin_Long",
            description="Cross margin long position",
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=2.0,
            leverage=10,
            margin_mode=MarginMode.CROSS,
            expected_results={
                "position_value": 100000.0,
                "initial_margin": 10000.0
            }
        ),

        # Different symbols
        ValidationScenario(
            name="ETHUSDT_Long_20x",
            description="ETH position with 20x leverage",
            direction=Direction.LONG,
            entry_price=3000,
            contract_qty=10.0,
            leverage=20,
            symbol="ETHUSDT",
            expected_results={
                "position_value": 30000.0,
                "initial_margin": 1500.0
            }
        )
    ]

    return scenarios


if __name__ == "__main__":
    # Example usage
    validator = BybitValidator()

    # Load or create scenarios
    validator._create_default_scenarios()

    # Run validation
    summary = validator.validate_all_scenarios()

    # Generate report
    report = validator.generate_validation_report()
    print(report)

    # Export results
    validator.export_results_to_json("validation_results.json")