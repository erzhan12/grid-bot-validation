#!/usr/bin/env bash
# Phase 4 recorder launcher: idempotent stop-of-prior + DB wipe + fresh start.
#
# Why this script: bare `uv run recorder &` is a footgun.
#   - $RECORDER_PID gets shadowed if the start command is re-run; old kill -INT no-ops.
#   - Two concurrent recorders writing the same SQLite DB → readonly errors.
#   - Stale DB rows from a prior run contaminate the new run_id.
# This script does the full reset cycle in one call.
#
# Usage: scripts/phase4/start_recorder.sh [config_path]
#   config_path: defaults to apps/recorder/conf/recorder_ltcusdt.yaml

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CONFIG="${1:-apps/recorder/conf/recorder_ltcusdt.yaml}"
LOG_FILE="/tmp/recorder.log"

if [[ ! -f "$CONFIG" ]]; then
  echo "ERROR: config not found: $CONFIG" >&2
  exit 1
fi

# Derive DB path from yaml `database_url` so we wipe the right file.
# Format: sqlite:///<relative_path>
DB_URL="$(grep -E '^database_url:' "$CONFIG" | sed -E 's/^database_url:[[:space:]]*"?([^"]+)"?$/\1/')"
DB_PATH="${DB_URL#sqlite:///}"
if [[ -z "$DB_PATH" || "$DB_PATH" == "$DB_URL" ]]; then
  echo "ERROR: cannot parse database_url from $CONFIG" >&2
  exit 1
fi

echo "==> Stopping any prior recorder for this config..."
# pkill -INT by command-line pattern. Matches both `uv run recorder` parent
# and the Python child. Returns 0 if killed, 1 if no match — both fine.
pkill -INT -f "recorder --config $CONFIG" || true

# Wait up to ~10s for graceful shutdown.
for i in 1 2 3 4 5 6 7 8 9 10; do
  if pgrep -f "recorder --config $CONFIG" > /dev/null; then
    sleep 1
  else
    break
  fi
done

if pgrep -f "recorder --config $CONFIG" > /dev/null; then
  echo "ERROR: recorder did not stop after 10s; manual intervention needed" >&2
  ps aux | grep -E "recorder --config $CONFIG" | grep -v grep >&2
  exit 1
fi
echo "    no recorder running for this config."

echo "==> Wiping prior recording artifacts..."
rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" "$LOG_FILE"
echo "    removed: $DB_PATH (and -wal/-shm), $LOG_FILE"

echo "==> Starting recorder (background, log → $LOG_FILE)..."
nohup uv run recorder --config "$CONFIG" > "$LOG_FILE" 2>&1 &
RECORDER_PID=$!
disown $RECORDER_PID 2>/dev/null || true

# Wait for the initial REST snapshot to land (recorder logs it within ~5-10s).
echo "==> Waiting for initial REST snapshot..."
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if grep -aq "Initial REST snapshot" "$LOG_FILE" 2>/dev/null; then
    break
  fi
  sleep 1
done

if ! grep -aq "Initial REST snapshot" "$LOG_FILE" 2>/dev/null; then
  echo "WARNING: no 'Initial REST snapshot' line in $LOG_FILE after 15s." >&2
  echo "         Recorder may still be starting. Check log:" >&2
  echo "           tail -f $LOG_FILE" >&2
  echo "         Process is alive: $(ps -p $RECORDER_PID -o pid= 2>/dev/null && echo yes || echo no)" >&2
else
  grep -aE "Initial REST snapshot|wallet_rows" "$LOG_FILE"
fi

echo ""
echo "Recorder PID: $RECORDER_PID"
echo "Tail logs:    tail -f $LOG_FILE"
echo "Stop:         scripts/phase4/stop_recorder.sh"
echo "Status:       scripts/phase4/status.sh"
