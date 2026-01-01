#!/usr/bin/env python3
"""
Bybit Test Report Generator

Generates comprehensive HTML and text reports for Bybit calculation validation.
Includes charts, statistics, and detailed analysis.

Usage:
    python scripts/generate_test_report.py
    python scripts/generate_test_report.py --format html
    python scripts/generate_test_report.py --output custom_report.html
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from scripts.validate_calculations import run_comprehensive_validation  # noqa: E402


def generate_html_report(validation_data: Dict[str, Any], output_file: str = "bybit_validation_report.html"):
    """Generate comprehensive HTML report"""

    # Calculate statistics
    total_tests = validation_data.get('total_tests', 0)
    passed_tests = validation_data.get('passed_tests', 0)
    failed_tests = validation_data.get('failed_tests', 0)
    pass_rate = validation_data.get('overall_pass_rate', 0)

    # HTML template
    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bybit Calculations Validation Report</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}

        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            text-align: center;
            margin-bottom: 30px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}

        .header h1 {{
            margin: 0;
            font-size: 2.5em;
        }}

        .header p {{
            margin: 10px 0 0 0;
            opacity: 0.9;
        }}

        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}

        .stat-card {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            text-align: center;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            border-left: 4px solid #667eea;
        }}

        .stat-card.passed {{
            border-left-color: #4CAF50;
        }}

        .stat-card.failed {{
            border-left-color: #f44336;
        }}

        .stat-card.rate {{
            border-left-color: #FF9800;
        }}

        .stat-number {{
            font-size: 3em;
            font-weight: bold;
            margin: 0;
        }}

        .stat-number.passed {{
            color: #4CAF50;
        }}

        .stat-number.failed {{
            color: #f44336;
        }}

        .stat-number.rate {{
            color: #FF9800;
        }}

        .stat-label {{
            font-size: 1.1em;
            color: #666;
            margin-top: 10px;
        }}

        .progress-bar {{
            background-color: #e0e0e0;
            border-radius: 10px;
            overflow: hidden;
            height: 20px;
            margin: 20px 0;
        }}

        .progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, #4CAF50 0%, #45a049 100%);
            transition: width 0.3s ease;
        }}

        .scenarios-section {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 30px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}

        .scenarios-section h2 {{
            color: #667eea;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }}

        .scenario-item {{
            background: #f9f9f9;
            margin: 15px 0;
            padding: 20px;
            border-radius: 6px;
            border-left: 4px solid #ddd;
        }}

        .scenario-item.pass {{
            border-left-color: #4CAF50;
            background: #f1f8e9;
        }}

        .scenario-item.partial {{
            border-left-color: #FF9800;
            background: #fff3e0;
        }}

        .scenario-item.fail {{
            border-left-color: #f44336;
            background: #ffebee;
        }}

        .scenario-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }}

        .scenario-name {{
            font-weight: bold;
            font-size: 1.2em;
        }}

        .scenario-badge {{
            padding: 5px 15px;
            border-radius: 20px;
            color: white;
            font-weight: bold;
            font-size: 0.9em;
        }}

        .scenario-badge.pass {{
            background-color: #4CAF50;
        }}

        .scenario-badge.partial {{
            background-color: #FF9800;
        }}

        .scenario-badge.fail {{
            background-color: #f44336;
        }}

        .test-results {{
            margin-top: 15px;
        }}

        .test-item {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }}

        .test-item:last-child {{
            border-bottom: none;
        }}

        .test-name {{
            font-weight: 500;
        }}

        .test-values {{
            font-family: 'Courier New', monospace;
            font-size: 0.9em;
        }}

        .test-pass {{
            color: #4CAF50;
        }}

        .test-fail {{
            color: #f44336;
        }}

        .footer {{
            text-align: center;
            margin-top: 50px;
            padding: 20px;
            color: #666;
            border-top: 1px solid #ddd;
        }}

        .accuracy-chart {{
            margin: 20px 0;
            text-align: center;
        }}

        .chart-container {{
            display: inline-block;
            position: relative;
        }}

        .chart-circle {{
            width: 150px;
            height: 150px;
            border-radius: 50%;
            background: conic-gradient(#4CAF50 0deg {pass_rate * 3.6}deg, #f44336 {pass_rate * 3.6}deg 360deg);
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }}

        .chart-inner {{
            width: 100px;
            height: 100px;
            border-radius: 50%;
            background: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.5em;
            font-weight: bold;
            color: #333;
        }}

        @media (max-width: 768px) {{
            .summary-grid {{
                grid-template-columns: 1fr;
            }}

            .scenario-header {{
                flex-direction: column;
                align-items: flex-start;
            }}

            .scenario-badge {{
                margin-top: 10px;
            }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🎯 Bybit Calculations Validation Report</h1>
        <p>Generated on {datetime.now().strftime('%Y-%m-%d at %H:%M:%S')}</p>
    </div>

    <div class="summary-grid">
        <div class="stat-card">
            <div class="stat-number">{total_tests}</div>
            <div class="stat-label">Total Tests</div>
        </div>
        <div class="stat-card passed">
            <div class="stat-number passed">{passed_tests}</div>
            <div class="stat-label">Passed</div>
        </div>
        <div class="stat-card failed">
            <div class="stat-number failed">{failed_tests}</div>
            <div class="stat-label">Failed</div>
        </div>
        <div class="stat-card rate">
            <div class="stat-number rate">{pass_rate:.1f}%</div>
            <div class="stat-label">Pass Rate</div>
        </div>
    </div>

    <div class="accuracy-chart">
        <h3>Overall Accuracy</h3>
        <div class="chart-container">
            <div class="chart-circle">
                <div class="chart-inner">{pass_rate:.1f}%</div>
            </div>
        </div>
    </div>

    <div class="progress-bar">
        <div class="progress-fill" style="width: {pass_rate}%"></div>
    </div>
    """

    # Add scenarios section
    if 'scenarios' in validation_data:
        html_content += """
    <div class="scenarios-section">
        <h2>📊 Scenario Results</h2>
        """

        for scenario_name, scenario_data in validation_data['scenarios'].items():
            pass_rate_scenario = scenario_data.get('pass_rate', 0)

            # Determine status class
            if pass_rate_scenario == 100:
                status_class = "pass"
                badge_class = "pass"
                badge_text = "PASS"
            elif pass_rate_scenario >= 80:
                status_class = "partial"
                badge_class = "partial"
                badge_text = "PARTIAL"
            else:
                status_class = "fail"
                badge_class = "fail"
                badge_text = "FAIL"

            html_content += f"""
        <div class="scenario-item {status_class}">
            <div class="scenario-header">
                <div class="scenario-name">{scenario_name}</div>
                <div class="scenario-badge {badge_class}">{badge_text}</div>
            </div>
            <p>{scenario_data.get('description', 'No description available')}</p>
            <p><strong>Pass Rate:</strong> {pass_rate_scenario:.1f}%
            ({scenario_data.get('passed', 0)}/{scenario_data.get('tests', 0)} tests)</p>
            """

            # Add test results if available
            if 'results' in scenario_data:
                html_content += '<div class="test-results">'
                for result in scenario_data['results']:
                    test_class = "test-pass" if result.get('passed', False) else "test-fail"
                    status_symbol = "✅" if result.get('passed', False) else "❌"

                    html_content += f"""
                <div class="test-item">
                    <div class="test-name">{status_symbol} {result.get('metric', 'Unknown')}</div>
                    <div class="test-values {test_class}">
                        Expected: {result.get('expected', 0):.2f} |
                        Actual: {result.get('actual', 0):.2f} |
                        Error: {result.get('percentage_error', 0):.3f}%
                    </div>
                </div>
                    """
                html_content += '</div>'

            html_content += '</div>'

    # Add footer
    html_content += """
        </div>

    <div class="footer">
        <p>Report generated by Bybit Calculations Validation System</p>
        <p>🎯 Ensuring accurate position calculations for cryptocurrency trading</p>
    </div>
</body>
</html>
    """

    # Write to file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"📄 HTML report generated: {output_file}")
    return output_file


