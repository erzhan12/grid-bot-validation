"""Console and JSON output for PnL check results.

Uses rich library for color-coded terminal tables.
Saves structured JSON to output/ directory.
"""

import json
import logging
from datetime import datetime, UTC
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from pnl_checker.comparator import ComparisonResult, PositionComparison, FieldComparison
from pnl_checker.config import PnlCheckerConfig

logger = logging.getLogger(__name__)

console = Console()


class _DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def _format_value(val: Decimal | str) -> str:
    """Format a value for display."""
    if isinstance(val, Decimal):
        # Show up to 8 decimal places, strip trailing zeros
        return f"{val:.8f}".rstrip("0").rstrip(".")
    return str(val)


def _status_text(passed: bool | None) -> Text:
    """Create a colored status indicator."""
    if passed is None:
        return Text("info", style="dim")
    if passed:
        return Text("PASS", style="bold green")
    return Text("FAIL", style="bold red")


def print_console(result: ComparisonResult) -> None:
    """Print comparison results to console with color coding."""
    console.print()
    console.rule("[bold]PnL Checker Results[/bold]")
    console.print()

    # Per-position tables
    for pos_comp in result.positions:
        _print_position_table(pos_comp)

    # Account summary
    if result.account.fields:
        _print_account_table(result)

    # Final verdict
    _print_verdict(result)


def _print_position_table(pos_comp: PositionComparison) -> None:
    """Print a table for a single position."""
    title = f"{pos_comp.symbol} — {pos_comp.direction.upper()}"
    table = Table(title=title, show_header=True, header_style="bold cyan")
    table.add_column("Field", style="white", min_width=28)
    table.add_column("Bybit", justify="right", min_width=18)
    table.add_column("Ours", justify="right", min_width=18)
    table.add_column("Delta", justify="right", min_width=14)
    table.add_column("Status", justify="center", min_width=6)

    for f in pos_comp.fields:
        bybit_str = _format_value(f.bybit_value)
        our_str = _format_value(f.our_value)
        delta_str = _format_value(f.delta)
        status = _status_text(f.passed)

        # Highlight delta in red if failed
        if f.passed is False:
            delta_text = Text(delta_str, style="bold red")
        elif f.passed is True:
            delta_text = Text(delta_str, style="green")
        else:
            delta_text = Text(delta_str, style="dim")

        table.add_row(f.field_name, bybit_str, our_str, delta_text, status)

    console.print(table)
    console.print(
        f"  Checked: {pos_comp.total_checked}  |  "
        f"[green]Pass: {pos_comp.pass_count}[/green]  |  "
        f"[red]Fail: {pos_comp.fail_count}[/red]"
    )
    console.print()


def _print_account_table(result: ComparisonResult) -> None:
    """Print account-level summary table."""
    table = Table(title="Account Summary", show_header=True, header_style="bold cyan")
    table.add_column("Field", style="white", min_width=28)
    table.add_column("Value", justify="right", min_width=20)

    for f in result.account.fields:
        table.add_row(f.field_name, _format_value(f.bybit_value))

    console.print(table)
    console.print()


def _print_verdict(result: ComparisonResult) -> None:
    """Print final pass/fail verdict."""
    console.print()
    if result.all_passed:
        console.print(
            f"[bold green]ALL CHECKS PASSED[/bold green] "
            f"({result.total_pass}/{result.total_pass + result.total_fail}) "
            f"tolerance={result.tolerance} USDT"
        )
    else:
        console.print(
            f"[bold red]CHECKS FAILED[/bold red] "
            f"({result.total_fail} failures out of {result.total_pass + result.total_fail}) "
            f"tolerance={result.tolerance} USDT"
        )
    console.print()


def _field_to_dict(f: FieldComparison) -> dict:
    """Convert a FieldComparison to a JSON-serializable dict."""
    return {
        "field": f.field_name,
        "bybit": f.bybit_value if isinstance(f.bybit_value, str) else str(f.bybit_value),
        "ours": f.our_value if isinstance(f.our_value, str) else str(f.our_value),
        "delta": f.delta if isinstance(f.delta, str) else str(f.delta),
        "passed": f.passed,
    }


def _redact_config(config: PnlCheckerConfig) -> dict:
    """Serialize config for JSON output, redacting credentials."""
    return {
        "api_key": "[REDACTED]",
        "api_secret": "[REDACTED]",
        "symbols": [s.model_dump(mode="json") for s in config.symbols],
        "risk_params": config.risk_params.model_dump(),
        "tolerance": config.tolerance,
        "funding_max_pages": config.funding_max_pages,
    }


def save_json(result: ComparisonResult, config: PnlCheckerConfig, output_dir: str = "output") -> str:
    """Save comparison results to a JSON file.

    Args:
        result: Comparison results to save
        config: PnL checker configuration (credentials will be redacted)
        output_dir: Directory for output files

    Returns:
        Path to the saved JSON file
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"pnl_check_{timestamp}.json"
    filepath = output_path / filename

    data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "tolerance": result.tolerance,
        "config": _redact_config(config),
        "summary": {
            "total_pass": result.total_pass,
            "total_fail": result.total_fail,
            "all_passed": result.all_passed,
        },
        "positions": [
            {
                "symbol": p.symbol,
                "direction": p.direction,
                "pass_count": p.pass_count,
                "fail_count": p.fail_count,
                "fields": [_field_to_dict(f) for f in p.fields],
            }
            for p in result.positions
        ],
        "account": [_field_to_dict(f) for f in result.account.fields],
    }

    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, cls=_DecimalEncoder)

    console.print(f"Results saved to [bold]{filepath}[/bold]")
    return str(filepath)
