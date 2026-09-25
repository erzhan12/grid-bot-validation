# Status-file watchdog (feature 0109, issue #258 Phase 1)

The VPS cron watchdog (`/home/gridbot/gridbot/watchdog.sh`, Phase 4c of
`docs/deploy/vps_plan.md`) enforces the consumer contract for the feature 0082
health status file by calling `python -m gridbot.status_check` (contract:
`.claude/rules/gridbot.md` §"Health status file" → "Consumer contract").

**Deployed 2026-09-25** at repo commit `ead4e96`, verified end-to-end (4 Telegram
test alerts received).

**Source of truth: [`docs/deploy/watchdog.sh`](watchdog.sh)** — a byte-identical copy
of the installed script (sha1 `ec963961bcfa79f99915b19d73fe1baaf305f24a`). It holds no
secrets: Telegram credentials are read at send time from
`/home/gridbot/gridbot/.env`. Change the script **here first** (branch + PR), then
install it with the procedure below — never hand-edit the VPS copy, or the two drift.

**Every VPS step below is 👤-gated: run it only after the user has explicitly
approved that specific step, one step at a time.** This is the live mainnet bot
(`mainnet_live`, ssh alias `gridbot-vps`, user `gridbot`) — never stop/restart the
bot to test.

## What the script does

cron `*/5` (`CRON_TZ=UTC`, `gridbot` user crontab) runs it; it reads the supervisor
state and branches:

| supervisor state | action |
|---|---|
| `FATAL` / `BACKOFF` | alert "bot DOWN", reset the grace counter |
| `RUNNING` | (a) alert if `gridbot.log` mtime > 10 min old; (b) status-file check, below |
| `STARTING` | nothing — grace counter left untouched (a restart loop may be sampled here) |
| `STOPPED` / `EXITED` / empty | stay quiet (intentional stop), reset the grace counter |

Status-file check (RUNNING only):

- **Startup grace 360s.** Bot uptime = `ps -o etimes=` of `sudo supervisorctl pid
  gridbot`. Worst-case startup before the first `starting` write is ≈ 290s of REST
  retry budget; a startup still unfinished after 6 min alerts by design. Empty or
  non-numeric uptime → treated as past grace (fail toward alerting).
- **Restart-loop guard.** supervisord marks RUNNING after `startsecs=30`, so a bot
  dying between 30s and 360s restarts forever without FATAL. Each in-grace sample
  increments `.watchdog_status_grace_count`; 3 in a row (~10–15 min) →
  `ALERT reason=restart_loop`. A past-grace RUNNING sample resets it.
- **Past grace:** run the CLI capturing stdout+stderr and exit code; any `rc ≠ 0`
  sends `status_check rc=<rc>: <last 3 lines>` — so a traceback/import error is
  itself an alert, never an empty message.

### Alert throttling

`alert MSG [KEY]` — 1 alert per 30 min **per key**, one timestamp file each (all in
`/home/gridbot/gridbot/`):

| alert | throttle file |
|---|---|
| FATAL/BACKOFF, stale log (legacy, no key) | `.watchdog_last_alert` |
| status-file verdict (`status`) | `.watchdog_last_alert_status` |
| restart loop (`restart_loop`) | `.watchdog_last_alert_restart_loop` |

Separate keys mean a liveness alert never suppresses a status alert and vice versa.

### Facts about the VPS the original rollout plan got wrong

- `supervisorctl` as user `gridbot` fails with `PermissionError`; the script uses
  **`sudo supervisorctl`** (sudoers entry from Phase 4). Pre-checks must use `sudo`.
- The pre-0109 script had **one** throttle file for everything — the per-key
  `alert MSG KEY` form was added in 0109.
- The pre-0109 `send()` spliced the message into Python source as `'''$1'''`: any
  message ending in a quote (e.g. `No module named 'x'`) was a `SyntaxError` and the
  alert was **silently lost** (only a syslog `telegram send failed` line). Fixed:
  the text is passed via env var `WATCHDOG_MSG`. Never splice alert text into code.
- The script uses `set -uo pipefail` (no `-e`); new code must still not rely on
  `-e` being absent (e.g. `if out=$(…); then rc=0; else rc=$?; fi`).
- VPS `git pull` uses an HTTPS remote with no GitHub credentials — it works only
  while the repo is **public**. If it is made private again, add a read-only deploy
  key or ship commits via `git bundle` + `scp`.

## Install / reinstall procedure

### Step 1 — 👤 Read-only pre-checks

```bash
ssh gridbot-vps '
  grep -n status_file ~/gridbot-etc/gridbot.yaml || echo "defaults: /tmp/gridbot_status.json, enabled"
  jq -r ".state,.generated_at" /tmp/gridbot_status.json; date -u   # abort unless < 60s old
  sudo supervisorctl status gridbot
  P=$(sudo supervisorctl pid gridbot); ps -o etimes= -p "$P"       # must print an integer
  crontab -l | grep watchdog
  sha1sum ~/gridbot/watchdog.sh'
```

