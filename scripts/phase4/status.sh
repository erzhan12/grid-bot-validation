#!/usr/bin/env bash
# Quick status snapshot of the Phase 4 recorder + DB.
#
# Usage: scripts/phase4/status.sh [config_path]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CONFIG="${1:-apps/recorder/conf/recorder_ltcusdt.yaml}"
LOG_FILE="/tmp/recorder.log"

DB_URL="$(grep -E '^database_url:' "$CONFIG" | sed -E 's/^database_url:[[:space:]]*"?([^"]+)"?$/\1/')"
DB_PATH="${DB_URL#sqlite:///}"

echo "==> Process:"
if pgrep -f "recorder --config $CONFIG" > /dev/null; then
  ps -o pid,stat,etime,command -p "$(pgrep -f "recorder --config $CONFIG" | head -1)" | tail -1
else
  echo "    (not running)"
fi

echo ""
echo "==> Counters:"
if [[ -f "$DB_PATH" ]]; then
  sqlite3 "$DB_PATH" "
    SELECT
      'tickers      ' || (SELECT COUNT(*) FROM ticker_snapshots) ||
      char(10) || 'executions   ' || (SELECT COUNT(*) FROM private_executions) ||
      char(10) || 'orders       ' || (SELECT COUNT(*) FROM orders) ||
      char(10) || 'positions    ' || (SELECT COUNT(*) FROM position_snapshots) ||
      char(10) || 'wallets      ' || (SELECT COUNT(*) FROM wallet_snapshots);
  "
else
  echo "    (DB not present: $DB_PATH)"
fi

echo ""
echo "==> Recent notable log events (last 5):"
if [[ -f "$LOG_FILE" ]]; then
  tail -n 50 "$LOG_FILE" | grep -aE "WARN|ERROR|disconnect|reconnect|gap|Initial REST snapshot" | tail -5 || echo "    (none)"
else
  echo "    (log not present: $LOG_FILE)"
fi