def generate_text_report(validation_data: Dict[str, Any], output_file: str = "bybit_validation_report.txt"):  # noqa: C901
    """Generate detailed text report"""

    lines = []
    lines.append("=" * 80)
    lines.append("🎯 BYBIT CALCULATIONS VALIDATION REPORT")
    lines.append("=" * 80)
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Summary
    total_tests = validation_data.get('total_tests', 0)
    passed_tests = validation_data.get('passed_tests', 0)
    failed_tests = validation_data.get('failed_tests', 0)
    pass_rate = validation_data.get('overall_pass_rate', 0)

    lines.append("📊 EXECUTIVE SUMMARY")
    lines.append("-" * 40)
    lines.append(f"Total Tests Executed: {total_tests}")
    lines.append(f"Tests Passed: {passed_tests}")
    lines.append(f"Tests Failed: {failed_tests}")
    lines.append(f"Overall Pass Rate: {pass_rate:.2f}%")
    lines.append("")

    # Status indicator
    if pass_rate >= 95:
        lines.append("✅ STATUS: EXCELLENT - All calculations highly accurate")
    elif pass_rate >= 90:
        lines.append("⚠️  STATUS: GOOD - Minor accuracy issues detected")
    elif pass_rate >= 80:
        lines.append("⚠️  STATUS: ACCEPTABLE - Some accuracy concerns")
    else:
        lines.append("❌ STATUS: POOR - Significant accuracy issues")
    lines.append("")

    # Progress bar
    bar_width = 50
    filled = int(bar_width * pass_rate / 100)
    bar = "█" * filled + "░" * (bar_width - filled)
    lines.append(f"Progress: [{bar}] {pass_rate:.1f}%")
    lines.append("")

    # Detailed scenario results
    if 'scenarios' in validation_data:
        lines.append("📋 DETAILED SCENARIO RESULTS")
        lines.append("-" * 80)

        for scenario_name, scenario_data in validation_data['scenarios'].items():
            scenario_pass_rate = scenario_data.get('pass_rate', 0)

            # Status symbol
            if scenario_pass_rate == 100:
                status = "✅"
            elif scenario_pass_rate >= 80:
                status = "⚠️"
            else:
                status = "❌"

            lines.append(f"{status} {scenario_name}")
            lines.append(f"   Description: {scenario_data.get('description', 'N/A')}")
            lines.append(f"   Pass Rate: {scenario_pass_rate:.1f}% ({scenario_data.get('passed', 0)}/{scenario_data.get('tests', 0)})")

            # Show failed tests
            if 'results' in scenario_data:
                failed_results = [r for r in scenario_data['results'] if not r.get('passed', False)]
                if failed_results:
                    lines.append("   Failed Tests:")
                    for result in failed_results:
                        lines.append(f"     • {result.get('metric', 'Unknown')}: "
                                   f"Expected {result.get('expected', 0):.4f}, "
                                   f"Got {result.get('actual', 0):.4f} "
                                   f"(Error: {result.get('percentage_error', 0):.3f}%)")
            lines.append("")

    # Accuracy analysis
    if 'scenarios' in validation_data:
        all_results = []
        for scenario_data in validation_data['scenarios'].values():
            if 'results' in scenario_data:
                all_results.extend(scenario_data['results'])

        if all_results:
            errors = [r.get('percentage_error', 0) for r in all_results]
            avg_error = sum(errors) / len(errors)
            max_error = max(errors)
            min_error = min(errors)

            lines.append("📈 ACCURACY ANALYSIS")
            lines.append("-" * 40)
            lines.append(f"Average Error: {avg_error:.4f}%")
            lines.append(f"Maximum Error: {max_error:.4f}%")
            lines.append(f"Minimum Error: {min_error:.4f}%")
            lines.append("")

    # Recommendations
    lines.append("💡 RECOMMENDATIONS")
    lines.append("-" * 40)

    if pass_rate >= 95:
        lines.append("• Calculations are highly accurate and production-ready")
        lines.append("• Continue monitoring with regular validation runs")
        lines.append("• Consider this implementation as the gold standard")
    elif pass_rate >= 90:
        lines.append("• Overall good accuracy with minor issues")
        lines.append("• Review failed test cases for potential improvements")
        lines.append("• Acceptable for production with monitoring")
    elif pass_rate >= 80:
        lines.append("• Accuracy is acceptable but needs improvement")
        lines.append("• Investigate and fix failed calculation methods")
        lines.append("• Run additional validation before production use")
    else:
        lines.append("• Significant accuracy issues detected")
        lines.append("• Review calculation implementation thoroughly")
        lines.append("• Not recommended for production use without fixes")

    lines.append("")
    lines.append("=" * 80)
    lines.append("Report generated by Bybit Calculations Validation System")
    lines.append("🎯 Ensuring accurate position calculations for cryptocurrency trading")
    lines.append("=" * 80)

    # Write to file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"📄 Text report generated: {output_file}")
    return output_file


