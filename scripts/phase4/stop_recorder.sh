#!/usr/bin/env bash
# Stop the Phase 4 recorder gracefully and report final counters.
#
# Usage: scripts/phase4/stop_recorder.sh [config_path]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CONFIG="${1:-apps/recorder/conf/recorder_ltcusdt.yaml}"

DB_URL="$(grep -E '^database_url:' "$CONFIG" | sed -E 's/^database_url:[[:space:]]*"?([^"]+)"?$/\1/')"
DB_PATH="${DB_URL#sqlite:///}"

if ! pgrep -f "recorder --config $CONFIG" > /dev/null; then
  echo "(no recorder running for $CONFIG)"
  exit 0
fi

echo "==> Sending SIGINT..."
pkill -INT -f "recorder --config $CONFIG" || true

# Give it up to 15s to drain pending writes.
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if pgrep -f "recorder --config $CONFIG" > /dev/null; then
    sleep 1
  else
    break
  fi
done

if pgrep -f "recorder --config $CONFIG" > /dev/null; then
  echo "WARNING: recorder still running after 15s, escalating to SIGTERM..." >&2
  pkill -TERM -f "recorder --config $CONFIG" || true
  sleep 3
fi

if pgrep -f "recorder --config $CONFIG" > /dev/null; then
  echo "ERROR: recorder still alive after SIGTERM. Manual kill needed:" >&2
  ps aux | grep -E "recorder --config $CONFIG" | grep -v grep >&2
  exit 1
fi
echo "    stopped."

if [[ -f "$DB_PATH" ]]; then
  echo ""
  echo "==> Final DB state ($DB_PATH):"
  sqlite3 "$DB_PATH" "
    SELECT
      'tickers      ' || (SELECT COUNT(*) FROM ticker_snapshots) ||
      char(10) || 'executions   ' || (SELECT COUNT(*) FROM private_executions) ||
      char(10) || 'orders       ' || (SELECT COUNT(*) FROM orders) ||
      char(10) || 'positions    ' || (SELECT COUNT(*) FROM position_snapshots) ||
      char(10) || 'wallets      ' || (SELECT COUNT(*) FROM wallet_snapshots);
  "

  echo ""
  # Feature 0049: scope to the latest recording run. In a shared DB, the
  # newest row in `runs` can be a live gridbot run, and `bybit_accounts LIMIT 1`
  # can pick the wrong account row. Always filter by run_type='recording' and
  # take account_id from that same row.
  RUN_ID=$(sqlite3 "$DB_PATH" \
    "SELECT run_id FROM runs WHERE run_type='recording' ORDER BY start_ts DESC LIMIT 1;")
  ACCOUNT_ID=$(sqlite3 "$DB_PATH" \
    "SELECT account_id FROM runs WHERE run_type='recording' ORDER BY start_ts DESC LIMIT 1;")
  if [[ -z "$RUN_ID" ]]; then
    echo "WARNING: no run with run_type='recording' found in $DB_PATH" >&2
    echo "         (paste-into-replay will be blank; check that the recorder created its run row)" >&2
  fi
  echo "RUN_ID:     $RUN_ID"
  echo "ACCOUNT_ID: $ACCOUNT_ID"
fi
