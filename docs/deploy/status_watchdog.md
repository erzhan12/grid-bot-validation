# Status-file watchdog rollout (feature 0109, issue #258 Phase 1)

Operator runbook for wiring `gridbot.status_check` (the pure, hermetically-tested
consumer for the feature 0082 health status file — see
`.claude/rules/gridbot.md` §"Health status file" → "Consumer contract") into the
existing VPS cron watchdog (`/home/gridbot/gridbot/watchdog.sh`, Phase 4c of
`docs/deploy/vps_plan.md`).

**Every step below is 👤-gated: run it only after the user has explicitly approved
that specific step, one step at a time.** This is the live mainnet bot
(`mainnet_live`, ssh alias `gridbot-vps`, user `gridbot`) — never batch these, never
stop/restart the bot to test, never run a step "because the plan says so" without a
fresh go-ahead in chat.

No automated deploy is in scope. This document is the checklist a human (or an
agent acting on explicit per-step approval) works through by hand over ssh.

## Prerequisites

- ssh access via the `gridbot-vps` alias.
- The Telegram bot/chat already configured for the existing watchdog alerts
  (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `/home/gridbot/gridbot/.env`) —
  reused as-is, no new channel.
- This repo's `feature/0109-status-watchdog` work merged to `main` (or otherwise
  present at the commit the operator intends to deploy) before step 2.

## Step 1 — 👤 Read-only pre-checks (no mutation)

Confirm the assumptions this rollout depends on before touching anything:

```bash
# Confirm the effective status_file_path (and that the feature isn't disabled)
grep -n status_file ~/gridbot-etc/gridbot.yaml
grep -n status_file_enabled ~/gridbot-etc/gridbot.yaml   # abort if the value is `false`

# Confirm the path found above is the one the bot actually writes (resolves any
# ${VAR} expansion) — abort unless generated_at is within the last 60s
jq '.state,.generated_at' <path from above>

# Locate the existing RUNNING branch, Telegram send function, and throttle
# mechanism in the current watchdog.sh — step 4's snippet must be adapted to
# whatever these actually are, not guessed
cat ~/gridbot/watchdog.sh

# Uptime-source pre-check (while supervisor reports RUNNING) — step 4's grace
# logic depends on both of these working; abort the rollout if either fails
supervisorctl pid gridbot                                   # must print a numeric PID, not 0
ps -o etimes= -p "$(supervisorctl pid gridbot)"              # must print an integer
```

If any check fails or looks inconsistent with what's below, stop and report back
before proceeding — do not improvise a fix on the live host.

## Step 2 — 👤 Code update (no bot restart)

```bash
cd ~/gridbot && git pull && ~/.local/bin/uv sync --locked
```

**No bot restart.** The running gridbot process never imports `gridbot.status_check`
— the watchdog invokes it as a separate short-lived process. Warn the user first:
`git pull` also stages any *other* merged changes for the next restart, whenever
that happens — confirm the `git log` delta with the user before pulling, since this
step affects more than just 0109.

## Step 3 — 👤 Manual dry run

```bash
~/gridbot/.venv/bin/python -m gridbot.status_check --path <path from step 1> --max-age-seconds 60; echo $?
```

Expected: `OK state=healthy age=<n>s path=<path>` and exit code `0`.