def generate_json_summary(validation_data: Dict[str, Any], output_file: str = "validation_summary.json"):
    """Generate JSON summary for API consumption"""

    summary = {
        "timestamp": datetime.now().isoformat(),
        "version": "1.0",
        "validation_summary": {
            "total_tests": validation_data.get('total_tests', 0),
            "passed_tests": validation_data.get('passed_tests', 0),
            "failed_tests": validation_data.get('failed_tests', 0),
            "pass_rate": validation_data.get('overall_pass_rate', 0),
            "status": ("PASS" if validation_data.get('overall_pass_rate', 0) >= 95
                      else "WARN" if validation_data.get('overall_pass_rate', 0) >= 80 else "FAIL")
        },
        "scenarios": {}
    }

    if 'scenarios' in validation_data:
        for scenario_name, scenario_data in validation_data['scenarios'].items():
            summary["scenarios"][scenario_name] = {
                "description": scenario_data.get('description', ''),
                "pass_rate": scenario_data.get('pass_rate', 0),
                "passed": scenario_data.get('passed', 0),
                "total": scenario_data.get('tests', 0),
                "status": ("PASS" if scenario_data.get('pass_rate', 0) == 100
                          else "PARTIAL" if scenario_data.get('pass_rate', 0) >= 80 else "FAIL")
            }

    with open(output_file, 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"📄 JSON summary generated: {output_file}")
    return output_file


