from pathlib import Path

import yaml


def test_check_tier_drift_has_exact_permissions() -> None:
    """Job permissions must be exactly contents: read and issues: write."""
    repo_root = Path(__file__).resolve().parents[2]
    workflow_path = repo_root / ".github/workflows/risk-tier-monitor.yml"

    with workflow_path.open() as workflow_file:
        workflow = yaml.safe_load(workflow_file)

    assert workflow["jobs"]["check-tier-drift"]["permissions"] == {
        "contents": "read",
        "issues": "write",
    }