Compare the installed sha1 with `docs/deploy/watchdog.sh`. If the VPS copy differs
from the repo copy, stop: someone hand-edited it — reconcile into the repo first.

### Step 2 — 👤 Code update (no bot restart)

Confirm the `git log` delta with the user first — the pull also stages any other
merged changes for the next bot restart.

```bash
ssh gridbot-vps 'cd ~/gridbot && git pull --ff-only && ~/.local/bin/uv sync --locked'
```

The running bot never imports `gridbot.status_check`; no restart needed.

### Step 3 — 👤 Dry run

```bash
ssh gridbot-vps '~/gridbot/.venv/bin/python -m gridbot.status_check; echo rc=$?'
```

Expect `OK state=healthy age=<n>s path=/tmp/gridbot_status.json`, `rc=0`.

### Step 4 — 👤 Install the script (atomic, with backup)

```bash
scp docs/deploy/watchdog.sh gridbot-vps:/home/gridbot/gridbot/watchdog.sh.new
ssh gridbot-vps 'cd ~/gridbot && cp -p watchdog.sh watchdog.sh.bak \
  && chmod 700 watchdog.sh.new && bash -n watchdog.sh.new \
  && mv watchdog.sh.new watchdog.sh && ./watchdog.sh; echo rc=$?'
```

Upload under a temp name and `mv` so a cron tick never runs a half-written file.
The manual run on the real file must stay silent (no throttle files created).
Rollback: `cp ~/gridbot/watchdog.sh.bak ~/gridbot/watchdog.sh`.

### Step 5 — 👤 Verify end-to-end (sends 4 Telegram messages)

Uses only overrides (`STATUS_FILE`, `STATUS_UPTIME`) — the live bot and real status
file are untouched. Run in one go, not near a `*/5` tick (a real cron run would reset
the grace counter mid-test):

```bash
ssh gridbot-vps 'cd ~/gridbot
  .venv/bin/python -c "from datetime import datetime,UTC,timedelta; import json; json.dump({\"state\":\"healthy\",\"generated_at\":(datetime.now(UTC)-timedelta(seconds=120)).isoformat()}, open(\"/tmp/status_probe.json\",\"w\"))"
  ./watchdog.sh test                                                     # 1: TEST alert
  STATUS_UPTIME=999 STATUS_FILE=/tmp/status_probe.json ./watchdog.sh     # 2: reason=stale
  STATUS_UPTIME=999 STATUS_FILE=/tmp/status_probe.json ./watchdog.sh     #    (throttled, silent)
  echo 0 > .watchdog_status_grace_count
  for i in 1 2 3; do STATUS_UPTIME=60 ./watchdog.sh; done                # 3: restart_loop on 3rd
  rm -f .watchdog_last_alert_status
  STATUS_UPTIME=abc STATUS_FILE=/tmp/status_probe.json ./watchdog.sh     # 4: bad uptime -> stale alert
  journalctl -t gridbot-watchdog --since "-5min" --no-pager              # expect no "send failed"'
```

Expect exactly 4 messages prefixed `[gridbot-vps watchdog]`: `TEST alert`,
`status_check rc=1: ALERT reason=stale …`, `ALERT reason=restart_loop …`, and a
second `reason=stale`.

**Then clean up — mandatory**, or real `status` / `restart_loop` alerts stay muted
for 30 min:

```bash
ssh gridbot-vps 'cd ~/gridbot && rm -f .watchdog_last_alert_status .watchdog_last_alert_restart_loop /tmp/status_probe.json \
  && echo 0 > .watchdog_status_grace_count && ./watchdog.sh; echo rc=$?; ls .watchdog_last_alert* 2>/dev/null || echo "no throttle files"'
```

Do not delete `.watchdog_last_alert` (legacy key) — the test never creates it.

### Step 6 — 👤 Record

Note the install (date, commit, sha1) in the gitignored
`docs/deploy/VPS_STATE.local.md` Phase status section.

## Notes

- `/tmp` is wiped on reboot (`docs/deploy/vps_plan.md` §"Durable bot status path");
  the 360s grace makes that benign.
- No new cron line and no new alert channel — the existing `*/5` entry and Telegram
  bot are reused.
- Accepted gaps (see `docs/features/0109_PLAN.md`): a crash loop whose period is
  > 360s is not flagged as `restart_loop` (the bot manages positions between crashes);
  the non-RUNNING counter reset is not exercised live (would require stopping the bot).
- Out of scope (Phase 2 of #258): producer-side changes — `starting` before startup
  reconcile, terminal `failed` snapshot, `reasons[]`, last-success timestamps,
  threshold degradation, in-bot transition alerts.
