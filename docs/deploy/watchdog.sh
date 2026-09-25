#!/usr/bin/env bash
# Liveness watchdog for gridbot (external to the bot process).
# cron: every 5 min. Alerts via Telegram on FATAL/BACKOFF or RUNNING-but-stale-log.
# Quiet on STOPPED (intentional pre-cutover stop). Throttle: 1 alert / 30 min.
# 0109: also enforces the status-file consumer contract (docs/deploy/status_watchdog.md).
set -uo pipefail

LOG=/home/gridbot/gridbot/gridbot.log
THROTTLE=/home/gridbot/gridbot/.watchdog_last_alert
STALE_MIN=10
THROTTLE_MIN=30

# --- 0109 status-file check settings ---
STATE_DIR=/home/gridbot/gridbot
STATUS_FILE="${STATUS_FILE:-/tmp/gridbot_status.json}"
STATUS_GRACE_SECONDS=360
STATUS_GRACE_MAX_RUNS=3
VENV_PYTHON=/home/gridbot/gridbot/.venv/bin/python
GRACE_COUNT_FILE="$STATE_DIR/.watchdog_status_grace_count"

status_cmd() {
  "$VENV_PYTHON" -m gridbot.status_check --path "$STATUS_FILE" --max-age-seconds 60
}

send() {
  cd /home/gridbot/gridbot
  set -a; source .env; set +a
  # Message passed via env var, never spliced into Python source: a message
  # containing or ending with a quote would otherwise be a SyntaxError and
  # the alert would be silently lost.
  WATCHDOG_MSG="$1" /home/gridbot/gridbot/.venv/bin/python -c "
import os, telebot
b = telebot.TeleBot(os.environ['TELEGRAM_BOT_TOKEN'])
b.send_message(os.environ['TELEGRAM_CHAT_ID'], '[gridbot-vps watchdog] ' + os.environ['WATCHDOG_MSG'])
" 2>/dev/null || logger -t gridbot-watchdog "telegram send failed"
}

# alert MSG [KEY] — KEY selects a separate throttle file so independent alert
# kinds (liveness / status / restart_loop) never suppress each other.
alert() {
  local now last throttle_file
  throttle_file="$THROTTLE${2:+_$2}"
  now=$(date +%s)
  if [ -f "$throttle_file" ]; then
    last=$(cat "$throttle_file" 2>/dev/null || echo 0)
    if [ $(( now - last )) -lt $(( THROTTLE_MIN * 60 )) ]; then return; fi
  fi
  echo "$now" > "$throttle_file"
  send "$1"
}

reset_grace_count() { echo 0 > "$GRACE_COUNT_FILE"; }

# 0109: status-file contract check, RUNNING branch only.
check_status_file() {
  local pid up grace_count out rc
  pid="$(sudo supervisorctl pid gridbot 2>/dev/null || true)"
  # STATUS_UPTIME is a manual-test override only (runbook step 5).
  up="${STATUS_UPTIME:-$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || true)}"
  # Empty/non-numeric uptime -> treat as past grace (fail toward alerting).
  case "$up" in ''|*[!0-9]*) up=999999 ;; esac

  grace_count="$(cat "$GRACE_COUNT_FILE" 2>/dev/null || echo 0)"
  case "$grace_count" in ''|*[!0-9]*|??????????*) grace_count=0 ;; esac

  if [ "$up" -lt "$STATUS_GRACE_SECONDS" ]; then
    # Inside startup grace (worst-case startup before the first "starting"
    # write is ~290s). Count in-grace samples: supervisord marks RUNNING after
    # startsecs=30, so a process dying between 30s and 360s restarts forever
    # without FATAL and would otherwise always be skipped.
    grace_count=$((10#$grace_count + 1))
    echo "$grace_count" > "$GRACE_COUNT_FILE"
    if [ "$grace_count" -ge "$STATUS_GRACE_MAX_RUNS" ]; then
      alert "ALERT reason=restart_loop uptime=${up}s — bot keeps restarting inside startup grace (${grace_count} samples)." restart_loop
    fi
    return
  fi

  reset_grace_count
  if out=$(status_cmd 2>&1); then rc=0; else rc=$?; fi
  if [ "$rc" -ne 0 ]; then
    alert "status_check rc=${rc}: $(printf '%s\n' "$out" | tail -n 3)" status
  fi
}

# Manual test hook: ./watchdog.sh test  -> sends one alert, bypasses throttle+state
if [ "${1:-}" = "test" ]; then
  send "TEST alert — watchdog channel OK"
  exit 0
fi

state=$(sudo supervisorctl status gridbot 2>/dev/null | awk '{print $2}')
case "$state" in
  FATAL|BACKOFF)
    reset_grace_count
    alert "process $state — bot DOWN, positions UNMANAGED. Fix ASAP." ;;
  RUNNING)
    if [ -f "$LOG" ]; then
      mtime=$(stat -c %Y "$LOG"); now=$(date +%s)
      if [ $(( now - mtime )) -gt $(( STALE_MIN * 60 )) ]; then
        alert "RUNNING but log stale >${STALE_MIN}m — bot may be HUNG."
      fi
    fi
    check_status_file ;;
  STARTING)
    : ;;   # leave the grace counter untouched (a restart loop may be sampled here)
  *)
    reset_grace_count ;;   # STOPPED / EXITED / empty -> intentional, stay quiet
esac
