#!/usr/bin/env bash
# Phase 4 recorder launcher: idempotent stop-of-prior + prepare_session
# (surgical wipe + identity bootstrap + preflight verify) + fresh start.
#
# Why this script: bare `uv run recorder &` is a footgun.
#   - $RECORDER_PID gets shadowed if the start command is re-run; old kill -INT no-ops.
#   - Two concurrent recorders writing the same SQLite DB → readonly errors.
#   - Stale recorder rows from a prior run contaminate the new run_id.
# This script does the full reset cycle in one call.
#
# Feature 0049 + Feature 0053: Phase 4 default is a SHARED SQLite DB used by
# gridbot, recorder, replay, and comparator. Wipe + identity bootstrap run
# inside recorder.prepare_session on the `load_config`-resolved DB URL so
# both Python code and shell agree on the path (no shell YAML grep).
#
# Usage: scripts/phase4/start_recorder.sh [recorder_config_path] [gridbot_config_path]
#   recorder_config_path: defaults to apps/recorder/conf/recorder_ltcusdt.yaml
#   gridbot_config_path:  defaults to conf/gridbot_test.yaml
#                         override via 2nd arg OR GRIDBOT_CONFIG_PATH env (2nd arg wins)
#
# Exit codes (feature 0055):
#   0 — initial REST snapshot emitted RECORDER_SNAPSHOT_OK; recorder running.
#   1 — RECORDER_SNAPSHOT_INCOMPLETE, 15s sentinel timeout, or any other
#       failure path (config not found, prepare_recorder_session failed,
#       prior recorder did not stop, unexpected classifier rc).
# Callers (cron, CI, wrappers) MUST treat any non-zero exit as "recorder not
# running for this config" — no `Recorder PID:` tail is printed on failure.

set -euo pipefail

# shellcheck source=lib/recorder_snapshot_check.sh
. "$(dirname "$0")/lib/recorder_snapshot_check.sh"
# shellcheck source=lib/recorder_stop.sh
. "$(dirname "$0")/lib/recorder_stop.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CONFIG="${1:-apps/recorder/conf/recorder_ltcusdt.yaml}"
GRIDBOT_CONFIG="${2:-${GRIDBOT_CONFIG_PATH:-conf/gridbot_test.yaml}}"
LOG_FILE="/tmp/recorder.log"

if [[ ! -f "$CONFIG" ]]; then
  echo "ERROR: config not found: $CONFIG" >&2
  exit 1
fi
if [[ ! -f "$GRIDBOT_CONFIG" ]]; then
  echo "ERROR: gridbot config not found: $GRIDBOT_CONFIG" >&2
  exit 1
fi

echo "==> Stopping any prior recorder for this config..."
# pkill -INT by command-line pattern via shared helper (lib/recorder_stop.sh).
# Matches both `uv run recorder` parent and the Python child. Waits up to 10s.
if ! _stop_recorder_pattern "recorder --config $CONFIG"; then
  echo "ERROR: recorder did not stop after 10s; manual intervention needed" >&2
  exit 1
fi
echo "    no recorder running for this config."

echo "==> Preparing DB (surgical wipe + identity bootstrap)..."
if ! uv run python scripts/phase4/prepare_recorder_session.py "$CONFIG" --gridbot-config "$GRIDBOT_CONFIG"; then
  echo "ERROR: prepare_recorder_session failed; aborting recorder start" >&2
  exit 1
fi

rm -f "$LOG_FILE"
echo "    removed: $LOG_FILE"

echo "==> Starting recorder (background, log → $LOG_FILE)..."
nohup uv run recorder --config "$CONFIG" > "$LOG_FILE" 2>&1 &
RECORDER_PID=$!
disown $RECORDER_PID 2>/dev/null || true

# Wait for a terminal snapshot sentinel. Recorder emits one of:
#   RECORDER_SNAPSHOT_OK         — snapshot complete
#   RECORDER_SNAPSHOT_INCOMPLETE — auth failed, zero wallet/position rows, etc.
# Do NOT break on human-readable "Initial REST snapshot:" — that line is
# emitted before the zero-count WARNING and would race the failure path.
echo "==> Waiting for initial REST snapshot..."
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if grep -aqE "RECORDER_SNAPSHOT_OK|RECORDER_SNAPSHOT_INCOMPLETE" "$LOG_FILE" 2>/dev/null; then
    break
  fi
  sleep 1
done

# Dispatch on classifier exit code (not stdout — it prints diagnostic lines).
set +e
_classify_recorder_snapshot "$LOG_FILE"
_rc=$?
set -e

case "$_rc" in
  1)
    # Incomplete snapshot — fail loud. Classifier already printed diagnostics.
    if ! _stop_recorder_pattern "recorder --config $CONFIG"; then
      echo "ERROR: recorder did not stop after 10s; manual intervention needed" >&2
      exit 1
    fi
    echo "ERROR: recorder initial REST snapshot is incomplete (auth failure or zero wallet/position rows)." \
         "See the classifier diagnostic above for the specific cause." \
         "Check API credentials and network. Recorder stopped." >&2
    exit 1
    ;;
  0)
    # Success — print operator tail.
    echo ""
    echo "Recorder PID: $RECORDER_PID"
    echo "Tail logs:    tail -f $LOG_FILE"
    echo "Stop:         scripts/phase4/stop_recorder.sh"
    echo "Status:       scripts/phase4/status.sh"
    ;;
  2)
    # Timeout — no sentinel after 15s. Classifier already printed diagnostics.
    # Kill the recorder so a retrying caller (cron/CI) does not race a second
    # one onto the same SQLite DB.
    echo "       Process is alive: $(ps -p $RECORDER_PID -o pid= 2>/dev/null && echo yes || echo no)" >&2
    if ! _stop_recorder_pattern "recorder --config $CONFIG"; then
      echo "ERROR: recorder did not stop after 10s; manual intervention needed" >&2
      exit 1
    fi
    echo "ERROR: recorder initial REST snapshot timed out after 15s with no sentinel." \
         "Recorder stopped." >&2
    exit 1
    ;;
  *)
    # Fail loud on any unexpected classifier return — silent fallthrough here
    # would reintroduce the exact bug class feature 0055 eliminates.
    echo "ERROR: snapshot classifier returned unexpected rc=$_rc" >&2
    exit 1
    ;;
esac