def main():  # noqa: C901
    """Main report generation function"""
    parser = argparse.ArgumentParser(description="Generate Bybit validation reports")
    parser.add_argument('--format', choices=['html', 'text', 'json', 'all'],
                       default='html', help='Report format')
    parser.add_argument('--output', help='Output file name')
    parser.add_argument('--run-validation', action='store_true',
                       help='Run validation before generating report')
    parser.add_argument('--validation-file', help='Use existing validation JSON file')

    args = parser.parse_args()

    # Get validation data
    if args.run_validation:
        print("🔄 Running comprehensive validation...")
        validator, validation_data = run_comprehensive_validation()
    elif args.validation_file:
        print(f"📂 Loading validation data from {args.validation_file}")
        with open(args.validation_file, 'r') as f:
            file_data = json.load(f)
            validation_data = file_data.get('summary', {})
    else:
        print("🔄 Running validation to generate fresh data...")
        validator, validation_data = run_comprehensive_validation()

    # Generate reports based on format
    generated_files = []

    if args.format == 'html' or args.format == 'all':
        output_file = args.output or "bybit_validation_report.html"
        generated_files.append(generate_html_report(validation_data, output_file))

    if args.format == 'text' or args.format == 'all':
        output_file = args.output or "bybit_validation_report.txt"
        if args.format == 'all':
            output_file = output_file.replace('.html', '.txt')
        generated_files.append(generate_text_report(validation_data, output_file))

    if args.format == 'json' or args.format == 'all':
        output_file = args.output or "validation_summary.json"
        if args.format == 'all':
            output_file = output_file.replace('.html', '.json').replace('.txt', '.json')
        generated_files.append(generate_json_summary(validation_data, output_file))

    # Summary
    print("\n🎉 Report generation complete!")
    print(f"Generated {len(generated_files)} file(s):")
    for file in generated_files:
        print(f"  📄 {file}")

    # Show validation summary
    pass_rate = validation_data.get('overall_pass_rate', 0)
    if pass_rate >= 95:
        print(f"✅ Validation Status: EXCELLENT ({pass_rate:.1f}% pass rate)")
    elif pass_rate >= 80:
        print(f"⚠️  Validation Status: GOOD ({pass_rate:.1f}% pass rate)")
    else:
        print(f"❌ Validation Status: NEEDS IMPROVEMENT ({pass_rate:.1f}% pass rate)")


if __name__ == "__main__":
    main()