Negative probe (proves the missing-file path works before it's wired into alerting):

```bash
~/gridbot/.venv/bin/python -m gridbot.status_check --path /tmp/does_not_exist.json; echo $?
```

Expected: `ALERT reason=missing state=- age=-s path=/tmp/does_not_exist.json detail=...`
and exit code `1`.

## Step 4 — 👤 `watchdog.sh` extension

Backup first: `cp ~/gridbot/watchdog.sh ~/gridbot/watchdog.sh.bak`.

The block below has two parts. The **settings** part (from `# --- settings` down to
the `status_cmd` function) goes at the **top of the script**, before the supervisor
state branches — `STATE_DIR` must be defined for every branch, including the
non-RUNNING reset lines further down, or `set -u` aborts them. Everything from
`# --- inside the RUNNING branch` onward goes **inside the existing supervisor
RUNNING branch only** (STOPPED stays quiet — an intentionally stopped bot leaves a
stale status file by design, that's not a failure). Everything in `< >` must be adapted to what step 1's
`cat watchdog.sh` actually showed — do not paste this verbatim without substituting
the real function/path names.

```bash
# --- 0109 status-file watchdog extension ------------------------------------
#
# PLACEHOLDERS to adapt from the existing watchdog.sh (found in step 1):
#   <TELEGRAM_SEND_FUNCTION>  the existing function/command that sends a
#                             Telegram message, e.g. `send_telegram "$msg"`.
#   <THROTTLE_CHECK> / <THROTTLE_MARK>
#                             the existing throttle primitives keyed by a
#                             string, e.g. `throttle_ok "<key>"` /
#                             `throttle_mark "<key>"`. Reuse whatever
#                             watchdog.sh already uses for its FATAL/BACKOFF/
#                             log-stale alerts — do not invent a new one.
#
# --- settings (top of script, alongside watchdog.sh's other constants) -----
# STATE_DIR must be the SAME directory the existing throttle files already
# live in (found in step 1) — set explicitly here so it's never undefined
# under `set -u`.
STATE_DIR="<directory from step 1 where existing throttle/state files live>"
STATUS_FILE="${STATUS_FILE:-<status_file_path from step 1>}"
STATUS_GRACE_SECONDS=360
STATUS_GRACE_MAX_RUNS=3
VENV_PYTHON="/home/gridbot/gridbot/.venv/bin/python"   # absolute path — no `uv run` in cron (no PATH/lock resolution, lighter on 1 GB RAM)
# A function, not a string + eval: arguments stay quoted even if the path
# ever contains spaces or shell metacharacters.
status_cmd() {
    "$VENV_PYTHON" -m gridbot.status_check --path "$STATUS_FILE" --max-age-seconds 60
}

# --- inside the RUNNING branch, after the existing log-staleness check -----
# `|| true`: a non-zero supervisorctl exit must not abort a `set -e` script;
# an empty/0 pid then falls through to the numeric guard below.
pid="$(supervisorctl pid gridbot 2>/dev/null || true)"
# `|| true` keeps a failed `ps` (e.g. pid just churned) from aborting this
# `set -euo pipefail` script. STATUS_UPTIME is a manual-test override only
# (see step 5) — never used in normal cron operation.
up="${STATUS_UPTIME:-$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || true)}"

# Numeric guard: empty or non-numeric `up` (ps failed, pid gone, etc.) is
# treated as "past grace" — fail toward alerting, not silence.
case "$up" in
    ''|*[!0-9]*) up=999999 ;;
esac

grace_count_file="$STATE_DIR/status_grace_count"
# A missing counter file reads as 0; so does an empty or non-numeric one
# (same guard as `up`) — a corrupt counter can never abort the script.
grace_count="$(cat "$grace_count_file" 2>/dev/null || echo 0)"
# Also reset absurdly long values (bash arithmetic overflow), and force
# base 10 below so a value like `08` is not parsed as invalid octal.
case "$grace_count" in
    ''|*[!0-9]*|??????????*) grace_count=0 ;;
esac

if [ "$up" -lt "$STATUS_GRACE_SECONDS" ]; then
    # Still inside the 360s startup grace window (sized from the worst-case
    # REST-retry budget before the first "starting" write — see the
    # Consumer contract section of .claude/rules/gridbot.md). This branch
    # also catches a crash-loop restart that never survives past grace:
    # supervisord marks RUNNING after startsecs=30, so a process dying
    # between 30s and 360s is auto-restarted forever without reaching
    # FATAL. Count every in-grace sample instead of silently skipping it.
    grace_count=$((10#$grace_count + 1))
    echo "$grace_count" > "$grace_count_file"
    if [ "$grace_count" -ge "$STATUS_GRACE_MAX_RUNS" ]; then
        # Own throttle key ("restart_loop"), separate from "status" below —
        # a restart-loop page must never suppress a later stale/unhealthy
        # page, and vice versa.
        if <THROTTLE_CHECK> "restart_loop"; then
            <TELEGRAM_SEND_FUNCTION> "ALERT reason=restart_loop uptime=${up}s"
            <THROTTLE_MARK> "restart_loop"
        fi
    fi
else
    # Past grace: the bot has had time to write its first "starting"
    # snapshot and subsequent healthy sweeps. Reset the grace counter — an
    # ordinary restart must not accumulate toward the restart_loop alert.
    echo 0 > "$grace_count_file"

    # Capture BOTH streams and the exit code without aborting the script on
    # a non-zero exit: a bare `out=$(...); rc=$?` would trip `set -e` on
    # exit 1 before the alert below ever runs.
    if out=$(status_cmd 2>&1); then
        rc=0
    else
        rc=$?
    fi

    if [ "$rc" -ne 0 ]; then
        # Own throttle key ("status") — a traceback / import error / argparse
        # error on stderr is itself an alert (never send an empty message);
        # the normal `rc=1` case already carries a readable `ALERT ...` line.
        if <THROTTLE_CHECK> "status"; then
            tail_out="$(printf '%s\n' "$out" | tail -n 3)"
            <TELEGRAM_SEND_FUNCTION> "status_check rc=${rc}: ${tail_out}"
            <THROTTLE_MARK> "status"
        fi
    fi
fi
```

**Also add one line to every OTHER branch except RUNNING and STARTING** (STOPPED,
BACKOFF, FATAL, EXITED) so an intentional stop/start, or a FATAL the existing branch
already alerted on, doesn't leave a stale grace count behind:

```bash
# In the STOPPED / BACKOFF / FATAL / EXITED branches only — NOT in STARTING
# (a restart loop sampled partly in STARTING must still be counted, so
# STARTING samples must leave the counter untouched):
echo 0 > "$STATE_DIR/status_grace_count"
```

## Step 5 — 👤 Verify

1. `~/gridbot/watchdog.sh test` still fires (confirms the existing hook is intact).
2. Exercise the **complete new branch** (uptime gate → CLI → rc capture → `status`
   throttle → Telegram) without touching the live bot. `STATE_DIR` exists only inside
   `watchdog.sh`, so first set it in **your** shell to the same directory (step 1) —
   otherwise the commands below expand to `/status.throttle` etc.:

   ```bash
   STATE_DIR="<same directory as STATE_DIR in watchdog.sh>"
   ```

   ```bash
   cp <status path from step 1> /tmp/status_probe.json
   # wait more than 60s so the copy is now stale relative to max-age-seconds 60
   rm -f "$STATE_DIR/status.throttle"   # or whatever file the existing throttle uses for the "status" key
   STATUS_UPTIME=999 STATUS_FILE=/tmp/status_probe.json ~/gridbot/watchdog.sh
   ```

   Expect exactly **one** Telegram message: `status_check rc=1: ALERT reason=stale …`.
   Run it again immediately with the same env — expect **no** second message (throttle
   works).

3. Restart-loop branch:

   ```bash
   rm -f "$STATE_DIR/restart_loop.throttle"  # or whatever file the existing throttle uses for the "restart_loop" key
   echo 0 > "$STATE_DIR/status_grace_count"
   STATUS_UPTIME=60 ~/gridbot/watchdog.sh
   STATUS_UPTIME=60 ~/gridbot/watchdog.sh
   STATUS_UPTIME=60 ~/gridbot/watchdog.sh
   ```

   Run all three back-to-back right after a `*/5` cron tick fires, so a real cron
   run can't reset the counter between manual runs. Expect exactly **one**
   `ALERT reason=restart_loop` on the third invocation.

   Then reset: `STATUS_UPTIME=999 ~/gridbot/watchdog.sh` → confirm
   `status_grace_count` reads back `0`.

4. Malformed-uptime fallback:

   ```bash
   rm -f "$STATE_DIR/status.throttle"
   STATUS_UPTIME=abc STATUS_FILE=/tmp/status_probe.json ~/gridbot/watchdog.sh
   ```

   Expect it to treat `abc` as past-grace and alert (the numeric `case` guard
   defaults non-numeric to "past grace").

5. **Never stop/kill the live bot to test** — every probe above uses
   `STATUS_UPTIME=…` / `STATUS_FILE=/tmp/status_probe.json` overrides so the real
   PID and the real status file are untouched.

6. Confirm quiet on the real file (no `STATUS_FILE`/`STATUS_UPTIME` override) for at
   least 2 cron cycles (`*/5`, so ≥ 10 minutes) and that `status_grace_count` reads
   `0` throughout.

7. Clean up: `rm -f /tmp/status_probe.json`.

## Step 6 — 👤 Record the rollout

Append a line to the (gitignored) `docs/deploy/VPS_STATE.local.md` Phase status
section noting that the watchdog now enforces the 0109 status contract (date,
commit, who verified).

## Notes

- `/tmp` is wiped on reboot (see `docs/deploy/vps_plan.md` §"Durable bot status
  path"). The 360s grace window makes that benign for this feature — moving
  `status_file_path` off `/tmp` is optional and out of scope here.
- This rollout adds **no new cron line** — the existing `*/5 CRON_TZ=UTC`
  `gridbot` crontab entry already runs `watchdog.sh`, which now does more inside
  its RUNNING branch.
- This rollout adds **no new alert channel** — reuses whatever
  `<TELEGRAM_SEND_FUNCTION>` / `<THROTTLE_CHECK>` / `<THROTTLE_MARK>` already exist
  in `watchdog.sh` for its supervisor FATAL/BACKOFF/log-stale alerts.
- Out of scope for this rollout (Phase 2 of issue #258, not planned yet): writing
  `starting` before startup reconcile, a terminal `failed` snapshot, `reasons[]`,
  last-success timestamps, threshold degradation, in-bot transition alerts.
