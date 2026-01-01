#!/usr/bin/env python3
"""
Bybit Calculations Validation Script

Comprehensive validation of all Bybit calculations against known values
and expected behaviors. Run this script to validate accuracy.

Usage:
    python scripts/validate_calculations.py
    python scripts/validate_calculations.py --export-json
    python scripts/validate_calculations.py --scenarios-file validation_scenarios.json
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from src.enums import Direction  # noqa: E402
from tests.validation.bybit_validator import BybitValidator, ValidationScenario, create_comprehensive_test_scenarios  # noqa: E402


def run_basic_validation():
    """Run basic validation with default scenarios"""
    print("🚀 Running Basic Bybit Calculations Validation")
    print("=" * 80)

    validator = BybitValidator()

    # Run validation
    summary = validator.validate_all_scenarios()

    # Print summary
    print("\n📊 VALIDATION SUMMARY")
    print(f"Total Scenarios: {summary['total_scenarios']}")
    print(f"Total Tests: {summary['total_tests']}")
    print(f"Passed: {summary['passed_tests']}")
    print(f"Failed: {summary['failed_tests']}")
    print(f"Overall Pass Rate: {summary['overall_pass_rate']:.2f}%")

    return validator, summary


def run_comprehensive_validation():
    """Run comprehensive validation with extended scenarios"""
    print("🎯 Running Comprehensive Bybit Calculations Validation")
    print("=" * 80)

    validator = BybitValidator()

    # Add comprehensive scenarios
    comprehensive_scenarios = create_comprehensive_test_scenarios()
    for scenario in comprehensive_scenarios:
        validator.add_scenario(scenario)

    # Run validation
    summary = validator.validate_all_scenarios()

    # Print detailed summary
    print("\n📊 COMPREHENSIVE VALIDATION SUMMARY")
    print(f"Total Scenarios: {summary['total_scenarios']}")
    print(f"Total Tests: {summary['total_tests']}")
    print(f"Passed: {summary['passed_tests']}")
    print(f"Failed: {summary['failed_tests']}")
    print(f"Overall Pass Rate: {summary['overall_pass_rate']:.2f}%")

    # Print per-scenario results
    print("\n📋 SCENARIO BREAKDOWN:")
    for scenario_name, scenario_data in summary['scenarios'].items():
        status = "✅" if scenario_data['pass_rate'] == 100 else "⚠️" if scenario_data['pass_rate'] >= 80 else "❌"
        print(f"   {status} {scenario_name}: {scenario_data['pass_rate']:.1f}% "
              f"({scenario_data['passed']}/{scenario_data['tests']})")

    return validator, summary


def run_integration_validation():
    """Run integration validation between Calculator and PositionTracker"""
    print("🔗 Running Integration Validation (Calculator vs PositionTracker)")
    print("=" * 80)

    validator = BybitValidator()

    # Test scenarios for integration
    integration_scenarios = [
        ValidationScenario(
            name="Integration_Long_10x",
            description="Integration test: Long position 10x leverage",
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=1.0,
            leverage=10
        ),
        ValidationScenario(
            name="Integration_Short_5x",
            description="Integration test: Short position 5x leverage",
            direction=Direction.SHORT,
            entry_price=45000,
            contract_qty=2.0,
            leverage=5
        ),
        ValidationScenario(
            name="Integration_HighLev_50x",
            description="Integration test: High leverage 50x",
            direction=Direction.LONG,
            entry_price=52000,
            contract_qty=0.5,
            leverage=50
        )
    ]

    integration_results = []

    for scenario in integration_scenarios:
        print(f"\n🧪 Testing Integration: {scenario.name}")
        comparison = validator.validate_against_position_tracker(scenario)
        integration_results.append({
            "scenario": scenario.name,
            "comparison": comparison
        })

        # Print results
        print(f"   Max Difference: ${comparison['max_difference']:.6f}")
        for metric, data in comparison['calculator_vs_tracker'].items():
            diff_pct = data['percentage_diff']
            status = "✅" if diff_pct < 0.01 else "⚠️" if diff_pct < 0.1 else "❌"
            print(f"   {status} {metric}: {diff_pct:.4f}% difference")

    return integration_results


def run_stress_validation():
    """Run stress validation with extreme scenarios"""
    print("💥 Running Stress Validation (Extreme Scenarios)")
    print("=" * 80)

    validator = BybitValidator()

    # Extreme scenarios
    extreme_scenarios = [
        ValidationScenario(
            name="Extreme_Leverage_125x",
            description="Maximum leverage scenario",
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=0.01,
            leverage=125,
            tolerance=0.005  # Higher tolerance for extreme cases
        ),
        ValidationScenario(
            name="Extreme_Position_100M",
            description="Extremely large position (100M)",
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=2000,  # 100M position
            leverage=2,
            tolerance=0.002
        ),
        ValidationScenario(
            name="Extreme_Low_Price",
            description="Very low price scenario",
            direction=Direction.LONG,
            entry_price=0.001,
            contract_qty=1000000,
            leverage=10,
            tolerance=0.01
        ),
        ValidationScenario(
            name="Extreme_High_Price",
            description="Very high price scenario",
            direction=Direction.SHORT,
            entry_price=1000000,
            contract_qty=0.1,
            leverage=5,
            tolerance=0.002
        )
    ]

    stress_results = []

    for scenario in extreme_scenarios:
        validator.add_scenario(scenario)
        print(f"\n💥 Testing: {scenario.name}")

        try:
            scenario_results = validator.validate_scenario(scenario)
            passed_count = sum(1 for r in scenario_results if r.passed)
            total_count = len(scenario_results)

            if total_count > 0:
                pass_rate = (passed_count / total_count) * 100
                status = "✅" if pass_rate == 100 else "⚠️" if pass_rate >= 80 else "❌"
                print(f"   {status} Pass Rate: {pass_rate:.1f}% ({passed_count}/{total_count})")

                stress_results.append({
                    "scenario": scenario.name,
                    "pass_rate": pass_rate,
                    "passed": passed_count,
                    "total": total_count
                })
            else:
                print("   ⚠️  No expected results to validate")

        except Exception as e:
            print(f"   ❌ Error during validation: {e}")
            stress_results.append({
                "scenario": scenario.name,
                "error": str(e)
            })

    return stress_results


def main():  # noqa: C901
    """Main validation script"""
    parser = argparse.ArgumentParser(description="Bybit Calculations Validation")
    parser.add_argument('--basic', action='store_true',
                       help='Run basic validation only')
    parser.add_argument('--comprehensive', action='store_true',
                       help='Run comprehensive validation')
    parser.add_argument('--integration', action='store_true',
                       help='Run integration validation')
    parser.add_argument('--stress', action='store_true',
                       help='Run stress validation with extreme scenarios')
    parser.add_argument('--all', action='store_true',
                       help='Run all validation types')
    parser.add_argument('--export-json',
                       help='Export results to JSON file')
    parser.add_argument('--scenarios-file',
                       help='Load scenarios from JSON file')
    parser.add_argument('--output-report',
                       help='Save detailed report to file')

    args = parser.parse_args()

    # Default to basic validation if no specific type is chosen
    if not any([args.basic, args.comprehensive, args.integration, args.stress, args.all]):
        args.basic = True

    results = {}

    try:
        # Run requested validation types
        if args.basic or args.all:
            validator, summary = run_basic_validation()
            results['basic'] = summary

            if args.export_json:
                json_file = args.export_json if args.export_json is not True else "basic_validation_results.json"
                validator.export_results_to_json(json_file)

        if args.comprehensive or args.all:
            validator, summary = run_comprehensive_validation()
            results['comprehensive'] = summary

            if args.export_json:
                json_file = args.export_json if args.export_json is not True else "comprehensive_validation_results.json"
                validator.export_results_to_json(json_file)

        if args.integration or args.all:
            integration_results = run_integration_validation()
            results['integration'] = integration_results

        if args.stress or args.all:
            stress_results = run_stress_validation()
            results['stress'] = stress_results

        # Generate and save report if requested
        if args.output_report and 'validator' in locals():
            report = validator.generate_validation_report()
            with open(args.output_report, 'w') as f:
                f.write(report)
            print(f"\n📄 Detailed report saved to {args.output_report}")

        # Print final summary
        print("\n🎉 VALIDATION COMPLETE")
        print("=" * 80)

        if 'basic' in results:
            print(f"Basic Validation: {results['basic']['overall_pass_rate']:.2f}% pass rate")
        if 'comprehensive' in results:
            print(f"Comprehensive Validation: {results['comprehensive']['overall_pass_rate']:.2f}% pass rate")
        if 'integration' in results:
            print(f"Integration Tests: {len(results['integration'])} scenarios tested")
        if 'stress' in results:
            valid_stress = [r for r in results['stress'] if 'pass_rate' in r]
            if valid_stress:
                avg_stress_rate = sum(r['pass_rate'] for r in valid_stress) / len(valid_stress)
                print(f"Stress Tests: {avg_stress_rate:.2f}% average pass rate")

        # Determine overall success
        if 'basic' in results and results['basic']['overall_pass_rate'] >= 95:
            print("✅ VALIDATION PASSED - Calculations are accurate!")
            return 0
        elif 'comprehensive' in results and results['comprehensive']['overall_pass_rate'] >= 90:
            print("⚠️  VALIDATION WARNING - Most calculations accurate, some issues detected")
            return 1
        else:
            print("❌ VALIDATION FAILED - Significant calculation errors detected")
            return 2

    except Exception as e:
        print(f"❌ VALIDATION ERROR: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())