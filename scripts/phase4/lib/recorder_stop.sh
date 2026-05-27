#!/usr/bin/env bash
# Feature 0055 P2: shared recorder stop+verify helper.
#
# `start_recorder.sh` previously inlined the same SIGINT-and-wait-for-pgrep
# block in three places (stop-prior, rc=1 incomplete, rc=2 timeout). This lib
# centralises that logic so the kill contract is testable and the launcher
# branches that perform the side effects are no longer untested.
#
# Pure-ish function: side effects are real `pkill`/`pgrep`/`ps`/`sleep`
# calls, but no `exit`, no globals read, no stdout output. Tests stub
# pkill/pgrep/sleep/ps via same-named bash functions before sourcing —
# functions take precedence over external commands in bash lookup order.

# _stop_recorder_pattern PATTERN [WAIT_SECONDS]
#
# Send SIGINT to processes matching `pkill -f PATTERN`, then poll
# `pgrep -f PATTERN` for up to WAIT_SECONDS (default 10) until the pattern
# no longer matches.
#
# Returns:
#   0 — pattern no longer matches (clean shutdown, or nothing was running).
#   1 — pattern still matches after the wait; a diagnostic `ps aux` line is
#       printed to stderr. Caller should print a "manual intervention" error
#       and exit non-zero.
#
# Notes:
#   - `pkill -f` returns 1 when nothing matched — that is fine, suppressed
#     with `|| true`. Same for the case where the pattern was already gone
#     before the loop started: `pgrep` returns 1 on the first iteration and
#     the function returns 0 immediately.
#   - WAIT_SECONDS=0 skips the loop entirely and returns based on a single
#     post-pkill `pgrep` probe — used by tests for the still-alive branch.
_stop_recorder_pattern() {
  local pattern="${1-}"
  local wait_seconds="${2:-10}"

  # Guard: refuse empty pattern. `pkill -f ""` matches every process on the
  # system, so a caller bug passing an unset/empty arg must fail loud
  # (return 2) rather than wipe out unrelated processes.
  if [[ -z "$pattern" ]]; then
    echo "ERROR: _stop_recorder_pattern called with empty pattern; refusing to pkill" >&2
    return 2
  fi

  pkill -INT -f "$pattern" || true

  local i
  for ((i = 0; i < wait_seconds; i++)); do
    pgrep -f "$pattern" > /dev/null || return 0
    sleep 1
  done

  if pgrep -f "$pattern" > /dev/null; then
    ps aux | grep -E "$pattern" | grep -v grep >&2
    return 1
  fi
  return 0
}
