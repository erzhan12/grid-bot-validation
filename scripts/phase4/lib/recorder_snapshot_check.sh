#!/usr/bin/env bash
# Feature 0055: classifier for recorder initial REST snapshot result.
#
# Provides _classify_recorder_snapshot, which inspects a recorder log file
# for terminal sentinels emitted by recorder.py (_write_initial_rest_snapshot,
# and Recorder.start when a collector does not start — feature 0110 B1c-2):
#   - RECORDER_SNAPSHOT_OK         → snapshot complete; wallet+position counts > 0
#   - RECORDER_SNAPSHOT_INCOMPLETE → a collector did not start, or the snapshot
#                                    failed (auth, zero counts, etc.)
#
# This file contains ONLY function definitions — no top-level side effects.
# Safe to source from pytest. Caller is responsible for kill/exit handling.

_classify_recorder_snapshot() {
  local log_file="${1:-${LOG_FILE:?LOG_FILE not set and no arg passed}}"
  # Seconds the caller waited for a sentinel (diagnostic text only).
  local waited="${2:-60}"

  if grep -aq "RECORDER_SNAPSHOT_INCOMPLETE" "$log_file"; then
    grep -aE "Initial REST snapshot incomplete|Recorder start aborted|RECORDER_SNAPSHOT_INCOMPLETE" "$log_file" >&2
    return 1
  elif grep -aq "RECORDER_SNAPSHOT_OK" "$log_file"; then
    grep -aE "Initial REST snapshot:|RECORDER_SNAPSHOT_OK" "$log_file"
    return 0
  else
    echo "ERROR: no RECORDER_SNAPSHOT_OK/INCOMPLETE in $log_file after ${waited}s." >&2
    echo "       Recorder may be hung. Check log:" >&2
    echo "         tail -f $log_file" >&2
    return 2
  fi
}
